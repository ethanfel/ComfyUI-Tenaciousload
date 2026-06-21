import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Shows a small status line on ComfyUI's "Comfy" loading splash so a long
// object_info build (or cache load) isn't a silent black screen.

const START = performance.now();
let overlay = null;
let ready = false;
let lastDone = -1;
let stallTicks = 0;

function injectStyle() {
    if (document.getElementById("tl-loading-style")) return;
    const s = document.createElement("style");
    s.id = "tl-loading-style";
    s.textContent = `
@keyframes tl-indet { 0%{left:-35%;width:35%} 50%{left:40%;width:45%} 100%{left:100%;width:35%} }
#tl-loading{position:fixed;left:0;right:0;top:57%;margin:0 auto;z-index:99999;width:340px;
  text-align:center;font-family:system-ui,-apple-system,sans-serif;color:#a1a1aa;pointer-events:none;
  transition:opacity .35s ease}
#tl-loading .tl-msg{font-size:12.5px;letter-spacing:.3px;line-height:1.5}
#tl-loading .tl-sub{font-size:11px;color:#71717a;margin-top:2px}
#tl-loading .tl-track{position:relative;margin:11px auto 0;width:240px;height:4px;
  background:#27272a;border-radius:3px;overflow:hidden}
#tl-loading .tl-bar{position:absolute;top:0;height:100%;border-radius:3px;
  background:linear-gradient(90deg,#60a5fa,#a78bfa,#ff9cf9)}
#tl-loading .tl-bar.indet{animation:tl-indet 1.15s ease-in-out infinite}`;
    document.head.appendChild(s);
}

function ensureOverlay() {
    if (overlay || ready) return;
    injectStyle();
    overlay = document.createElement("div");
    overlay.id = "tl-loading";
    overlay.innerHTML =
        '<div class="tl-msg" id="tl-msg">Tenaciousload</div>' +
        '<div class="tl-sub" id="tl-sub"></div>' +
        '<div class="tl-track"><div class="tl-bar indet" id="tl-bar" style="left:0;width:35%"></div></div>';
    (document.body || document.documentElement).appendChild(overlay);
}

function fmt(s) {
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60);
    return m ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
}
const mb = (b) => (b / 1048576).toFixed(1) + " MB";

async function tick() {
    if (ready) return;
    if (!overlay && performance.now() - START > 700) ensureOverlay();
    if (!overlay) return;

    let st = null;
    try { st = await (await api.fetchApi("/tenaciousload/status")).json(); } catch (e) { /* ignore */ }
    const msg = overlay.querySelector("#tl-msg");
    const sub = overlay.querySelector("#tl-sub");
    const bar = overlay.querySelector("#tl-bar");
    if (!msg) return;

    if (st && st.building) {
        const pct = st.total ? Math.round((100 * st.done) / st.total) : 0;
        msg.textContent = `Building node definitions — ${st.done}/${st.total} (${pct}%)`;
        // detect a stall (the model-folder walk) and explain it
        stallTicks = st.done === lastDone ? stallTicks + 1 : 0;
        lastDone = st.done;
        sub.textContent = stallTicks >= 3
            ? `scanning model folders over the network… · ${fmt(st.elapsed || 0)}`
            : `first build / refresh · ${fmt(st.elapsed || 0)}`;
        bar.classList.add("indet");
        bar.style.width = "35%";
    } else if (st && st.cached) {
        // known fast path: serving the cached object_info
        msg.textContent = "Loading node definitions…";
        sub.textContent = `from cache · ${mb(st.gz_bytes || st.cache_bytes || 0)} gzipped`;
        bar.classList.remove("indet");
        bar.style.left = "0";
        bar.style.width = "90%";
    } else {
        // disabled, or server busy/frozen building natively — show "working"
        msg.textContent = "Loading node definitions…";
        sub.textContent = "";
        bar.classList.add("indet");
        bar.style.width = "35%";
    }
}

const poll = setInterval(tick, 600);
tick();

app.registerExtension({
    name: "Tenaciousload.LoadingStatus",
    // setup() fires once the app is ready (object_info loaded, nodes registered).
    async setup() {
        ready = true;
        clearInterval(poll);
        if (overlay) {
            overlay.style.opacity = "0";
            setTimeout(() => overlay && overlay.remove(), 350);
        }
    },
});

// hard safety: never let the overlay linger
setTimeout(() => { if (overlay && !ready) { clearInterval(poll); overlay.remove(); } }, 15 * 60 * 1000);
