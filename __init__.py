"""
ComfyUI-Tenaciousload
=====================
Self-contained fix for slow / black-screen ComfyUI loading when you have a huge
model/LoRA collection (especially on a network mount).

It injects an aiohttp middleware that caches the huge /api/object_info response
in memory and on disk (survives restarts) and serves it gzipped, so the slow
build runs only on the first load or an explicit refresh — not on every page
load. That build (and the refresh folder-walk) runs in a worker thread, so a
slow/stalling network model mount no longer freezes ComfyUI's event loop.

Three refresh modes are exposed (menu buttons, a graph node, and HTTP):
  * full     - clear ComfyUI's folder cache -> full re-walk of every model
               folder. Most thorough (catches moves/deletes anywhere). Slowest.
  * quick     - incremental: re-walk only the folders whose timestamp changed
               since the last scan, reuse the cache for the rest. Much faster on
               local disks; ~2x on a slow network mount (it still has to stat
               every folder to find which changed).
  * register - append specific file path(s) to the cache with NO folder walk.
               Instant disk-wise; use right after downloading a known file.

All modes then rebuild the object_info cache so new files show up.
"""

import os
import gzip
import json
import time
import asyncio
import hashlib
import logging
import threading

from aiohttp import web

import folder_paths
from server import PromptServer

log = logging.getLogger("Tenaciousload")

WEB_DIRECTORY = "./web"

# Disabled mode: do NOTHING to ComfyUI (no caching middleware, no refresh routes,
# no fingerprint, no graph node) — but still serve the loading-bar overlay.
# Toggle with TENACIOUSLOAD_DISABLED=1. Requires a restart, since the middleware
# is installed at startup.
_DISABLED = os.environ.get("TENACIOUSLOAD_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")

# --------------------------------------------------------------------------- #
#  object_info response cache (memory + disk)
# --------------------------------------------------------------------------- #
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_RAW_PATH = os.path.join(_CACHE_DIR, "object_info.json")
_GZ_PATH = os.path.join(_CACHE_DIR, "object_info.json.gz")
_SNAP_PATH = os.path.join(_CACHE_DIR, "scan_snapshot.json")
_SIG_PATH = os.path.join(_CACHE_DIR, "node_signature.txt")
_OBJECT_INFO_PATHS = ("/object_info", "/api/object_info")
_GZIP_LEVEL = 5

_lock = threading.Lock()
_mem = {"raw": None, "gz": None}
_disk_loaded = False


def _load_from_disk():
    global _disk_loaded
    _disk_loaded = True
    try:
        if os.path.exists(_GZ_PATH):
            with open(_GZ_PATH, "rb") as f:
                _mem["gz"] = f.read()
        if os.path.exists(_RAW_PATH):
            with open(_RAW_PATH, "rb") as f:
                _mem["raw"] = f.read()
        if _mem["gz"] is not None and _mem["raw"] is None:
            _mem["raw"] = gzip.decompress(_mem["gz"])
        if _mem["raw"] is not None:
            log.info("Tenaciousload: loaded object_info cache from disk (%d bytes raw)", len(_mem["raw"]))
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: failed to load disk cache: %s", e)


def _store(raw_bytes):
    with _lock:
        gz = gzip.compress(raw_bytes, _GZIP_LEVEL)
        _mem["raw"] = raw_bytes
        _mem["gz"] = gz
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(_RAW_PATH, "wb") as f:
                f.write(raw_bytes)
            with open(_GZ_PATH, "wb") as f:
                f.write(gz)
        except Exception as e:  # pragma: no cover
            log.warning("Tenaciousload: failed to persist disk cache: %s", e)
    log.info("Tenaciousload: cached object_info (%d bytes raw / %d gz)", len(raw_bytes), len(gz))


def invalidate_object_info_cache():
    with _lock:
        _mem["raw"] = None
        _mem["gz"] = None
        for p in (_RAW_PATH, _GZ_PATH):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except Exception as e:  # pragma: no cover
                log.warning("Tenaciousload: could not delete %s: %s", p, e)


def clear_comfy_model_cache():
    """Full reset: force ComfyUI to re-walk every model/LoRA folder next build."""
    try:
        folder_paths.filename_list_cache.clear()
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: could not clear filename_list_cache: %s", e)
    try:
        folder_paths.cache_helper.clear()
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: could not clear cache_helper: %s", e)


