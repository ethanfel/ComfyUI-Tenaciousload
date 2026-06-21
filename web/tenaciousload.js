import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

let busy = false;

function notify(severity, detail, life = 6000) {
    const toast = app.extensionManager?.toast;
    if (toast?.add) {
        toast.add({ severity, summary: "Tenaciousload", detail, life });
    } else if (severity === "error") {
        alert("Tenaciousload: " + detail);
    }
    console[severity === "error" ? "error" : "log"]("[Tenaciousload]", detail);
}

async function runRefresh(mode, extra) {
    if (busy) {
        notify("warn", "A refresh is already running…");
        return;
    }
    busy = true;
    const label =
        mode === "quick" ? "Quick refresh (changed folders)" :
        mode === "register" ? "Registering file(s)" : "Full refresh (rescan all)";
    notify("info", `${label} — rebuilding model lists… this can take a moment.`, 10000);
    try {
        // 1) run the chosen refresh mode on the server
        const res = await api.fetchApi("/tenaciousload/refresh", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode, ...(extra || {}) }),
        });
        const data = await res.json().catch(() => ({}));
        // 2) rebuild + re-cache object_info (the slow build, if any)
        await api.fetchApi("/object_info?nocache=1").then((r) => r.arrayBuffer());
        // 3) update open dropdowns live if supported
        try { await app.refreshComboInNodes?.(); } catch (e) { /* non-fatal */ }

        let detail = "Done — new models are available (reload the page if a dropdown still looks stale).";
        if (mode === "register") {
            detail = `Registered ${data.added || 0} file(s)` +
                (data.skipped ? `, ${data.skipped} not found on disk` : "") + ". " + detail;
        } else if (mode === "quick") {
            const dirs = (data.folders || []).reduce((a, f) => a + (f.scanned || 0), 0);
            detail = `Quick scan: ${dirs} folder(s) rescanned. ` + detail;
        }
        notify("success", detail, 8000);
    } catch (e) {
        notify("error", "Refresh failed: " + (e?.message || e), 10000);
    } finally {
        busy = false;
    }
}

async function doRegister() {
    const folder = (prompt("Model folder type (loras, checkpoints, vae, …):", "loras") || "").trim();
    if (!folder) return;
    const raw = prompt(
        `New file path(s) relative to the '${folder}' folder\n(comma- or newline-separated, e.g. mypack/newlora.safetensors):`,
        "",
    );
    if (!raw) return;
    const files = raw.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
    if (!files.length) return;
    await runRefresh("register", { folder, files });
}

// Skip the refresh buttons entirely when Tenaciousload is disabled.
let _tlEnabled = true;
try {
    const r = await api.fetchApi("/tenaciousload/status");
    if (r.ok) _tlEnabled = (await r.json()).enabled !== false;
} catch (e) { /* assume enabled */ }

if (_tlEnabled) app.registerExtension({
    name: "Tenaciousload.Refresh",
    commands: [
        {
            id: "Tenaciousload.quick",
            label: "⚡ Quick refresh (changed folders)",
            icon: "pi pi-bolt",
            function: () => runRefresh("quick"),
        },
        {
            id: "Tenaciousload.full",
            label: "🔄 Full refresh (rescan all models/LoRAs)",
            icon: "pi pi-refresh",
            function: () => runRefresh("full"),
        },
        {
            id: "Tenaciousload.register",
            label: "➕ Register new model file…",
            icon: "pi pi-plus",
            function: doRegister,
        },
    ],
    menuCommands: [
        {
            path: ["Extensions"],
            commands: ["Tenaciousload.quick", "Tenaciousload.full", "Tenaciousload.register"],
        },
    ],
});
