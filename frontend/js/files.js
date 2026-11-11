/**
 * SiloVault Files View Logic (ES module).
 *
 * Loads and renders the file listing from the backend endpoint
 * ``GET /api/v1/files``. Re-fetches data every time the view is shown so
 * that newly uploaded or indexed files appear without a page reload.
 */

import { SiloVaultAPI } from "./api.js";

// Sync-state display labels matching the backend enum values.
const SYNC_LABELS = {
    LOCAL: "Local",
    PENDING: "Pending",
    COMPLETED: "Synced",
    FAILED: "Failed",
};

/**
 * Initialize the files view. Called whenever the Files tab is activated;
 * always performs a fresh fetch rather than caching the result.
 */
export async function initFilesView() {
    const fileList = document.getElementById("file-list");
    if (!fileList) {
        return;
    }

    // Show a loading state immediately.
    fileList.innerHTML = '<p class="placeholder-text">Loading files…</p>';

    try {
        const response = await SiloVaultAPI.listFiles();

        // API returned an error / unreachable backend → null.
        if (response === null) {
            renderError(fileList);
            return;
        }

        const items = Array.isArray(response.items) ? response.items : [];

        if (items.length === 0) {
            fileList.innerHTML =
                '<p class="placeholder-text">No indexed files yet.</p>';
            return;
        }

        renderFileList(items, fileList);
    } catch (error) {
        console.error("Failed to load files:", error);
        renderError(fileList);
    }
}

/**
 * Render an error message into the target container.
 * @param {HTMLElement} container
 */
function renderError(container) {
    container.innerHTML = "";
    const para = document.createElement("p");
    para.className = "placeholder-text";
    para.textContent = "Could not load files. Is the backend running?";
    container.appendChild(para);
}

/**
 * Render a list of file entries into the target container using safe DOM APIs.
 * Each row shows: file name, provider + MIME type + size, and a sync-state badge.
 *
 * @param {Array<object>} files - File metadata entries from the backend.
 * @param {HTMLElement} container - Target DOM element (#file-list).
 */
function renderFileList(files, container) {
    // Clear any existing content (including the loading placeholder).
    container.innerHTML = "";

    files.forEach((file) => {
        const row = document.createElement("div");
        row.className = "file-row";

        // Left side: file name (primary) + sub-info row
        const infoCol = document.createElement("div");
        infoCol.className = "file-info";

        const nameEl = document.createElement("div");
        nameEl.className = "file-name";
        nameEl.textContent = file.name || "(unknown)";

        const subInfoEl = document.createElement("div");
        subInfoEl.className = "file-subinfo";
        subInfoEl.textContent = [
            file.provider ? `Provider: ${file.provider}` : null,
            file.mime_type || null,
            formatFileSize(file.size_bytes),
        ]
            .filter(Boolean)
            .join("  ·  ");

        infoCol.appendChild(nameEl);
        infoCol.appendChild(subInfoEl);

        // Right side: sync-state badge + optional download action
        const metaCol = document.createElement("div");
        metaCol.className = "file-meta";

        const badge = document.createElement("span");
        badge.className = buildBadgeClass(file.sync_state);
        badge.textContent = SYNC_LABELS[file.sync_state] || file.sync_state || "\u2014";

        metaCol.appendChild(badge);

        // Show a download button only for locally stored files.
        if (file.provider === "LOCAL") {
            const dlBtn = document.createElement("button");
            dlBtn.className = "btn-download";
            dlBtn.title = `Download ${file.name}`;
            dlBtn.setAttribute("aria-label", `Download ${file.name}`);
            dlBtn.textContent = "Download";
            dlBtn.addEventListener("click", () => handleDownload(file, dlBtn));
            metaCol.appendChild(dlBtn);

            // Delete button — shows only for LOCAL records after the download btn.
            const delBtn = document.createElement("button");
            delBtn.className = "btn-delete";
            delBtn.title = `Delete ${file.name}`;
            delBtn.setAttribute("aria-label", `Delete ${file.name}`);
            delBtn.textContent = "Delete";
            delBtn.addEventListener("click", () => handleDelete(file, delBtn));
            metaCol.appendChild(delBtn);
        }

        row.appendChild(infoCol);
        row.appendChild(metaCol);
        container.appendChild(row);
    });
}

/**
 * Trigger a browser download for a LOCAL file record.
 *
 * The file is fetched as a Blob and saved via an object URL so the browser
 * honours the original filename even though the backend is on a different
 * origin (127.0.0.1:8000 vs the static host on :3000).
 *
 * @param {object} file - File metadata from the backend.
 * @param {HTMLButtonElement} [btn] - Optional button to show loading state on.
 */
