/**
 * SiloVault Main Application Controller (ES module entry point).
 *
 * Manages view routing, sidebar navigation, view initialization, and the
 * initial health check against the local FastAPI backend.
 */

import { SiloVaultAPI } from "./api.js";
import { initDashboard } from "./dashboard.js";
import { initFilesView } from "./files.js";
import { initUploadsView } from "./uploads.js";
import { initSettingsView } from "./settings.js";

/** Views that have already been initialized (prevents duplicate init). */
const initializedViews = new Set();

/**
 * Initialize a view's logic exactly once.
 * @param {string} viewName - The target view identifier.
 */
function ensureViewInitialized(viewName) {
    // The Files view must always re-fetch when revisited so that newly
    // uploaded or indexed records appear without a page reload.
    if (viewName === "files") {
        initFilesView();
        return;
    }

    if (initializedViews.has(viewName)) {
        return;
    }

    switch (viewName) {
        case "dashboard":
            initDashboard();
            break;
        case "uploads":
            initUploadsView();
            break;
        case "settings":
            initSettingsView();
            break;
        default:
            // "sync" and "activity" are static placeholders for now.
            break;
    }

    initializedViews.add(viewName);
}

document.addEventListener("DOMContentLoaded", () => {
    // Register the service worker for PWA support.
    if ("serviceWorker" in navigator) {
        navigator.serviceWorker
            .register("./service-worker.js")
            .then(() => console.log("Service worker registered."))
            .catch((err) => console.warn("Service worker registration failed:", err));
    }

    // Initialize navigation handlers.
    initNavigation();

    // Initialize the default (dashboard) view.
    ensureViewInitialized("dashboard");

    // Perform the initial backend health check.
    checkBackendHealth();
});

/**
 * Wire up sidebar navigation clicks to switch views.
 */
function initNavigation() {
    const navItems = document.querySelectorAll(".nav-item");

    navItems.forEach((item) => {
        item.addEventListener("click", (event) => {
            event.preventDefault();
            const viewName = item.dataset.view;
            switchView(viewName);
        });
    });
}

/**
 * Switch the visible view section.
 * @param {string} viewName - The target view identifier.
 */
function switchView(viewName) {
    // Hide all views and deactivate nav items.
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));

    // Show the target view and mark its nav item active.
    const targetView = document.getElementById(`view-${viewName}`);
    const targetNav = document.querySelector(`.nav-item[data-view="${viewName}"]`);

    if (targetView) {
        targetView.classList.add("active");
    }
    if (targetNav) {
        targetNav.classList.add("active");
    }

    // Lazy-initialize view logic on first visit.
    ensureViewInitialized(viewName);
}

/**
 * Check the local backend health and update the sidebar status indicator.
 */
async function checkBackendHealth() {
    const statusDot = document.querySelector(".status-dot");
    const statusText = document.querySelector(".status-text");

    const health = await SiloVaultAPI.checkHealth();

    if (health && health.status === "healthy") {
        statusDot.classList.remove("offline");
        statusDot.classList.add("online");
        statusText.textContent = "Local Node Online";
    } else {
        statusDot.classList.remove("online");
        statusDot.classList.add("offline");
        statusText.textContent = "Local Node Offline";
    }
}