# --------------------------------------------------------------------------- #
#  Incremental folder scanning  (quick mode)
# --------------------------------------------------------------------------- #
# Snapshot layout: { folder_name: { root: { dirpath: {"m": mtime, "f": [names], "d": [subdir names]} } } }
_scan_lock = threading.Lock()
_snapshot = None
_SKIP_FOLDERS = {"custom_nodes"}


def _load_snapshot():
    global _snapshot
    if _snapshot is not None:
        return
    try:
        with open(_SNAP_PATH) as f:
            _snapshot = json.load(f)
    except Exception:
        _snapshot = {}


def _save_snapshot():
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _SNAP_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_snapshot, f)
        os.replace(tmp, _SNAP_PATH)
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: could not save scan snapshot: %s", e)


def _scandir_immediate(d):
    """One scandir of a single directory -> {m, f, d} or None if inaccessible."""
    files, subdirs = [], []
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name == ".git":
                    continue
                try:
                    if e.is_dir(follow_symlinks=True):
                        subdirs.append(e.name)
                    elif e.is_file(follow_symlinks=True):
                        files.append(e.name)
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    try:
        m = os.path.getmtime(d)
    except OSError:
        m = 0.0
    return {"m": m, "f": files, "d": subdirs}


def _scan_root_incremental(root, old):
    """Walk a root, scandir-ing only dirs whose mtime changed; reuse the rest."""
    new = {}
    scanned = reused = 0
    visited = set()  # real paths, to defend against symlink cycles on network mounts
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            rp = os.path.realpath(d)
        except OSError:
            continue
        if rp in visited:
            continue
        visited.add(rp)
        try:
            m = os.path.getmtime(d)
        except OSError:
            continue  # directory disappeared -> drop it
        rec = old.get(d)
        if rec is not None and rec["m"] == m:
            new[d] = rec
            reused += 1
            for sub in rec["d"]:
                stack.append(os.path.join(d, sub))
        else:
            fresh = _scandir_immediate(d)
            if fresh is None:
                continue
            new[d] = fresh
            scanned += 1
            for sub in fresh["d"]:
                stack.append(os.path.join(d, sub))
    return new, scanned, reused


def incremental_scan_folder(folder_name):
    """Update folder_paths' cached file list for one folder type, incrementally."""
    folder_name = folder_paths.map_legacy(folder_name)
    fnp = folder_paths.folder_names_and_paths.get(folder_name)
    if not fnp:
        return None
    roots, exts = fnp[0], fnp[1]
    if _snapshot is None:
        _load_snapshot()
    folder_snap = _snapshot.setdefault(folder_name, {})
    all_rel, dirs_mtime = set(), {}
    scanned = reused = 0
    for root in roots:
        if not os.path.isdir(root):
            folder_snap.pop(root, None)
            continue
        new, s, r = _scan_root_incremental(root, folder_snap.get(root, {}))
        folder_snap[root] = new
        scanned += s
        reused += r
        for d, rec in new.items():
            dirs_mtime[d] = rec["m"]
            for fname in rec["f"]:
                all_rel.add(os.path.relpath(os.path.join(d, fname), root))
    filtered = folder_paths.filter_files_extensions(all_rel, exts)
    folder_paths.filename_list_cache[folder_name] = (filtered, dirs_mtime, time.perf_counter())
    return {"folder": folder_name, "files": len(filtered), "scanned": scanned, "reused": reused}


def quick_rescan_all():
    """Incrementally refresh every model folder type. Returns a per-folder summary."""
    with _scan_lock:
        if _snapshot is None:
            _load_snapshot()
        results = []
        for folder_name in list(folder_paths.folder_names_and_paths.keys()):
            if folder_name in _SKIP_FOLDERS:
                continue
            try:
                r = incremental_scan_folder(folder_name)
                if r and (r["scanned"] or r["files"]):
                    results.append(r)
            except Exception as e:  # pragma: no cover
                log.warning("Tenaciousload: quick scan of '%s' failed: %s", folder_name, e)
        _save_snapshot()
        # also drop ComfyUI's strong request-cache so the new lists are picked up
        try:
            folder_paths.cache_helper.clear()
        except Exception:
            pass
        return results


