/**
 * SiloVault Uploads View Logic (ES module).
 *
 * Manages the upload zone interactions, queues selected/dropped files,
 * sends each one to ``POST /api/v1/uploads`` via multipart/form-data,
 * and reports per-file progress through status badges.
 */

import { SiloVaultAPI } from "./api.js";

// Tracks whether ANY file is currently in-flight; blocks new submissions
// while an upload is running to avoid overwhelming the local backend.
let _isUploading = false;

export function initUploadsView() {
    const uploadZone = document.getElementById("upload-zone");
    const fileInput = document.getElementById("file-input");
    const uploadQueue = document.getElementById("upload-queue");
    const uploadBtn = document.getElementById("upload-btn");

    if (!fileInput) {
        return;
    }

    // The "+ Upload" topbar button opens the file picker.
    if (uploadBtn) {
        uploadBtn.addEventListener("click", (event) => {
            event.preventDefault();
            fileInput.click();
        });
    }

    // Clicking the drop zone itself also opens the picker.
    if (uploadZone) {
        uploadZone.addEventListener("click", () => fileInput.click());
    }

    // File picker selection.
    fileInput.addEventListener("change", (event) => {
        const selectedFiles = Array.from(event.target.files || []);
        if (selectedFiles.length === 0) {
            return;
        }
        void processSelectedFiles(selectedFiles, uploadQueue);
        // Reset so the same file can be re-selected later.
        fileInput.value = "";
    });

    // Drag-and-drop onto the upload zone.
    if (uploadZone) {
        uploadZone.addEventListener("dragover", (event) => {
            event.preventDefault();
            event.stopPropagation();
            uploadZone.style.borderColor = "var(--accent)";
            uploadZone.style.backgroundColor = "var(--bg-hover)";
        });

        uploadZone.addEventListener("dragleave", (event) => {
            event.preventDefault();
            event.stopPropagation();
            uploadZone.style.borderColor = "";
            uploadZone.style.backgroundColor = "";
        });

        uploadZone.addEventListener("drop", (event) => {
            event.preventDefault();
            event.stopPropagation();
            uploadZone.style.borderColor = "";
            uploadZone.style.backgroundColor = "";

            const droppedFiles = Array.from(event.dataTransfer.files || []);
            if (droppedFiles.length === 0) {
                return;
            }
            void processSelectedFiles(droppedFiles, uploadQueue);
        });
    }
}

/**
 * Upload a batch of files to the backend, one at a time.
 * Shows per-file status badges in the queue container.
 * @param {Array<File>} files
 * @param {HTMLElement} container
 */
async function processSelectedFiles(files, container) {
    if (_isUploading) {
        appendErrorEntry(container, "Please wait for the current upload(s) to finish.");
        return;
    }

    _isUploading = true;
    container.innerHTML = "";

    for (const file of files) {
        const entry = createUploadEntry(file.name, formatBytes(file.size));
        container.appendChild(entry);
        await uploadSingleFile(file, entry);
    }

    _isUploading = false;

    // Re-trigger the Files view refresh so newly uploaded records appear.
    // We dispatch a custom event that app.js can listen for, but since
    // initFilesView() is already exported we call it directly here.
    if (typeof window !== "undefined") {
        // Lazy-import to avoid circular init issues; caller owns ensureViewInitialized.
        import("./files.js").then((mod) => {
            mod.initFilesView();
        }).catch(() => {
            // Files view refresh failure is non-fatal.
        });
    }
}

/**
 * Send a single File object to the backend and update its UI entry.
 * @param {File} file
 * @param {HTMLElement} entry - The DOM element representing this file's queue row.
 */
async function uploadSingleFile(file, entry) {
    setStatus(entry, "uploading", "Uploading…");

    try {
        const result = await SiloVaultAPI.uploadFile(file);

        if (result === null) {
            setStatus(entry, "failed", "Failed (network error)");
            return;
        }

        if (!result.id) {
            setStatus(entry, "failed", "Failed (invalid response)");
            return;
        }

        // Build a concise confirmation line from the backend metadata.
        const sizeStr = formatBytes(result.size_bytes || file.size);
        const nameEl = entry.querySelector(".file-name");
        const metaEl = entry.querySelector(".file-size");
        if (nameEl) {
            nameEl.textContent = result.name || file.name;
        }
        if (metaEl) {
            metaEl.textContent = `${sizeStr}  ·  ${result.provider || "LOCAL"}`;
        }
        setStatus(entry, "success", "Uploaded");
    } catch (error) {
        console.error(`Upload failed for "${file.name}":`, error);
        setStatus(entry, "failed", "Failed");
    }
}

/**
 * Create a new queue-entry DOM element showing file name and size.
 * @param {string} fileName
 * @param {string} fileSizeStr
 * @returns {HTMLElement}
 */
function createUploadEntry(fileName, fileSizeStr) {
    const entry = document.createElement("div");
    entry.className = "queue-entry";

    const nameSpan = document.createElement("span");
    nameSpan.className = "file-name";
    nameSpan.textContent = fileName;

    const sizeSpan = document.createElement("span");
    sizeSpan.className = "file-size";
    sizeSpan.textContent = fileSizeStr;

    const statusSpan = document.createElement("span");
    statusSpan.className = "queue-status pending";
    statusSpan.textContent = "Waiting";

    entry.appendChild(nameSpan);
    entry.appendChild(sizeSpan);
    entry.appendChild(statusSpan);
    return entry;
}

/**
 * Update the status badge class and label text on a queue entry.
 * @param {HTMLElement} entry
 * @param {string} type - One of: "pending", "uploading", "success", "failed"
 * @param {string} label
 */
function setStatus(entry, type, label) {
    const statusSpan = entry.querySelector(".queue-status");
    if (!statusSpan) return;

    // Remove any previous status classes.
    statusSpan.classList.remove(
        "pending",
        "uploading",
        "success",
        "failed"
    );
    statusSpan.classList.add(type);
    statusSpan.textContent = label;
}

/**
 * Append an error note to the queue container.
 * @param {HTMLElement} container
 * @param {string} message
 */
function appendErrorEntry(container, message) {
    const para = document.createElement("p");
    para.className = "placeholder-text";
    para.style.color = "var(--danger)";
    para.style.marginTop = "0.5rem";
    para.textContent = message;
    container.appendChild(para);
}

/**
 * Format a byte count into a human-readable string.
 * @param {number} bytes
 * @returns {string}
 */
function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    const mb = kb / 1024;
    if (mb < 1024) return `${mb.toFixed(1)} MB`;
    return `${(mb / 1024).toFixed(2)} GB`;
}
