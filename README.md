<p align="center">
  <img src="assets/banner.svg" alt="ComfyUI-Tenaciousload" width="100%">
</p>

# ComfyUI-Tenaciousload

Self-contained fix for slow / black-screen ComfyUI loading when you have a huge
model/LoRA collection (especially on a network mount). **Just install the pack
and restart ComfyUI — no nginx, no docker, no extra port.**

## The problem
ComfyUI's `/api/object_info` enumerates every node's inputs. With thousands of
LoRAs (worse on a network mount) it becomes tens of MB and takes **minutes to
build on every page load** — and the build **freezes ComfyUI's whole event
loop**, so you get a long black screen, worst over a remote network.

## How this pack fixes it

<p align="center">
  <img src="assets/how-it-works.svg" alt="How it works: requests are served from an in-process cache in milliseconds; the slow build only runs on a miss or refresh" width="100%">
</p>

On load it injects an aiohttp **middleware** into ComfyUI that intercepts
`/object_info` and `/api/object_info` and:

- **caches the built response in memory _and_ on disk** (`./cache/`), so it is
  built once instead of on every load — and the disk copy makes **restarts
  instant** (no rebuild);
- **serves it gzipped in ~milliseconds** (≈85% smaller, independent of any CLI
  flag);
- serves from cache **without running the build**, so the event-loop freeze is
  gone for normal loads.

The only time a build runs is the first load after install, or when you
explicitly refresh (below).

## Refreshing after you add / remove models or LoRAs
The cache holds the old model lists until you refresh, so new files won't appear
until you do one of:

- **Menu:** `Extensions ▸ 🔄 Refresh Models / LoRAs` (also in the command palette).
- **Graph node:** `🔄 Refresh Models/LoRAs (Tenaciousload)` (for automated workflows).
- **HTTP:** `POST /tenaciousload/refresh`, then `GET /object_info?nocache=1`.

A refresh re-walks your model folders (slow over a network mount, ~minutes) — the
button shows a "refreshing…" toast meanwhile. Normal loads stay instant.

## Requirements
**None to install.** Only ComfyUI itself (tested on 0.23.0) and Python ≥ 3.8.
Everything used is Python stdlib or already bundled with ComfyUI (`aiohttp`,
`folder_paths`, `server`). The web button needs no npm packages.

## Install
Clone (or copy) this repo into your ComfyUI `custom_nodes/` folder and restart
ComfyUI:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/ethanfel/ComfyUI-Tenaciousload.git
# then restart ComfyUI
```
Nothing to `pip install`. ComfyUI-Manager can also install it from the registry.

## Verify it's working
After restart, load the page once (first time builds + caches), then:
```bash
curl -s -H 'Accept-Encoding: gzip' -o /dev/null \
  -w '%{time_total}s | %{size_download} bytes | %header{x-tenaciousload-cache} | %header{content-encoding}\n' \
  http://127.0.0.1:8188/api/object_info   # use your ComfyUI port
# expect after the first load: ~0.00Xs | ~10 MB | HIT | gzip
```
ComfyUI's startup log should show `Tenaciousload: object_info cache middleware installed`.

## Notes
- The disk cache lives in `./cache/` (git-ignored). Delete it, or use the refresh
  button, to force a rebuild.
- An nginx reverse proxy can cache `object_info` at the HTTP layer too, but this
  pack does it in-process so no extra service, container, or port is needed.

## License
MIT — see [LICENSE](LICENSE).