async function handleDownload(file, btn) {
    const originalLabel = btn ? btn.textContent : null;
    if (btn) {
        btn.disabled = true;
        btn.textContent = "Downloading…";
    }

    try {
        const url = `${SiloVaultAPI.baseUrl}${SiloVaultAPI.apiPrefix}/files/${file.id}/download`;
        const response = await fetch(url);

        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const body = await response.json();
                if (body && body.detail) {
                    detail = body.detail;
                }
            } catch {
                // Non-JSON error body; keep the HTTP status text.
            }
            console.error(`Download failed for "${file.name}": ${detail}`);
            if (btn) {
                btn.textContent = "Failed";
                btn.classList.add("btn-download-error");
                setTimeout(() => {
                    btn.classList.remove("btn-download-error");
                    btn.textContent = originalLabel;
                }, 1500);
            }
            return;
        }

        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = file.name || "download";
        link.style.display = "none";
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(objectUrl);

        // Refresh the dashboard Recent Activity panel so the download appears there.
        if (typeof window !== "undefined") {
            import("./dashboard.js").then((mod) => mod.refreshRecentActivity()).catch(() => {});
        }
    } catch (error) {
        console.error(`Download failed for "${file.name}":`, error);
        if (btn) {
            btn.textContent = "Failed";
            btn.classList.add("btn-download-error");
            setTimeout(() => {
                btn.classList.remove("btn-download-error");
                btn.textContent = originalLabel;
            }, 1500);
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            if (!btn.classList.contains("btn-download-error")) {
                btn.textContent = originalLabel;
            }
        }
    }
}

/**
 * Delete a LOCAL file record with explicit user confirmation.
 *
 * Shows a native confirm dialog identifying the original filename.
 * On success the Files view is refreshed immediately. On failure the row
 * remains visible and the button is reset so the user can retry.
 *
 * @param {object} file - File metadata from the backend.
 * @param {HTMLButtonElement} btn - The delete button element.
 */
async function handleDelete(file, btn) {
    const originalLabel = btn.textContent;

    // Require explicit confirmation; identify the file by its original name.
    const confirmed = window.confirm(
        `Are you sure you want to delete "${file.name}"?\n\n` +
        `This action cannot be undone.`
    );
    if (!confirmed) {
        return;
    }

    btn.disabled = true;
    btn.textContent = "Deleting…";

    try {
        const url = `${SiloVaultAPI.baseUrl}${SiloVaultAPI.apiPrefix}/files/${file.id}`;
        const response = await fetch(url, { method: "DELETE" });

        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const body = await response.json();
                if (body && body.detail) {
                    detail = body.detail;
                }
            } catch {
                // Non-JSON error body; keep the HTTP status text.
            }
            console.error(`Delete failed for "${file.name}": ${detail}`);
            alert(`Could not delete "${file.name}".\n\n${detail}`);
        } else {
            // Success — remove the row from the DOM.
            const row = btn.closest(".file-row");
            if (row) {
                row.remove();
            }
            // Refresh both the Files list and the Dashboard Recent Activity panel.
            initFilesView();
            if (typeof window !== "undefined") {
                import("./dashboard.js").then((mod) => mod.refreshRecentActivity()).catch(() => {});
            }
        }
    } catch (error) {
        console.error(`Delete request failed for "${file.name}":`, error);
        alert(`Network error while deleting "${file.name}".`);
    } finally {
        btn.disabled = false;
        btn.textContent = originalLabel;
    }
}

/**
 * Build a CSS class string for a sync-state badge.
 * @param {string} syncState
 * @returns {string}
 */
function buildBadgeClass(syncState) {
    switch (syncState) {
        case "COMPLETED":
            return "sync-badge sync-completed";
        case "FAILED":
            return "sync-badge sync-failed";
        case "PENDING":
        case "LOCAL":
        default:
            return "sync-badge sync-pending";
    }
}

/**
 * Format a byte count into a human-readable string (e.g. "1.5 MB").
 * Mirrors the helper already present in uploads.js.
 * @param {number} bytes
 * @returns {string}
 */
function formatFileSize(bytes) {
    if (bytes == null) return "\u2014";
    if (bytes < 1024) return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    const mb = kb / 1024;
    if (mb < 1024) return `${mb.toFixed(1)} MB`;
    return `${(mb / 1024).toFixed(2)} GB`;
}
