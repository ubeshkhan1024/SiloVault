# SiloVault — Proposed API Contracts & Data Specifications

This document defines the proposed communication contracts, data formatting rules,
error handling standards, and integration protocols for **SiloVault**.

For overall architecture, see [ARCHITECTURE.md](ARCHITECTURE.md). For data movement, see [DATA_FLOW.md](DATA_FLOW.md). For security guidelines, see [SECURITY.md](SECURITY.md).

> [!IMPORTANT]
> **Status:** All endpoints, JSON schemas, and API groups described in this document are **Proposed Specifications**. **14 API routes are registered** in the FastAPI backend under `/api/v1/`, but **only `GET /api/v1/health` is functional**; the remaining 13 endpoints return placeholder responses with no business logic. No databases or live network listeners exist yet.
>
> ## Implementation Status (Foundation Skeleton)
>
> ### Functional Endpoint
> - **`GET /api/v1/health`** — Returns `{"status": "healthy", "app": "SiloVault", "version": "0.1.0-skeleton", "timestamp": "..."}`. Extra `app` field not in contract.
>
> ### Placeholder Endpoints (13 of 14)
> All other endpoints return `{"status": "placeholder", "message": "... not yet implemented.", ...}` with minimal fields. **These responses are temporary and do not match the target response schemas documented below.**
>
> | Endpoint | Placeholder Response Structure |
> |----------|--------------------------------|
> | `GET /api/v1/system/status` | `{"status": "placeholder", "message": "...", "components": {...}}` |
> | `GET /api/v1/files` | `{"status": "placeholder", "message": "...", "files": []}` |
> | `GET /api/v1/files/{file_id}` | `{"status": "placeholder", "message": "...", "file_id": "..."}` |
> | `POST /api/v1/uploads` | `{"status": "placeholder", "message": "..."}` |
> | `GET /api/v1/uploads` | `{"status": "placeholder", "message": "...", "uploads": []}` |
> | `GET /api/v1/sync/status` | `{"status": "placeholder", "message": "...", "sync_service": "...", "pending_files": 0, "last_sync_at": null}` |
> | `POST /api/v1/sync/trigger` | `{"status": "placeholder", "message": "..."}` |
> | `POST /api/v1/ai/process/{file_id}` | `{"status": "placeholder", "message": "...", "file_id": "..."}` |
> | `GET /api/v1/ai/tasks/{task_id}` | `{"status": "placeholder", "message": "...", "task_id": "..."}` |
> | `GET /api/v1/approvals/pending` | `{"status": "placeholder", "message": "...", "pending_approvals": []}` |
> | `POST /api/v1/approvals/{approval_id}/action` | `{"status": "placeholder", "message": "...", "approval_id": "...", "received_action": null}` |
> | `GET /api/v1/activity` | `{"status": "placeholder", "message": "...", "entries": []}` |
> | `POST /api/v1/activity/rollback/{activity_id}` | `{"status": "placeholder", "message": "...", "activity_id": "..."}` |
>
> ### Missing Implementation (Deferred to December)
> - **Pydantic request/response schemas** — No schemas defined in `backend/schemas/`. All endpoints use raw dict returns.
> - **Standardized error handling** — Section 5/6 error schema not implemented. No exception handlers for 400/401/404/409/422/502.
> - **Request validation** — `POST /api/v1/approvals/{approval_id}/action` accepts any JSON body; no schema validation.
> - **Response schema alignment** — Placeholder responses wrap data differently than target contracts (e.g., `system/status` nests fields under `components`, `files` wraps array in object).
> - **Frontend approval body mismatch** — `frontend/js/api.js` `submitApprovalAction` sends only `{action}`; contract expects `{action, custom_filename, custom_tags}`.
> - **Uploads, sync, AI processing, approvals, database, rollback** — No business logic implemented. All endpoints are stubs.
>
> **⚠️ Placeholder responses are temporary and must not be treated as production API behavior.** The JSON examples in Sections 2.1–2.6 represent the **TARGET CONTRACT / FUTURE IMPLEMENTATION** to be built in Phase 1+.

---

## 1. API Design Principles

1. **REST-Style Conventions:** Endpoints follow standard HTTP verbs (`GET`, `POST`, `PUT`, `DELETE`) and resource-oriented URL paths.
2. **JSON Payloads:** All request bodies and response payloads use `application/json` with UTF-8 encoding.
3. **Consistent Base Path & Versioning:** All endpoints are versioned with a prefix (e.g., `/api/v1/`).
4. **Loopback & Auth Restrictions:**
   - Local Node endpoints run on `http://127.0.0.1:8000` (FastAPI proposed).
   - Cloud / Mobile endpoints require TLS (`https://`) and OAuth 2.0 / Bearer JWT headers (`Authorization: Bearer <token>`).
