/**
 * SiloVault API Client (ES module).
 *
 * Handles all HTTP communication between the frontend PWA and the local
 * FastAPI backend. All requests are routed to the configured base URL.
 *
 * NOTE: Only the `/api/v1/health` endpoint is currently functional on the
 * backend. All other calls return placeholder responses.
 */

/**
 * Base URL for the local FastAPI backend.
 *
 * Defaults to the loopback address used by the backend configuration
 * (see `backend/config.py`: host 127.0.0.1, port 8000). It can be
 * overridden at runtime by setting `SiloVaultAPI.baseUrl`.
 */
const DEFAULT_BASE_URL = "http://127.0.0.1:8000";

export const SiloVaultAPI = {
    baseUrl: DEFAULT_BASE_URL,
    apiPrefix: "/api/v1",

    async request(path, options = {}) {
        const url = `${this.baseUrl}${this.apiPrefix}${path}`;
        try {
            const response = await fetch(url, {
                headers: {
                    "Content-Type": "application/json",
                    ...options.headers,
                },
                ...options,
            });
            if (!response.ok) {
                console.error(`API error [${response.status}]: ${url}`);
                return null;
            }
            return await response.json();
        } catch (error) {
            console.error(`Network error reaching ${url}:`, error);
            return null;
        }
    },

    async checkHealth() {
        return this.request("/v1/health");
    },

    async getSystemStatus() {
        return this.request("/v1/system/status");
    },

    async listFiles() {
        return this.request("/files");
    },

    async listUploads() {
        return this.request("/uploads");
    },

    async getSyncStatus() {
        return this.request("/sync/status");
    },

    async triggerSync() {
        return this.request("/sync/trigger", { method: "POST" });
    },

    async listPendingApprovals() {
        return this.request("/approvals/pending");
    },

    async listActivity(actionType = "", limit = 50, offset = 0) {
        const params = new URLSearchParams({
            limit: String(limit),
            offset: String(offset),
        });
        if (actionType) {
            params.set("action_type", actionType);
        }
        return this.request(`/activity?${params.toString()}`);
    },

    /**
     * Upload a single file via multipart/form-data.
     *
     * The Content-Type header is intentionally left unset so the browser
     * generates the multipart boundary itself.  Passing it manually would
     * produce an invalid request body.
     *
     * @param {File} file - The File object to upload.
     * @returns {object|null} Parsed JSON response on success, null on error.
     */
    async uploadFile(file) {
        const url = `${this.baseUrl}${this.apiPrefix}/uploads`;
        try {
            const formData = new FormData();
            // Field name must match the backend endpoint expectation.
            formData.append("file", file, file.name);

            const response = await fetch(url, {
                method: "POST",
                body: formData,
                // Do NOT set Content-Type; the browser must set it with the boundary.
            });

            if (!response.ok) {
                console.error(`Upload error [${response.status}]: ${url}`);
                return null;
            }
            return await response.json();
        } catch (error) {
            console.error(`Network error during upload to ${url}:`, error);
            return null;
        }
    },

    async processFile(fileId) {
        return this.request(`/ai/process/${fileId}`, { method: "POST" });
    },

    async getProcessingTask(taskId) {
        return this.request(`/ai/tasks/${taskId}`);
    },

    async submitApprovalAction(approvalId, action) {
        return this.request(`/approvals/${approvalId}/action`, {
            method: "POST",
            body: JSON.stringify({ action }),
        });
    },
};