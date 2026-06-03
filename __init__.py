"""
ComfyUI-Tenaciousload
=====================
Self-contained fix for slow / black-screen ComfyUI loading when you have a huge
model/LoRA collection (especially on a network mount).

The cause: /api/object_info is tens of MB and takes minutes to build on EVERY
page load (it re-walks the model folders), and the build blocks ComfyUI's event
loop the whole time. This pack installs an in-process caching layer that:

  * caches the built object_info in memory AND on disk (so it survives restarts),
  * serves it gzipped in ~milliseconds instead of rebuilding every load,
  * exposes a one-click "Refresh Models / LoRAs" button + graph node to rebuild
    the cache after you add/remove models.

No nginx, no docker, no extra port. Just install the pack and restart ComfyUI.
"""

import os
import gzip
import json
import logging
import threading

from aiohttp import web

import folder_paths
from server import PromptServer

log = logging.getLogger("Tenaciousload")

WEB_DIRECTORY = "./web"

# --- cache storage --------------------------------------------------------------
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_RAW_PATH = os.path.join(_CACHE_DIR, "object_info.json")
_GZ_PATH = os.path.join(_CACHE_DIR, "object_info.json.gz")
_OBJECT_INFO_PATHS = ("/object_info", "/api/object_info")
_GZIP_LEVEL = 5

_lock = threading.Lock()
_mem = {"raw": None, "gz": None}   # bytes / bytes
_disk_loaded = False


def _load_from_disk():
    """Populate the in-memory cache from disk once (cheap, makes restarts instant)."""
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
    """Cache a freshly built object_info body (memory + disk)."""
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
    """Drop the cached object_info so the next request rebuilds it."""
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
    """Force ComfyUI to re-scan every model/LoRA folder on the next build."""
    try:
        folder_paths.filename_list_cache.clear()
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: could not clear filename_list_cache: %s", e)
    try:
        folder_paths.cache_helper.clear()
    except Exception as e:  # pragma: no cover
        log.warning("Tenaciousload: could not clear cache_helper: %s", e)


def _serve_cached(request):
    raw = _mem["raw"]
    gz = _mem["gz"]
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "")
    if accepts_gzip and gz is not None:
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
    # Only touch the full object_info list (not /object_info/{node_class}).
    if request.method != "GET" or request.path not in _OBJECT_INFO_PATHS:
        return await handler(request)

    global _disk_loaded
    if not _disk_loaded:
        _load_from_disk()

    nocache = "nocache" in request.query
    if not nocache and _mem["raw"] is not None:
        return _serve_cached(request)

    # MISS (or forced refresh): build via the real handler, then cache the body.
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
        app = PromptServer.instance.app
        app.middlewares.insert(0, _object_info_cache_mw)
        log.info("Tenaciousload: object_info cache middleware installed")
    except Exception as e:
        log.error("Tenaciousload: could not install cache middleware (loads will be slow): %s", e)


_install_middleware()


# --- refresh API ----------------------------------------------------------------
@PromptServer.instance.routes.post("/tenaciousload/refresh")
async def _refresh(request):
    clear_comfy_model_cache()
    invalidate_object_info_cache()
    log.info("Tenaciousload: caches cleared via refresh endpoint")
    return web.json_response({"status": "ok"})


# --- optional graph node (for workflow automation) ------------------------------
class _AnyType(str):
    def __ne__(self, other):
        return False


ANY = _AnyType("*")


class TenaciousloadRefresh:
    @classmethod
    def INPUT_TYPES(cls):
        return {"optional": {"trigger": (ANY, {})}}

    RETURN_TYPES = (ANY,)
    RETURN_NAMES = ("trigger",)
    FUNCTION = "refresh"
    CATEGORY = "Tenaciousload"
    OUTPUT_NODE = True
    DESCRIPTION = "Clear ComfyUI's model/LoRA cache and invalidate the object_info cache."

    def refresh(self, trigger=None):
        clear_comfy_model_cache()
        invalidate_object_info_cache()
        return (trigger,)


NODE_CLASS_MAPPINGS = {"TenaciousloadRefresh": TenaciousloadRefresh}
NODE_DISPLAY_NAME_MAPPINGS = {"TenaciousloadRefresh": "🔄 Refresh Models/LoRAs (Tenaciousload)"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