def register_files(folder_name, rel_paths):
    """Append specific files to a folder's cache with no disk walk. Returns counts."""
    folder_name = folder_paths.map_legacy(folder_name)
    fnp = folder_paths.folder_names_and_paths.get(folder_name)
    if not fnp:
        return {"added": 0, "skipped": len(rel_paths), "folder": folder_name}
    roots, exts = fnp[0], fnp[1]
    cache = folder_paths.filename_list_cache.get(folder_name)
    if cache is None:
        cache = folder_paths.get_filename_list_(folder_name)
    files, dirs = set(cache[0]), dict(cache[1])
    added = skipped = 0
    for rp in rel_paths:
        rp = (rp or "").strip().strip("/\\")
        if not rp:
            continue
        placed = False
        for root in roots:
            full = os.path.join(root, rp)
            if os.path.exists(full):
                d, rootn = os.path.dirname(full), os.path.normpath(root)
                while True:  # bump mtimes from the file's dir up to the root
                    try:
                        if os.path.isdir(d):
                            dirs[d] = os.path.getmtime(d)
                    except OSError:
                        pass
                    if os.path.normpath(d) == rootn:
                        break
                    parent = os.path.dirname(d)
                    if parent == d:
                        break
                    d = parent
                files.add(rp)
                placed = True
                break
        if placed:
            added += 1
        else:
            skipped += 1
    filtered = folder_paths.filter_files_extensions(files, exts)
    folder_paths.filename_list_cache[folder_name] = (filtered, dirs, time.perf_counter())
    try:
        folder_paths.cache_helper.clear()
    except Exception:
        pass
    return {"added": added, "skipped": skipped, "folder": folder_name, "files": len(filtered)}


# --------------------------------------------------------------------------- #
#  Auto-invalidate when installed nodes change (install / update / enable / remove)
# --------------------------------------------------------------------------- #
_sig_checked = False


def _custom_nodes_code_hash():
    """Hash of every custom-node .py path + mtime. Changes when a node's code is
    updated in place (git pull / edit) even if its class names stay the same, so
    feature updates to an existing node are detected. (custom_nodes is local, so
    this walk is fast; __pycache__ is skipped so it has no false positives.)"""
    try:
        bases = folder_paths.get_folder_paths("custom_nodes")
    except Exception:
        bases = [os.path.join(getattr(folder_paths, "base_path", "."), "custom_nodes")]
    skip = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
    items = []
    for base in sorted(set(bases)):
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for fn in filenames:
                if fn.endswith(".py"):
                    p = os.path.join(dirpath, fn)
                    try:
                        items.append((p, os.path.getmtime(p)))
                    except OSError:
                        pass
    h = hashlib.sha1()
    for p, mt in sorted(items):
        h.update(p.encode("utf-8", "replace"))
        h.update(f":{mt}\x00".encode())
    return h.hexdigest()


