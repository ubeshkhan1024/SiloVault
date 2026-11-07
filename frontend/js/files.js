/**
 * SiloVault Files View Logic (ES module).
 *
 * Loads and renders the file listing for the Files view.
 * PLACEHOLDER - The backend file index is not yet implemented.
 */

import { SiloVaultAPI } from "./api.js";

/**
 * Initialize the files view.
 */
export async function initFilesView() {
    const fileList = document.getElementById("file-list");

    const filesResponse = await SiloVaultAPI.listFiles();

    if (filesResponse && Array.isArray(filesResponse.files) && filesResponse.files.length > 0) {
        renderFileList(filesResponse.files, fileList);
    } else {
        if (fileList) {
            fileList.innerHTML = '<p class="placeholder-text">No indexed files yet.</p>';
        }
    }
}

/**
 * Render a list of file entries into the target container.
 * @param {Array<object>} files - File metadata entries.
 * @param {HTMLElement} container - Target DOM element.
 */
function renderFileList(files, container) {
    container.innerHTML = "";

    files.forEach((file) => {
        const row = document.createElement("div");
        row.className = "file-row";

        const nameDiv = document.createElement("div");
        nameDiv.className = "file-name";
        nameDiv.textContent = file.original_name ?? file.file_id;

        const metaDiv = document.createElement("div");
        metaDiv.className = "file-meta";
        metaDiv.textContent = file.category ?? "Uncategorized";

        row.appendChild(nameDiv);
        row.appendChild(metaDiv);
        container.appendChild(row);
    });
}