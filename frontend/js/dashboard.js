/**
 * SiloVault Dashboard View Logic (ES module).
 *
 * Loads overview statistics and recent activity for the dashboard view.
 * PLACEHOLDER - Most backend endpoints return stub data at this stage.
 */

import { SiloVaultAPI } from "./api.js";

/**
 * Initialize the dashboard view with data from the backend.
 */
export async function initDashboard() {
    const statFiles = document.getElementById("stat-files");
    const statPending = document.getElementById("stat-pending");
    const statSync = document.getElementById("stat-sync");
    const statAI = document.getElementById("stat-ai");

    // Fetch system status (currently returns placeholder values).
    const systemStatus = await SiloVaultAPI.getSystemStatus();

    if (systemStatus && systemStatus.components) {
        const { database, ollama, pending_approvals_count, sync_service } =
            systemStatus.components;

        if (statAI) {
            statAI.textContent = ollama === "online" ? "Online" : "Offline";
        }
        if (statPending) {
            statPending.textContent = pending_approvals_count ?? 0;
        }
        if (statSync) {
            statSync.textContent = sync_service === "running" ? "Active" : "Idle";
        }
        if (statFiles) {
            statFiles.textContent = database === "connected" ? "N/A" : "0";
        }
    }
}