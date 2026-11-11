/**
 * SiloVault Dashboard View Logic (ES module).
 *
 * Loads overview statistics and recent activity for the dashboard view.
 * PLACEHOLDER - Most backend endpoints return stub data at this stage.
 */

import { SiloVaultAPI } from "./api.js";
import { _createActivityRow } from "./activity.js";

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

    // Populate Recent Activity panel with the latest 5 records.
    await _loadRecentActivity();
}

/**
 * Public alias for _loadRecentActivity — exported so that other modules
 * (uploads.js, files.js) can refresh the dashboard panel after an action
 * completes without needing a full view re-navigation.
 */
export async function refreshRecentActivity() {
    return _loadRecentActivity();
}

async function _loadRecentActivity() {
    const container = document.getElementById("recent-activity");
    if (!container) return;

    // Clear stale content / placeholder before any fetch begins.
    container.innerHTML = "";

    try {
        const response = await SiloVaultAPI.listActivity("", 5, 0);

        if (response === null) {
            // Network/API error — leave the empty container so the user
            // can still interact with the rest of the dashboard.
            console.warn("Dashboard: failed to load recent activity");
            return;
        }

        const items = Array.isArray(response.items) ? response.items : [];

        if (items.length === 0) {
            const para = document.createElement("p");
            para.className = "placeholder-text";
            para.textContent = "No recent activity. Upload files to begin.";
            container.appendChild(para);
            return;
        }

        items.forEach((activity) => {
            const row = _createActivityRow(activity);
            container.appendChild(row);
        });
    } catch (error) {
        console.error("Dashboard: unexpected error loading recent activity:", error);
    }
}