5. **No Secret Leaks:** Headers, query parameters, and error responses must never expose raw API keys, OAuth refresh tokens, or user credentials.

---

## 2. Proposed Local Backend API Groups

The primary local backend on the Windows laptop exposes local management endpoints to control sync, trigger AI processing, and review human approval queues.

### 2.1 Health & System Status
* **`GET /api/v1/health`** — Check local server operational health.
  ```json
  {
    "status": "healthy",
    "version": "0.1.0-proposed",
    "timestamp": "2026-11-07T21:14:45Z"
  }
  ```
  > **TARGET CONTRACT / FUTURE IMPLEMENTATION** — Current implementation returns additional `app` field.

* **`GET /api/v1/system/status`** — Get local AI engine (Ollama) and cloud sync status.
  ```json
  {
    "ollama_status": "online",
    "ollama_model": "llama3:8b",
    "sync_service": "idle",
    "pending_approvals_count": 2
  }
  ```
  > **TARGET CONTRACT / FUTURE IMPLEMENTATION** — Current placeholder returns nested `components` object with mock values.

### 2.2 File Indexing & Metadata
* **`GET /api/v1/files`** — List indexed files across all or specific connected storage providers.
  - Query parameters (proposed): `provider` (optional filter, e.g. `google_drive`, `onedrive`), `search` (keyword), `limit`, `offset`.
* **`GET /api/v1/files/{file_id}`** — Retrieve unified file details, tags, category, and native provider reference.
  ```json
  {
    "file_id": "file_9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "provider": "GOOGLE_DRIVE",
    "provider_account_id": "acc_personal_drive",
    "provider_file_id": "1A2B3C4D5E6F7G8H9I0J",
    "original_name": "receipt_scan_042.pdf",
    "logical_path": "/Finances/2026/Receipts/receipt_scan_042.pdf",
    "mime_type": "application/pdf",
    "size_bytes": 1048576,
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "processing_state": "COMPLETED",
    "category": "Financial/Receipts",
    "tags": ["#groceries", "#2026", "#tax-deductible"],
    "created_at": "2026-11-07T20:00:00Z"
  }
  ```
  > **TARGET CONTRACT / FUTURE IMPLEMENTATION** — Current placeholders return wrapper objects (`{"status": "placeholder", "message": "...", "files": []}` and `{"status": "placeholder", "message": "...", "file_id": "..."}`) instead of direct arrays/objects.

### 2.3 Synchronization Control
* **`GET /api/v1/sync/status`** — Get active intake queues, sync progress, and provider connectivity states.
* **`POST /api/v1/sync/trigger`** — Trigger immediate poll of connected intake inboxes (optional query param: `provider_id`).

### 2.4 AI Processing Queue
* **`POST /api/v1/ai/process/{file_id}`** — Trigger local OCR and AI classification for a file.
* **`GET /api/v1/ai/tasks/{task_id}`** — Query status of an asynchronous local AI task.

### 2.5 Human Approval Queue
* **`GET /api/v1/approvals/pending`** — Retrieve list of organization plans awaiting human approval.
  ```json
  {
    "pending_approvals": [
      {
        "approval_id": "appr_12345",
        "file_id": "file_9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "provider": "GOOGLE_DRIVE",
        "current_path": "GoogleDrive:/MobileInbox/receipt_scan_042.pdf",
        "proposed_path": "GoogleDrive:/Finances/2026/Receipts/2026-11-07_Grocery_Store.pdf",
        "proposed_tags": ["#groceries", "#2026"],
        "confidence_score": 0.92,
        "is_consequential": true
      }
    ]
  }
  ```
  > **TARGET CONTRACT / FUTURE IMPLEMENTATION** — Current placeholder returns wrapper object with `status: "placeholder"`.

* **`POST /api/v1/approvals/{approval_id}/action`** — Execute human decision.
  ```json
  {
    "action": "APPROVE", 
    "custom_filename": null,
    "custom_tags": null
  }
  ```
  > **TARGET CONTRACT / FUTURE IMPLEMENTATION** — Current placeholder accepts any JSON body and returns `received_action`. **Known issue:** Frontend `submitApprovalAction` sends only `{action}`; contract expects `{action, custom_filename, custom_tags}`. To be fixed in December.

### 2.6 Activity History & Audit Log
* **`GET /api/v1/activity`** — Query operational log history (moves, renames, syncs across providers).
* **`POST /api/v1/activity/rollback/{activity_id}`** — Request single-click rollback of a previous move operation via the appropriate provider adapter.

### 2.7 Proposed Storage Provider Management (Future Phase 2+)
* **`GET /api/v1/providers`** — List registered cloud storage providers, connection states, and declared capabilities.
* **`POST /api/v1/providers/{provider_type}/connect`** — Initiate secure local OAuth exchange (Google Drive, OneDrive, Dropbox, Box) or secure master key/credential exchange (MEGA).
* **`DELETE /api/v1/providers/{account_id}`** — Disconnect account and revoke active session.

