/**
 * SiloVault Activity History View Logic (ES module).
 *
 * Fetches and renders the full activity history from the backend endpoint
 * ``GET /api/v1/activity``. Always refreshes when the view is opened so that
 * newly recorded operations appear without a page reload. Supports filtering
 * by action type, infinite-scroll-style load-more, and shows loading/empty/
 * error states.
 */

import { SiloVaultAPI } from "./api.js";

// Internal state for pagination and current filter.
let _currentOffset = 0;
let _currentFilter = "";
let _isLoading = false;
let _hasMore = true;

// Page size used for every request; matched to the backend's default.
const PAGE_SIZE = 50;

// Glyphs used as visual indicators per action type. Mirrors the nav-icon
// palette already present in the sidebar.
const ACTION_GLYPHS = {
    UPLOAD: "\u2191", // ↑
    DOWNLOAD: "\u2193", // ↓
    DELETE: "\u2715", // ✕
};

/**
 * Initialise the activity-history view. Called whenever the Activity tab is
 * activated; always performs a fresh fetch rather than caching the result.
 */
export async function initActivityView() {
    // Reset pagination state on each open so the user always sees the latest
    // entries first.
    _currentOffset = 0;
    _hasMore = true;

    const container = document.getElementById("activity-log");
    if (!container) {
        return;
    }

    // Clear any stale content and show a loading placeholder.
    container.innerHTML = '<p class="placeholder-text">Loading activity…</p>';
    _hidePagination();

    try {
        const response = await SiloVaultAPI.listActivity(
            _currentFilter,
            PAGE_SIZE,
            _currentOffset,
        );

        if (response === null) {
            _renderError(container);
            return;
        }

        const items = Array.isArray(response.items) ? response.items : [];
        _hasMore = _currentOffset + items.length < response.total;

        if (_hasMore && typeof window !== "undefined") {
            _showLoadMore(container);
        } else {
            _hidePagination();
        }

        if (items.length === 0) {
            container.innerHTML =
                '<p class="placeholder-text">No activity records yet.</p>';
            return;
        }

        _renderItems(items, container);
    } catch (error) {
        console.error("Failed to load activity:", error);
        _renderError(container);
    }
}

/**
 * Render the loaded activity entries into the target container. Appends them
 * below any existing rows so that "load more" accumulates correctly.
 * @param {Array<object>} items
 * @param {HTMLElement} container
 */
function _renderItems(items, container) {
    items.forEach((activity) => {
        const row = _createActivityRow(activity);
        container.appendChild(row);
    });
}

/**
 * Build a single activity-row DOM element using safe text-content APIs.
 * Never inserts raw HTML to prevent XSS.
 * @param {object} activity
 * @returns {HTMLElement}
 */
export function _createActivityRow(activity) {
    const row = document.createElement("div");
    row.className = "activity-item";

    // Left: icon circle.
    const icon = document.createElement("div");
    icon.className = `activity-icon action-${_normaliseActionType(
        activity.action_type,
    )}`;
    icon.textContent =
        ACTION_GLYPHS[activity.action_type] || activity.action_type.charAt(0);
    icon.setAttribute("aria-label", activity.action_type);

    // Middle: descriptive text + metadata line.
    const info = document.createElement("div");
    info.className = "activity-info";

    const main = document.createElement("div");
    main.className = "activity-main";
    main.textContent = _formatMainLine(activity);

    const sub = document.createElement("div");
    sub.className = "activity-sub";
    sub.textContent = _formatSubLine(activity);

    info.appendChild(main);
    info.appendChild(sub);

    // Right: human-readable timestamp.
    const time = document.createElement("div");
    time.className = "activity-time";
    time.textContent = _formatTimestamp(activity.timestamp);

    row.appendChild(icon);
    row.appendChild(info);
    row.appendChild(time);

    return row;
}

/**
 * Format the primary one-line description shown in the timeline row.
 * @param {object} activity
 * @returns {string}
 */
