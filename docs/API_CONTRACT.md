# SiloVault — Proposed API Contracts & Data Specifications

This document defines the proposed communication contracts, data formatting rules,
error handling standards, and integration protocols for **SiloVault**.

For overall architecture, see [ARCHITECTURE.md](ARCHITECTURE.md). For data movement, see [DATA_FLOW.md](DATA_FLOW.md). For security guidelines, see [SECURITY.md](SECURITY.md).

> [!IMPORTANT]
> **Implementation status (26 November 2026):** This document contains both implemented API behavior and target contracts. It is no longer accurate to describe the backend as a skeleton or all non-health endpoints as placeholders. Core local file operations, activity history, Google Drive connect/status/disconnect, rename, logical move, deletion/recovery paths, and the AI task/approval backend workflow have implementation and automated coverage. However, not every proposed API group is complete: full sync/intake, the end-user AI pipeline, approvals UI, and a complete user-facing restore workflow remain incomplete.
>
> The latest full automated test run (26 November 2026, excluding the preserved duplicate module `tests/test_m52_vision_runtime_new.py`) reported **1792 passed, 3 skipped, 0 failed, 1 warning**. The warning is a Starlette/FastAPI deprecation for `HTTP_422_UNPROCESSABLE_ENTITY` (replacement: `HTTP_422_UNPROCESSABLE_CONTENT`). The HVSA H1.5.3 storage-account management API (list, detail, create, partial update) is implemented; see §2.8. Account-scoped file operations (download, rename, logical move, delete/quarantine, recovery and upload) are implemented; see §2.9. See `ARCHITECTURE.md` for provider-specific behavior and known endpoint limitations. In particular, the generic file DELETE endpoint must not be described as full Google Drive deletion support.
>
> **Contract rule:** Examples and schemas below that are not explicitly marked implemented remain target specifications, not guarantees about current runtime responses. Sections 2.8 and 2.9 are implemented and covered by automated tests. Verify endpoint behavior against the implementation and tests before relying on a contract.
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

### 2.8 Storage Account Management (Implemented — HVSA H1.5.3)

Base path: `/api/v1/storage-accounts`. Every response below excludes `credentials_ref`, tokens, and all other credential material. Status values are `pending`, `connected`, `disconnected`, and `error`; CRUD behavior does not depend on status.

* **`GET /api/v1/storage-accounts`** — List accounts. Optional `provider_id` query parameter is validated with the canonical provider rule (`400` if invalid); a valid provider with no matches returns `{"items": [], "total": 0}`. Response: `{"items": [StorageAccountResponse], "total": number}` (not paginated).
* **`GET /api/v1/storage-accounts/{account_id}`** — Return one account (`200`) or `404` when it does not exist.
* **`POST /api/v1/storage-accounts`** — Create an account (`201`).
  ```json
  {
    "provider_id": "GOOGLE_DRIVE",
    "display_name": "Work Drive",
    "configuration": {"folder_id": "optional-provider-value"}
  }
  ```
  `provider_id` must match `^[A-Z][A-Z0-9_]*$`; `display_name` is stored trimmed. `configuration` defaults to `{}` and must contain only JSON-serializable values (`NaN`/`Infinity` are rejected). Unknown fields, including `credentials_ref`, are rejected with `422`. The server generates `credentials_ref = storage-account:<uuid4>`; the new account starts `pending` with no credentials loaded and no adapter installed. A reference collision returns `409`.
* **`PATCH /api/v1/storage-accounts/{account_id}`** — Partially update `display_name` and/or `configuration` (`200`). Omitted fields are unchanged, `null` leaves the existing value unchanged, and an empty body is a no-op. `configuration` replaces the stored document rather than merging. `provider_id` and `credentials_ref` are rejected with `422`. Unknown account: `404`. Invalid name or non-finite configuration: `422`. Cache invalidation is scoped to the updated account and happens only when a LOCAL account's `storage_root` changes.
* **`StorageAccountResponse`**: `id`, `provider_id`, `display_name`, `status`, `configuration`, `created_at`, `updated_at`.

