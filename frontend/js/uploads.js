/**
 * SiloVault Uploads View Logic (ES module).
 *
 * Manages the upload zone interactions and upload queue display.
 * PLACEHOLDER - Real cloud uploads are not yet implemented. Files selected
 * here are NOT transmitted to Google Drive in this skeleton.
 */

export function initUploadsView() {
    const uploadZone = document.getElementById("upload-zone");
    const fileInput = document.getElementById("file-input");
    const uploadQueue = document.getElementById("upload-queue");
    const uploadBtn = document.getElementById("upload-btn");

    if (!fileInput) {
        return;
    }

    // The "+ Upload" button opens the file picker.
    if (uploadBtn) {
        uploadBtn.addEventListener("click", (event) => {
            event.preventDefault();
            fileInput.click();
        });
    }

    if (uploadZone) {
        uploadZone.addEventListener("click", () => fileInput.click());
    }

    fileInput.addEventListener("change", (event) => {
        const selectedFiles = Array.from(event.target.files || []);
        if (selectedFiles.length === 0) {
            return;
        }

        // Display selected files in the local queue (UI only).
        displayLocalQueue(selectedFiles, uploadQueue);

        // NOTE: No actual upload occurs in the skeleton stage.
        console.warn(
            "SiloVault skeleton: file upload transmission is not implemented.",
            selectedFiles
        );
    });
}

/**
 * Display selected files in the upload queue UI.
 * @param {Array<File>} files - Files selected by the user.
 * @param {HTMLElement} container - Target DOM element.
 */
function displayLocalQueue(files, container) {
    if (!container) return;
    container.innerHTML = "";

    files.forEach((file) => {
        const entry = document.createElement("div");
        entry.className = "queue-entry";

        const nameSpan = document.createElement("span");
        nameSpan.className = "file-name";
        nameSpan.textContent = file.name;

        const sizeSpan = document.createElement("span");
        sizeSpan.className = "file-size";
        sizeSpan.textContent = formatBytes(file.size);

        const statusSpan = document.createElement("span");
        statusSpan.className = "queue-status pending";
        statusSpan.textContent = "Queued (local only)";

        entry.appendChild(nameSpan);
        entry.appendChild(sizeSpan);
        entry.appendChild(statusSpan);
        container.appendChild(entry);
    });
}

/**
 * Format a byte count into a human-readable string.
 * @param {number} bytes - Size in bytes.
 * @returns {string} Formatted size.
 */
function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    const mb = kb / 1024;
    if (mb < 1024) return `${mb.toFixed(1)} MB`;
    return `${(mb / 1024).toFixed(2)} GB`;
}