---

## 3. Proposed Mobile Application Interface

The mobile application communicates with cloud storage inboxes for uploads and the Cloud Control Plane for metadata.

* **Authentication:** `POST /api/v1/auth/login` (Obtains access & refresh tokens via Supabase Auth).
* **Device Registration:** `POST /api/v1/devices/register` (Registers mobile device ID for notifications).
* **Direct Storage Upload:** Mobile uploads stream file bytes directly to the target cloud provider intake (e.g., Google Drive API `/upload/drive/v3/files` or OneDrive/Dropbox/MEGA upload endpoints).
* **Upload Progress & Status Query:** Mobile app queries cloud metadata endpoints to check if uploaded files have been processed by the local node.

---

## 4. Cloud Integration Responsibilities

| Service | Primary Responsibilities | Data Exchanged |
|---|---|---|
| **Google Drive API** | Direct payload storage, Mobile Inbox folder storage, final folder moves/renames via Google Drive Adapter. | Raw file binaries, Drive folder IDs, storage tokens. |
| **Microsoft OneDrive API** | Payload storage, delta synchronization, driveItem moves/renames via OneDrive Adapter. | Raw file binaries, Graph driveItem IDs, tokens. |
| **Dropbox API** | Payload storage, cursor-based folder listing, chunked upload sessions via Dropbox Adapter. | Raw file binaries, Dropbox paths/IDs, tokens. |
| **Box API** | Enterprise storage, metadata templates, chunked uploads via Box Adapter. | Raw file binaries, Box file IDs, tokens. |
| **MEGA API (Proposed)** | Zero-knowledge client-side encrypted storage, client-side decrypted intake via MegaAdapter. | Ciphertext binaries, node handles, encrypted file keys. Plaintext never sent to MEGA servers. |
| **Supabase (Proposed)** | User account authentication, device registration, metadata state synchronization across clients. | JWTs, file metadata JSON (hashes, tags, paths, states). No file binaries. |
| **Local Sync Service** | Polling inboxes across provider adapters, downloading intake payloads to local node, registering tasks in SQLite. | File payloads, state change triggers, checksum verification. |

---

## 5. Data Format Conventions

* **Identifiers:** String UUIDv4 format (e.g., `"file_9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"`).
* **Timestamps:** ISO 8601 strings in UTC format (e.g., `"2026-11-07T21:14:45Z"`).
* **File Sizes:** Integer count of bytes.
* **Processing States Enum:**
  `PENDING` | `DOWNLOADING` | `DOWNLOADED` | `PROCESSING` | `AWAITING_APPROVAL` | `COMPLETED` | `FAILED`
* **Standard Error Schema:**
  ```json
  {
    "error": {
      "code": "RESOURCE_NOT_FOUND",
      "message": "The requested file ID was not found.",
      "details": {
        "file_id": "file_invalid_123"
      },
      "timestamp": "2026-11-07T21:14:45Z"
    }
  }
  ```

---

## 6. Error Handling Specifications

> **NOT YET IMPLEMENTED** — Standard error schema (Section 5) and exception handlers for 400/401/404/409/422/502 are deferred to December.

| Scenario | HTTP / Error Code | Behavior & Response |
|---|---|---|
| **Invalid Request Payload** | `400 Bad Request` | Returns JSON error detailing invalid parameters or malformed syntax. |
| **Unauthorized Access** | `401 Unauthorized` | Missing or expired Bearer token. Client prompts for token refresh/login. |
| **Resource Not Found** | `404 Not Found` | Requested file ID or approval ID does not exist in local SQLite index. |
| **Duplicate Upload** | `409 Conflict` | File hash (SHA-256) matches existing file. Returns existing file metadata without re-downloading. |
| **Google Drive API Error** | `502 Bad Gateway` | Exponential backoff retry triggered. Sync state set to `PENDING` with retry note. |
| **AI Extraction Failure** | `422 Unprocessable` | Unreadable format or OCR failure. File isolated to `FAILED` queue for manual review. |

---

## 7. Developer Integration Rules

To support seamless collaboration between the two developers on GitHub:

1. **Pre-Implementation Contract Sign-off:** Neither developer may write backend endpoints or mobile client integrations without first submitting an update to `API_CONTRACT.md` and agreeing on the payload schema.
2. **Backward Compatibility:** Endpoint changes must preserve backward compatibility or increment the API version prefix (`/api/v2/`).
3. **Mandatory Test Coverage:** Every newly introduced API route must include unit tests verifying success schemas and standard error responses before merging to `main`.
4. **Code Review Gate:** Pull requests involving API contract changes require explicit review and sign-off from both developers.
