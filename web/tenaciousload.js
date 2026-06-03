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

async function doRefresh() {
    if (busy) {
        notify("warn", "A refresh is already running…");
        return;
    }
    busy = true;
    notify("info", "Refreshing models/LoRAs — scanning over the network, this can take a few minutes…", 10000);
    try {
        // 1) clear ComfyUI's folder cache + drop the object_info cache.
        await api.fetchApi("/tenaciousload/refresh", { method: "POST" });
        // 2) force a fresh object_info build and re-cache it (this is the slow step).
        await api.fetchApi("/object_info?nocache=1").then((r) => r.arrayBuffer());
        // 3) update open node dropdowns live if the frontend supports it.
        try { await app.refreshComboInNodes?.(); } catch (e) { /* non-fatal */ }
        notify("success", "Done — new models are now available (reload the page if a dropdown still looks stale).", 8000);
    } catch (e) {
        notify("error", "Refresh failed: " + (e?.message || e), 10000);
    } finally {
        busy = false;
    }
}

app.registerExtension({
    name: "Tenaciousload.Refresh",
    commands: [
        {
            id: "Tenaciousload.refresh",
            label: "🔄 Refresh Models / LoRAs",
            icon: "pi pi-refresh",
            function: doRefresh,
        },
    ],
    // Adds the command under the top "Extensions" menu (and the command palette).
    menuCommands: [
        {
            path: ["Extensions"],
            commands: ["Tenaciousload.refresh"],
        },
    ],
});