Not implemented: an account `DELETE` endpoint and an account-management UI. Account-scoped file routing is implemented (HVSA H1.5.4); see §2.9. Account-scoped Google Drive OAuth is implemented under `/api/v1/storage-accounts/{account_id}/google-drive/connect|status|disconnect`.

### 2.9 Account-Scoped File Operations (Implemented — HVSA H1.5.4)

File records carry an optional `provider_account_id`. Every file operation resolves its adapter through one helper, `backend/storage/file_account_resolution.py:resolve_file_adapter()`:

- **Legacy record (`provider_account_id` is `NULL`):** dispatches through the provider registry (`lookup(record.provider)`), exactly as before H1.5.4.
- **Managed record (`provider_account_id` is set):** resolves exactly that `StorageAccount`. The account must exist and its `provider_id` must equal the record's `provider`.

There is **no fallback**: a managed-account failure never resolves through the provider registry, so a request can never silently read, rename, move, delete or restore a file through a different account or the legacy provider adapter.

Account-resolution errors map to these responses. Every message below is fixed: no account ID, credential reference, token, filesystem path or chained exception text is ever written into a response body. A `StorageUnavailableError` — whether raised during account resolution, by an adapter operation, or by a legacy registry lookup — is always answered with the single message `Storage provider is temporarily unavailable.`; the exception text is kept server-side (logs and the chained `__cause__`) for diagnostics.

| Condition | HTTP | `detail` |
|---|---|---|
| The file's storage account does not exist | `404` | `The storage account for this file was not found.` |
| The account belongs to a different provider than the file | `409` | `The storage account does not match this file's storage provider.` |
| The account is disconnected, has no usable credentials, or fails authentication | `503` | `The storage account for this file is not available.` |
| Credential store unavailable, or the provider is not registered (legacy record) | `503` | `Storage provider is temporarily unavailable.` |

* **`GET /api/v1/files/{file_id}/download`** — resolves the record's adapter as above. No new query parameters.
* **`PATCH /api/v1/files/{file_id}/rename`** — same resolution. LOCAL renames are metadata-only; Google Drive renames change the Drive filename.
* **`PATCH /api/v1/files/{file_id}/move`** — same resolution. LOCAL and Google Drive moves are **logical only**: `logical_path` changes in SiloVault, the physical file location and the Drive parent folder do not.
* **`DELETE /api/v1/files/{file_id}`** — same resolution; the same adapter instance performs quarantine, rollback restoration and cleanup. The durable `PendingDeletion` row stores the record's `provider_account_id` (`NULL` for legacy records) so recovery resolves the same account.
* **`POST /api/v1/uploads`** — accepts an optional `account_id` query parameter in addition to `provider` (default `LOCAL`).
  - Omitted: legacy provider-level upload through the registry; the record's `provider_account_id` stays `NULL`.
  - Supplied: uploads through that exact managed account, requires the account's provider to match `provider`, and persists `account_id` as the new record's `provider_account_id`. Empty or whitespace-only values return `422`; an unknown account returns `404`, a provider mismatch `409`, and a disconnected/unavailable account `503`. The `LOCAL` DB-failure compensation (quarantine + cleanup) uses the same resolved adapter, so a managed LOCAL upload is cleaned up in that account's own storage root.
  - Cloud uploads have **no** compensating delete: if the provider upload succeeds and the database commit fails, the provider file can be orphaned (known limitation, unchanged by H1.5.4).
* **Recovery** — the background `RecoveryService` resolves each `PendingDeletion` through the same helper. Any resolution failure schedules a transient retry and keeps the row; it is never treated as a successful cleanup or a missing file.

No account-count limit is imposed by the application: accounts are addressed individually, with no maximum, quota or default-account selection. `GET /api/v1/files` still lists all records; there is no `account_id` filter.

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