def _current_node_signature():
    """Fingerprint of the available nodes AND their code. Changes when a node is
    installed, removed, enabled, disabled, or updated in place."""
    try:
        import nodes
        keys = sorted(nodes.NODE_CLASS_MAPPINGS.keys())
    except Exception:
        return None
    h = hashlib.sha1()
    for k in keys:
        h.update(k.encode("utf-8", "replace"))
        h.update(b"\x00")
    try:
        code = _custom_nodes_code_hash()
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: custom_nodes code hash failed: %s", e)
        code = ""
    # Optionally fold in the input-dir mtime so new files in the input folder
    # show up after a restart. OFF BY DEFAULT: on a high-churn input folder
    # (e.g. a video workflow that constantly adds clips) it changes on nearly
    # every restart, invalidating the cache and forcing a slow rebuild — which
    # defeats the whole point. Enable with TENACIOUSLOAD_WATCH_INPUT=1 only if
    # your input folder is fairly static. Otherwise use a refresh button for new
    # input files, same as for new models.
    inp = ""
    if os.environ.get("TENACIOUSLOAD_WATCH_INPUT", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            inp = str(os.path.getmtime(folder_paths.get_input_directory()))
        except Exception:
            inp = ""
    return f"{len(keys)}:{h.hexdigest()}:{code}:{inp}"


def _check_node_signature():
    """Drop the cached object_info if the node set changed since last run, so
    newly installed/updated nodes show up without a manual refresh."""
    global _sig_checked
    _sig_checked = True
    try:
        cur = _current_node_signature()
        if cur is None:
            return
        old = None
        if os.path.exists(_SIG_PATH):
            with open(_SIG_PATH) as f:
                old = f.read().strip()
        if old is not None and old != cur:
            log.info("Tenaciousload: installed node set changed -> invalidating object_info cache")
            invalidate_object_info_cache()
        if old != cur:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(_SIG_PATH, "w") as f:
                f.write(cur)
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: node signature check failed: %s", e)


# --------------------------------------------------------------------------- #
#  Off-loop object_info builder
# --------------------------------------------------------------------------- #
# Building object_info walks the model folders synchronously. On a slow/stalling
# network mount that walk blocks ComfyUI's single event loop = the whole UI
# hangs. We instead run the build in a worker thread: the folder-walk syscalls
# release the GIL while they wait on the NAS, so the event loop stays responsive.
_node_info_fn = None
_node_info_resolved = False

# Live build progress, surfaced at /tenaciousload/status for the loading overlay.
_build_state = {"building": False, "started": 0.0, "done": 0, "total": 0, "last_ms": 0, "last_bytes": 0}

# Single-flight: only ONE object_info build may run at a time. ComfyUI's
# cache_helper is a global, so two concurrent builds (e.g. a manual refresh
# fired during a rebuild) corrupt it and the second build re-walks the network
# mount per-node = a hang. Concurrent requests wait on this and serve the result.
_build_lock = asyncio.Lock()


def _resolve_node_info_fn():
    """Pull ComfyUI's own `node_info` closure off the /object_info route, so the
    threaded build is byte-for-byte the same logic (no drift). Routes are added
    after custom nodes load, so this is done lazily on first use."""
    global _node_info_fn, _node_info_resolved
    _node_info_resolved = True
    try:
        for route in PromptServer.instance.app.router.routes():
            if route.method != "GET":
                continue
            path = getattr(route.resource, "canonical", None)
            if path not in ("/object_info", "/api/object_info"):
                continue
            fn = getattr(route.handler, "__wrapped__", route.handler)
            code = getattr(fn, "__code__", None)
            if code and fn.__closure__:
                for name, cell in zip(code.co_freevars, fn.__closure__):
                    if name == "node_info" and callable(cell.cell_contents):
                        _node_info_fn = cell.cell_contents
                        log.info("Tenaciousload: threaded object_info build enabled")
                        return
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: could not resolve node_info (%s); builds stay on the loop", e)


def _build_object_info_bytes():
    """Replicate ComfyUI's object_info build. Runs in a worker thread."""
    import nodes
    keys = list(nodes.NODE_CLASS_MAPPINGS.keys())
    _build_state.update(building=True, started=time.time(), done=0, total=len(keys))
    out = {}
    try:
        with folder_paths.cache_helper:
            for i, x in enumerate(keys):
                try:
                    out[x] = _node_info_fn(x)
                except Exception:  # pragma: no cover
                    log.error("Tenaciousload: node_info failed for '%s'", x, exc_info=True)
                _build_state["done"] = i + 1
        raw = json.dumps(out).encode("utf-8")
        _build_state["last_bytes"] = len(raw)
        return raw
    finally:
        _build_state["building"] = False
        _build_state["last_ms"] = int((time.time() - _build_state["started"]) * 1000)


async def _build_object_info_off_loop():
    """Build object_info in a thread; return raw bytes, or None to fall back."""
    if _node_info_fn is None and not _node_info_resolved:
        _resolve_node_info_fn()
    if _node_info_fn is None:
        return None
    try:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, _build_object_info_bytes)
        if isinstance(raw, (bytes, bytearray)) and len(raw) > 1000:  # sanity: real one is huge
            return bytes(raw)
        log.warning("Tenaciousload: threaded build looked wrong (%d bytes); falling back", len(raw or b""))
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: threaded build failed (%s); falling back", e)
    return None


# --------------------------------------------------------------------------- #
#  object_info caching middleware
# --------------------------------------------------------------------------- #
def _serve_cached(request):
    raw, gz = _mem["raw"], _mem["gz"]
    if "gzip" in request.headers.get("Accept-Encoding", "") and gz is not None:
        return web.Response(
            body=gz, status=200, content_type="application/json",
            headers={"Content-Encoding": "gzip", "X-Tenaciousload-Cache": "HIT",
                     "Cache-Control": "no-store"},
        )
    if raw is None and gz is not None:
        raw = gzip.decompress(gz)
    return web.Response(
        body=raw, status=200, content_type="application/json",
        headers={"X-Tenaciousload-Cache": "HIT", "Cache-Control": "no-store"},
    )