function _formatMainLine(activity) {
    const actionLabel = _actionVerb(activity.action_type);
    return `${actionLabel} “${activity.file_name}”`;
}

/**
 * Format the secondary metadata line shown beneath the primary text.
 * @param {object} activity
 * @returns {string}
 */
function _formatSubLine(activity) {
    const parts = [activity.provider];
    if (activity.details?.size_bytes != null) {
        parts.push(formatBytes(activity.details.size_bytes));
    }
    if (activity.status) {
        parts.push(activity.status.toLowerCase());
    }
    return parts.join("  ·  ");
}

/**
 * Normalise an action type string into a CSS-class-safe token. Falls back to
 * upper-case with non-alpha characters replaced by underscores.
 * @param {string} type
 * @returns {string}
 */
function _normaliseActionType(type) {
    return (type || "").toUpperCase().replace(/[^A-Z0-9]/g, "_");
}

/**
 * Map an action-type enum value to its human-friendly verb.
 * @param {string} type
 * @returns {string}
 */
function _actionVerb(type) {
    switch (type?.toUpperCase()) {
        case "UPLOAD":
            return "Uploaded";
        case "DOWNLOAD":
            return "Downloaded";
        case "DELETE":
            return "Deleted";
        default:
            return `${type ?? "?"}`;
    }
}

/**
 * Format a UTC ISO timestamp string into a local human-readable form.
 * Uses Intl.DateTimeFormat so the output respects the browser locale.
 * @param {string} iso
 * @returns {string}
 */
function _formatTimestamp(iso) {
    if (!iso) return "";
    try {
        const date = new Date(iso);
        if (Number.isNaN(date.getTime())) return iso;
        return new Intl.DateTimeFormat(navigator.language ?? "en-US", {
            month: "short",
            day: "numeric",
            year: "numeric",
            hour: "numeric",
            minute: "2-digit",
            hour12: true,
        }).format(date);
    } catch {
        return iso;
    }
}

/**
 * Render an error message into the target container.
 * @param {HTMLElement} container
 */
function _renderError(container) {
    container.innerHTML = "";
    const para = document.createElement("p");
    para.className = "placeholder-text";
    para.style.color = "var(--danger)";
    para.textContent =
        "Could not load activity. Is the backend running?";
    container.appendChild(para);
}

/**
 * Show a "load more" button at the bottom of the container.
 * @param {HTMLElement} container
 */
function _showLoadMore(container) {
    _hidePagination();
    const btn = document.createElement("button");
    btn.className = "btn-load-more";
    btn.textContent = "Load more";
    btn.addEventListener("click", () => {
        if (_isLoading) return;
        _isLoading = true;
        btn.disabled = true;
        btn.textContent = "Loading…";

        const offset = _currentOffset + PAGE_SIZE;
        void SiloVaultAPI.listActivity(_currentFilter, PAGE_SIZE, offset)
            .then((response) => {
                if (response === null) {
                    _renderError(container);
                    return;
                }
                const items = Array.isArray(response.items)
                    ? response.items
                    : [];
                _currentOffset = offset;
                _hasMore = _currentOffset + items.length < response.total;
                _renderItems(items, container);
                if (_hasMore) {
                    _showLoadMore(container);
                } else {
                    _hidePagination();
                }
            })
            .catch((err) => {
                console.error("Load-more failed:", err);
                _renderError(container);
            })
            .finally(() => {
                _isLoading = false;
            });
    });
    container.appendChild(btn);
}

/**
 * Hide any previously rendered pagination controls.
 */
function _hidePagination() {
    const existing = document.querySelector(".btn-load-more");
    if (existing) {
        existing.remove();
    }
}

/**
 * Helper copied from uploads.js to format byte counts consistently across views.
 * @param {number} bytes
 * @returns {string}
 */
function formatBytes(bytes) {
    if (bytes == null) return "\u2014";
    if (bytes < 1024) return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    const mb = kb / 1024;
    if (mb < 1024) return `${mb.toFixed(1)} MB`;
    return `${(mb / 1024).toFixed(2)} GB`;
}
