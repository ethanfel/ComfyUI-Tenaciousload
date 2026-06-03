"""
ComfyUI-Tenaciousload
=====================
Self-contained fix for slow / black-screen ComfyUI loading when you have a huge
model/LoRA collection (especially on a network mount).

It injects an aiohttp middleware that caches the huge /api/object_info response
in memory and on disk (survives restarts) and serves it gzipped, so the slow
build (which freezes ComfyUI's event loop) runs only on the first load or an
explicit refresh — not on every page load.

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
import logging
import threading

from aiohttp import web

import folder_paths
from server import PromptServer

log = logging.getLogger("Tenaciousload")

WEB_DIRECTORY = "./web"

# --------------------------------------------------------------------------- #
#  object_info response cache (memory + disk)
# --------------------------------------------------------------------------- #
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_RAW_PATH = os.path.join(_CACHE_DIR, "object_info.json")
_GZ_PATH = os.path.join(_CACHE_DIR, "object_info.json.gz")
_SNAP_PATH = os.path.join(_CACHE_DIR, "scan_snapshot.json")
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
    stack = [root]
    while stack:
        d = stack.pop()
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

    if "nocache" not in request.query and _mem["raw"] is not None:
        return _serve_cached(request)

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


_install_middleware()


# --------------------------------------------------------------------------- #
#  Refresh API
# --------------------------------------------------------------------------- #
@PromptServer.instance.routes.post("/tenaciousload/refresh")
async def _refresh(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    mode = (data.get("mode") or "full").lower()

    if mode == "quick":
        summary = quick_rescan_all()
        invalidate_object_info_cache()
        rescanned = sum(s["scanned"] for s in summary)
        log.info("Tenaciousload: quick refresh — %d folders touched, %d dirs rescanned", len(summary), rescanned)
        return web.json_response({"status": "ok", "mode": "quick", "folders": summary})

    if mode == "register":
        folder = data.get("folder") or "loras"
        files = data.get("files") or []
        result = register_files(folder, files)
        invalidate_object_info_cache()
        log.info("Tenaciousload: register — %s", result)
        return web.json_response({"status": "ok", "mode": "register", **result})

    # default: full
    clear_comfy_model_cache()
    invalidate_object_info_cache()
    log.info("Tenaciousload: full refresh — folder cache cleared")
    return web.json_response({"status": "ok", "mode": "full"})


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


NODE_CLASS_MAPPINGS = {"TenaciousloadRefresh": TenaciousloadRefresh}
NODE_DISPLAY_NAME_MAPPINGS = {"TenaciousloadRefresh": "🔄 Refresh Models/LoRAs (Tenaciousload)"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