@web.middleware
async def _object_info_cache_mw(request, handler):
    if request.method != "GET" or request.path not in _OBJECT_INFO_PATHS:
        return await handler(request)

    global _disk_loaded
    if not _disk_loaded:
        _load_from_disk()
    if not _sig_checked:
        _check_node_signature()  # auto-drop cache if nodes were installed/updated

    if "nocache" not in request.query and _mem["raw"] is not None:
        return _serve_cached(request)

    # MISS / refresh: build in a worker thread so a slow folder-walk does not
    # freeze the event loop. Single-flight via _build_lock — a concurrent
    # request (e.g. a manual refresh during a rebuild) waits here and then serves
    # the fresh result instead of starting a second, conflicting build.
    async with _build_lock:
        # another request may have finished the build while we waited for the lock
        if "nocache" not in request.query and _mem["raw"] is not None:
            return _serve_cached(request)
        raw = await _build_object_info_off_loop()
        if raw is not None:
            _store(raw)
            return _serve_cached(request)
        # off-loop build unavailable -> in-loop handler (still under the lock)
        resp = await handler(request)
        try:
            body = getattr(resp, "body", None)
            if resp.status == 200 and isinstance(body, (bytes, bytearray)) and len(body) > 0:
                _store(bytes(body))
                return _serve_cached(request)
        except Exception as e:  # pragma: no cover
            log.warning("Tenaciousload: caching skipped: %s", e)
        return resp


def _install_middleware():
    try:
        PromptServer.instance.app.middlewares.insert(0, _object_info_cache_mw)
        log.info("Tenaciousload: object_info cache middleware installed")
    except Exception as e:
        log.error("Tenaciousload: could not install cache middleware (loads will be slow): %s", e)


if _DISABLED:
    log.info("Tenaciousload: DISABLED — no middleware / caching / refresh (loading overlay only)")
else:
    _install_middleware()


# --------------------------------------------------------------------------- #
#  Refresh API
# --------------------------------------------------------------------------- #
@PromptServer.instance.routes.get("/tenaciousload/status")
async def _status(request):
    st = {
        "enabled": not _DISABLED,
        "building": _build_state["building"],
        "done": _build_state["done"],
        "total": _build_state["total"],
        "last_ms": _build_state["last_ms"],
        "cached": _mem["raw"] is not None,
        "cache_bytes": len(_mem["raw"]) if _mem["raw"] else 0,
        "gz_bytes": len(_mem["gz"]) if _mem["gz"] else 0,
    }
    if _build_state["building"] and _build_state["started"]:
        st["elapsed"] = round(time.time() - _build_state["started"], 1)
    return web.json_response(st)


async def _refresh(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    mode = (data.get("mode") or "full").lower()
    loop = asyncio.get_event_loop()

    if mode == "quick":
        # run the folder walk off the loop so the UI stays responsive
        summary = await loop.run_in_executor(None, quick_rescan_all)
        invalidate_object_info_cache()
        rescanned = sum(s["scanned"] for s in summary)
        log.info("Tenaciousload: quick refresh — %d folders touched, %d dirs rescanned", len(summary), rescanned)
        return web.json_response({"status": "ok", "mode": "quick", "folders": summary})

    if mode == "register":
        folder = data.get("folder") or "loras"
        files = data.get("files") or []
        result = await loop.run_in_executor(None, register_files, folder, files)
        invalidate_object_info_cache()
        log.info("Tenaciousload: register — %s", result)
        return web.json_response({"status": "ok", "mode": "register", **result})

    # default: full
    clear_comfy_model_cache()
    invalidate_object_info_cache()
    log.info("Tenaciousload: full refresh — folder cache cleared")
    return web.json_response({"status": "ok", "mode": "full"})


if not _DISABLED:
    PromptServer.instance.routes.post("/tenaciousload/refresh")(_refresh)


# --------------------------------------------------------------------------- #
#  Optional graph node (workflow automation)
# --------------------------------------------------------------------------- #
class _AnyType(str):
    def __ne__(self, other):
        return False


ANY = _AnyType("*")


class TenaciousloadRefresh:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"mode": (["quick", "full"], {"default": "quick"})},
            "optional": {"trigger": (ANY, {})},
        }

    RETURN_TYPES = (ANY,)
    RETURN_NAMES = ("trigger",)
    FUNCTION = "refresh"
    CATEGORY = "Tenaciousload"
    OUTPUT_NODE = True
    DESCRIPTION = "Refresh the model/LoRA cache (quick = changed folders only, full = rescan everything)."

    def refresh(self, mode="quick", trigger=None):
        if mode == "full":
            clear_comfy_model_cache()
        else:
            quick_rescan_all()
        invalidate_object_info_cache()
        return (trigger,)


NODE_CLASS_MAPPINGS = {} if _DISABLED else {"TenaciousloadRefresh": TenaciousloadRefresh}
NODE_DISPLAY_NAME_MAPPINGS = {} if _DISABLED else {"TenaciousloadRefresh": "🔄 Refresh Models/LoRAs (Tenaciousload)"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
