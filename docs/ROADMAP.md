# SiloVault — Phased Development Roadmap

This document defines the proposed multi-phase development roadmap for **SiloVault** as it evolves into a **privacy-focused, AI-powered unified cloud storage management platform**.

For overall scope and terminology, see [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md). For structural design, see [ARCHITECTURE.md](ARCHITECTURE.md). For data movement, see [DATA_FLOW.md](DATA_FLOW.md). For security guidelines, see [SECURITY.md](SECURITY.md).

> [!NOTE]
> **Current implementation snapshot (26 November 2026):** M0, M1, M2, M3.1, and the current M3.2 implementation checkpoint are complete. The HVSA H1.5 storage-account foundation is implemented: storage-account list/detail/create/partial-update APIs, server-generated credential references, account-scoped Google Drive OAuth, per-account adapter caching with scoped invalidation, cross-account isolation tests, and a migration-owned partial unique index (Alembic head `b7c4e1a92f30`). Account-scoped file operations (download, rename, logical move, delete/quarantine/restore, recovery and upload) are implemented and resolve each record through its own storage account; account deletion and an account-management UI are not implemented. M4.0 AI architecture planning is complete. M4.1 has progressed beyond the database/API foundation: the AI task lifecycle, worker processing, proposal storage, and the backend approval/rejection workflow are implemented and tested. The end-to-end AI task/approval test has passed. The latest full automated test run (26 November 2026, excluding the preserved duplicate module `tests/test_m52_vision_runtime_new.py`) reported **1792 passed, 3 skipped, 0 failed, 1 warning**. This roadmap retains future milestones as plans; a milestone's planned capabilities must not be mistaken for implemented functionality.
>
> **Milestone numbering:** the AI model-runtime track uses its own `AI-M4.x` / `AI-M5.x` numbering (AI-M5.1 through AI-M5.9.x). Those identifiers are **not** the roadmap Milestone 5 below, which covers additional cloud providers. Read "M5.x" in AI documents and test module names as AI-M5.x.

---

## Roadmap Overview & Milestone Dependencies

```mermaid
flowchart TD
    M0["Phase 0: Foundation & Governance\n(COMPLETE - Skeleton & Docs)"] --> M1["Milestone 1: Local Storage Foundation\n(DB, Metadata Model, Local Indexing)"]
    M1 --> M2["Milestone 2: Unified Storage Architecture\n(Provider Interface, Capabilities, Account Model)"]
    M2 --> M3["Milestone 3: First Cloud Integration\n(Google Drive Adapter, Auth, Intake Sync)"]
    M1 & M3 --> M4["Milestone 4: Local AI Pipeline\n(Ollama, OCR, Tagging, Approval Queue)"]
    M2 & M3 --> M5["Milestone 5: Additional Cloud Providers\n(OneDrive, Dropbox, Box Adapters)"]
    M3 & M4 & M5 --> M6["Milestone 6: Mobile & Advanced Sync\n(PWA Uploader, Offline Queue, Multi-Sync)"]
    M6 --> M7["Phase 7: Future Expansion\n(Natural Language Search, Vector RAG, S3)"]
```

---

## Detailed Milestone Specifications

### Phase 0 — Foundation & Governance
* **Status:** **COMPLETE**
* **Objective:** Establish repository safety, architectural blueprints, development guidelines, collaboration workflows, and a runnable application skeleton.
* **Deliverables:** Clean repository layout, `.gitignore`, `AGENTS.md`, `README.md`, core documentation (`docs/`), FastAPI backend with 14 registered routes, PWA frontend shell with 6 views, and 6 passing tests.

---

### Milestone 1 — Local Storage Foundation
* **Status:** **COMPLETE**
* **Objective:** Build a robust, persistence-backed local foundation capable of tracking files, schemas, and operational history without cloud dependencies.
* **Key Capabilities:**
  - SQLAlchemy models and SQLite database initialization (`silo_vault.db`).
  - Unified file metadata model (`UnifiedFileRecord`) tracking filename, logical path, MIME type, size, SHA-256 hash, and timestamps.
  - Safe local path resolution and traversal sanitization (preventing `../` attacks).
  - Real file registration and listing endpoints (`GET /api/v1/files`, `GET /api/v1/files/{file_id}`).
  - Dynamic file listing in the PWA frontend dashboard replacing placeholder text.
  - **Activity History** (`GET /api/v1/activity`) recording every successful upload, download, and delete with paginated filtering and newest-first ordering.
  - Overview "Recent Activity" panel wired to the activity API; dashboards and uploads refresh automatically after each operation.
* **Implemented so far (M1 tasks 1–7):**
  - `UnifiedFileRecord` model, `backend/database.py` session management, and `silo_vault.db` initialization.
  - Real file endpoints: `POST /api/v1/uploads`, `GET /api/v1/files`, `GET /api/v1/files/{file_id}`, `GET /api/v1/files/{file_id}/download`, `DELETE /api/v1/files/{file_id}` (quarantine-based safe deletion with rollback recovery).
  - Activity History endpoint: `GET /api/v1/activity` (pagination, `action_type` filter, deterministic newest-first ordering via `timestamp DESC, id DESC`). Activity records are written for every successful upload, download, and delete; failed operations produce no record.
  - Frontend Files, Uploads, Dashboard, and Activity views wired to the real backend (upload queue, drag-and-drop, download/delete actions, load-more pagination on the dedicated Activity page, automatic refresh of Recent Activity on the Overview after any operation).
  - **Alembic** database migrations: baseline revision `b436c12118f9` for `unified_file_records`; activity revision `4ef0d0cc89e5` creating `activity_records` with three indexes (`timestamp`, `action_type`, `provider`); existing development database stamped at baseline without data loss; explicit `alembic upgrade head` workflow (migrations are never run automatically on app startup).
  - Test suite: **93 tests passing (1 platform-specific skip on Windows)** covering database, uploads, files, downloads, deletion rollback, Alembic migrations (including the new activity table), and activity history behavior.
* **Historical limitations at the M1 checkpoint (some were addressed in later milestones):**
  - **Activity History rollback** (`POST /api/v1/activity/rollback/{id}`) is a **placeholder** — it returns a constant message. Reversible operations require provider adapters that do not yet exist.
  - **Cloud provider integrations (Google Drive, OneDrive, Dropbox, Box, MEGA)** are **planned but not implemented**. The only operational storage today is local disk under the configured uploads directory.
  - **AI processing (Ollama, OCR, tagging, classification)**, the **approval queue**, and **cross-provider sync** are all **planned for future milestones** and have no working implementation yet.
* **Remaining for M1:** None — milestone complete.
* **Dependencies & Prerequisites:** Phase 0 foundation skeleton.
* **Deliverables:** `backend/database.py` session management, `backend/models/file.py`, `backend/schemas/file.py`, populated `/files` endpoints, updated dashboard view.
* **Relevant Areas:**
  - Backend: `backend/database.py`, `backend/models/`, `backend/schemas/`, `backend/api/v1/endpoints/files.py`.
  - Frontend: `frontend/js/files.js`, `frontend/js/dashboard.js`, `frontend/js/api.js`.
  - Docs: `docs/API_CONTRACT.md`.
* **Suggested Tests:**
  - Unit tests for database table creation and CRUD operations.
  - Path traversal security tests (malicious filenames and paths).
  - API endpoint integration tests for `GET /api/v1/files` returning real JSON lists.
* **Risks & Decisions:**
  - *Decision:* Synchronous SQLite vs `aiosqlite` with async SQLAlchemy sessions.
  - *Risk:* Concurrent write lock contention in SQLite during bulk indexing.
* **Suggested Ownership:** Developer 1 (Windows / Backend Core).

---

### Milestone 2 — Unified Storage Architecture
* **Status:** **COMPLETE**
* **Objective:** Design and implement the provider-independent storage layer that allows SiloVault to connect multiple cloud providers through a common contract.
* **Key Capabilities:**
  - Abstract base class `StorageAdapter` defining standard methods (`authenticate`, `list_files`, `get_metadata`, `upload`, `download`, `rename`, `move`, `delete`, `check_sync_status`).
  - Provider capability model declaring feature flags (`supports_delta_sync`, `supports_chunked_upload`, `addressing_mode`).
  - Account and connection manager in the backend for storing encrypted provider credentials and tokens locally.
  - Virtual multi-provider namespace in `UnifiedStorageService`.
  - Standardized error handling and mapping (`NormalizedStorageError`, rate-limit handling).
* **Dependencies & Prerequisites:** Milestone 1 (data models for storing provider accounts and references).
* **Deliverables:** `backend/storage/interfaces.py`, `backend/storage/capabilities.py`, `backend/storage/registry.py`, `backend/storage/account_manager.py`, `backend/models/storage_account.py`.
* **Relevant Areas:**
  - Backend: `backend/storage/`, `backend/models/`, `backend/schemas/`.
  - Frontend: `frontend/js/settings.js` (provider accounts view).
  - Docs: `docs/ARCHITECTURE.md`.
* **Suggested Tests:**
  - Mock adapter unit tests verifying interface compliance and capability flags.
  - Error normalization tests for HTTP 429, 401, 404, 503 translations.
  - Multi-account registry isolation tests.
* **Risks & Decisions:**
  - *Decision:* Key management strategy for encrypting stored OAuth refresh tokens on disk.
  - *Decision:* Virtual path routing syntax (e.g., `/{provider}/{account_id}/path`).
* **Suggested Ownership:** Developer 2 (macOS / Architecture & Storage Abstractions).

---

### Milestone 3 — First Cloud Integration: Google Drive
* **Status:** **COMPLETE for M3.1 and the current M3.2 checkpoint** — OAuth, the adapter and file-management/recovery work are done; mobile-inbox intake polling and the sync worker are **not** implemented, and the Sync Status view remains a placeholder. Do not read this milestone as "cloud sync complete".
* **Objective:** Implement the first concrete cloud storage adapter using the Google Drive API, enabling real cloud file ingestion and management.
* **Scope note:** The Google Drive OAuth/adapter foundation and the currently implemented file-management/recovery work are complete for this checkpoint. The roadmap bullets below are the broader target scope; mobile inbox polling/intake synchronization and full cloud-provider parity are not implied to be complete. The generic file DELETE endpoint dispatches through the registered provider adapter; it is not LOCAL-only. Provider dispatch depends on a registered adapter — if an adapter is unavailable, the endpoint returns an appropriate unavailable response (HTTP 503). Google Drive automated tests use mocks and a fake keyring; do not claim live Google Drive production verification.
* **Key Capabilities:**
  - Concrete `GoogleDriveAdapter` implementing `StorageAdapter`.
  - OAuth 2.0 PKCE / loopback flow for developer authentication with token refresh.
  - Mobile Inbox folder monitoring and intake polling.
  - Resumable file upload and streaming download.
  - Reversible file moves and rename operations within Google Drive.
  - Exponential backoff retry handler for Google Drive API rate limits.
* **Dependencies & Prerequisites:** Milestone 2 (Provider Adapter Interface).
* **Deliverables:** `backend/storage/google_drive.py`, OAuth credentials configuration, functional sync polling worker.
* **Relevant Areas:**
  - Backend: `backend/storage/google_drive.py`, `backend/api/v1/endpoints/sync.py`, `backend/config.py`.
  - Frontend: `frontend/js/settings.js` (there is no `frontend/js/sync.js`; the Sync Status view is a placeholder).
  - Docs: `docs/API_CONTRACT.md`, `docs/DATA_FLOW.md`.
* **Suggested Tests:**
  - Mock Google Drive API tests for folder listing, pagination, and download streaming.
  - OAuth token refresh flow tests.
  - Rate-limit backoff simulation tests.
* **Risks & Decisions:**
  - *Decision:* Google Drive API scopes (full drive vs restricted `drive.file` / app-folder scope).
  - *Risk:* API quota consumption during polling loops (mitigated by using `Changes.list` with page tokens).
* **Suggested Ownership:** Developer 1 (Windows / Cloud Sync Worker).

#### HVSA H1.5 — Storage Account Foundation (Implemented)
* **Status:** **IMPLEMENTED (H1.5.4 checkpoint)**
* **Implemented:** `GET /api/v1/storage-accounts` (optional provider filter), `GET /api/v1/storage-accounts/{account_id}`, `POST /api/v1/storage-accounts` (server-generated `storage-account:<uuid4>` reference, `201`, `409` on collision, `422` on invalid input), and `PATCH /api/v1/storage-accounts/{account_id}` (partial update, `404`/`422`, per-account cache invalidation only for a changed LOCAL `storage_root`). Account-scoped Google Drive OAuth remains available under `/api/v1/storage-accounts/{account_id}/google-drive/...`.
* **HVSA H1.5.4 — account-scoped file operations (implemented):** `resolve_file_adapter()` routes each file record to its own `StorageAccount`; download, rename, logical move, delete/quarantine, recovery (CLEANUP and RESTORE) and upload are account-aware, and managed-account failures return `404`/`409`/`503` with generic messages and never fall back to the provider registry. `POST /api/v1/uploads` accepts an optional `account_id` (omitted = legacy provider-level upload) and persists it as the record's `provider_account_id`; deletion copies it into `PendingDeletion`. LOCAL rename/move stay metadata-only and Google Drive move stays logical-only.
* **Data and isolation:** responses never include `credentials_ref` or secrets; all four statuses (`pending`, `connected`, `disconnected`, `error`) support CRUD; credential references are validated in `AccountManager` and backed by the migration-owned partial unique expression index `uq_storage_accounts_credentials_ref_trimmed` (ASCII whitespace trim set; Unicode whitespace remains a Python-side check); managed adapters stay in the per-account cache and never enter the provider registry.
* **Not implemented:** account `DELETE` and account-management UI. Business file endpoints are account-aware but `GET /api/v1/files` has no `account_id` filter. Cloud uploads still lack a compensating delete when the database commit fails after a successful provider upload.

---

### Milestone 4 — Local AI Processing Pipeline
* **Status:** **IN PROGRESS (M4.0 complete; M4.1 foundation in progress)**
* **Current checkpoint:** M4.0 architecture planning is complete. M4.1 has progressed beyond the database/API foundation: the AI task lifecycle, worker processing, proposal storage, and the backend approval/rejection workflow are implemented and tested. The end-to-end AI task/approval test has passed. The end-user OCR, image understanding, classification, semantic search, and approval-driven organization pipeline is still future work.
* **Objective:** Connect the local Ollama AI engine and local OCR to classify files, extract metadata, generate tags, and route consequential actions through human review.
* **Key Capabilities:**
  - Local HTTP client connecting to `http://127.0.0.1:11434` (Ollama) with configurable model (`llama3:8b`).
  - Local OCR extraction for PDFs and images.
  - Structured prompt engine constraining Ollama outputs to strict JSON schemas.
  - Duplicate detection engine comparing universal SHA-256 hashes.
  - Human Approval Queue (`GET /api/v1/approvals/pending`, `POST /api/v1/approvals/{approval_id}/action`).
  - Approval UI in PWA for reviewing, editing, approving, or rejecting proposed renames/moves.
* **Dependencies & Prerequisites:** Milestone 1 (file records) and Milestone 3 (real file payloads downloaded to intake temp sandbox).
* **Original planned deliverables:** `backend/ai/ollama_client.py`, `backend/ai/ocr.py`, `backend/ai/pipeline.py`, populated `/ai` and `/approvals` endpoints. **None of those three module paths exists in the repository** — they were superseded by the actual layout described next; keep them only as historical intent.
* **Actual implemented layout:** `backend/ai/` contains interfaces, storage, model installation, trusted catalogue, execution bridge, text extraction, processors, processor registration, the Florence-2 vision runtime, an OCR runtime/adapter pair, `pack_manager.py`, and the worker lifespan integration in `lifecycle.py`. The durable AI task lifecycle, proposal storage and backend approval/rejection workflow live in `backend/services/ai_worker.py` and `backend/services/approval.py`, with routes in `backend/api/v1/endpoints/ai_tasks.py`. The M4.4 worker infrastructure — lease heartbeat, expired-lease recovery, processor registration and lifespan integration — is implemented and tested.
* **What is still missing:** no Ollama client exists, so no local LLM inference path exists. The Florence-2 runtime needs optional packages and model weights, OCR is not wired into task processing, and `PackManager` is a read-only foundation. No AI capability is end-to-end functional, and no model downloader or pack installation service is implemented.
* **Relevant Areas:**
  - Backend: `backend/ai/`, `backend/api/v1/endpoints/processing.py`, `backend/api/v1/endpoints/review.py`.
  - Frontend: `frontend/js/dashboard.js`, `frontend/js/api.js` (approval actions).
  - Docs: `docs/SECURITY.md`.
* **Suggested Tests:**
  - Mock Ollama response parsing tests ensuring JSON schema validation.
  - Prompt injection isolation tests (adversarial text inside documents).
  - Approval queue execution and rollback tests.
* **Risks & Decisions:**
  - *Risk:* Ollama inference latency on large documents (mitigated by asynchronous background queue).
  - *Decision:* OCR engine selection (Tesseract binary vs lightweight local Python OCR).
* **Suggested Ownership:** Developer 2 (macOS / AI Pipeline & Approval UX).

---

### Milestone 5 — Additional Cloud Providers (OneDrive, Dropbox, Box, MEGA)
* **Status:** **PLANNED / PROPOSED**
* **Objective:** Expand the multi-cloud platform by implementing adapters for Microsoft OneDrive, Dropbox, Box, and MEGA based on the Milestone 2 architecture.
* **Key Capabilities:**
  - `OneDriveAdapter` utilizing Microsoft Graph API (upload sessions, delta queries).
  - `DropboxAdapter` using Dropbox API v2 (`/files/list_folder/continue`, content hashes).
  - `BoxAdapter` utilizing Box API (chunked upload, enterprise metadata).
  - `MegaAdapter` integrating MEGA's client-side zero-knowledge E2EE architecture (scheduled as a later provider integration once standard OAuth adapters are stable).
  - Cross-provider browsing and search in the PWA dashboard.
  - Cross-provider duplicate detection using unified SHA-256 hashes.
* **Dependencies & Prerequisites:** Milestone 2 (Adapter interface), Milestone 3 (lessons learned from Google Drive adapter), and a dedicated cryptographic/licensing spike for MEGA.
* **Deliverables:** `backend/storage/onedrive.py`, `backend/storage/dropbox.py`, `backend/storage/box.py`, and `backend/storage/mega.py` (with an optional isolated process boundary recommended for security and reliability, not required by license).
* **Relevant Areas:**
  - Backend: `backend/storage/`, `backend/api/v1/endpoints/files.py`.
  - Frontend: `frontend/js/files.js`, `frontend/js/settings.js`.
  - Docs: `docs/ARCHITECTURE.md`.
* **Suggested Tests:**
  - Adapter contract compliance test suite run against each new provider mock.
  - Multi-provider unified file search tests.
  - Resumable upload session tests across varying chunk sizes.
  - Local decryption verification test for MEGA ciphertext payload streams before SHA-256 calculation.
* **Risks & Decisions:**
  - *Risk:* Maintaining multiple cloud developer apps and OAuth verification requirements.
  - *Decision:* Priority order of adapters (recommended sequence: OneDrive first, then Dropbox, then Box, followed by MEGA after the E2EE adapter spike).
  - *MEGA Architecture Risk:* The official MEGA SDK is licensed under **BSD 2-Clause "Simplified"** (permissive, no copyleft), but MEGAcmd and third-party dependencies may carry other licenses (e.g., AGPLv3) and require individual audit. An official API key from MEGA Ltd. is required for production use. *Recommended:* Evaluate running MEGA operations in an isolated CLI subprocess (`MEGAcmd`) or separate micro-process boundary as a **security and reliability** measure (not a licensing requirement), to limit crash blast radius and simplify credential handling.
  - *MEGA Key Management:* Establishing a secure workflow for master key / password derivation without ever storing plaintext passwords or exposing keys to the frontend.
* **Suggested Ownership:** Shared / Decoupled (Developer 1 on OneDrive; Developer 2 on Dropbox & Box; Joint spike on MEGA).

---

### Milestone 6 — Mobile Client & Advanced Synchronization
* **Status:** **PLANNED**
* **Objective:** Deliver an offline-friendly mobile PWA experience for capturing, uploading, and monitoring files across connected cloud storage inboxes.
* **Key Capabilities:**
  - Responsive mobile PWA with camera capture and native file picker integration.
  - Offline upload queue backed by IndexedDB.
  - Direct upload streaming to target cloud provider inboxes.
  - Real-time or polled processing status indicators ("Uploaded", "Processing", "Awaiting Approval").
  - Multi-provider conflict resolution (handling remote renames vs local organization moves).
* **Dependencies & Prerequisites:** Milestone 3 (cloud intake) and Milestone 4 (approval workflows).
* **Deliverables:** Enhanced `frontend/` PWA application, service worker offline caching, upload queue worker.
* **Relevant Areas:**
  - Frontend: `frontend/js/uploads.js`, `frontend/service-worker.js`, `frontend/manifest.json`.
  - Backend: `backend/api/v1/endpoints/uploads.py`, `backend/api/v1/endpoints/sync.py`.
  - Docs: `docs/DATA_FLOW.md`.
* **Suggested Tests:**
  - Service worker offline cache verification.
  - IndexedDB upload queue retry tests on network reconnect.
  - End-to-end upload to intake inbox simulation.
* **Risks & Decisions:**
  - *Risk:* Browser background execution limits on mobile OS (iOS Safari background timeouts).
  - *Decision:* Mobile direct upload vs relaying through local node when local node is on same Wi-Fi.
* **Suggested Ownership:** Developer 2 (Frontend / Mobile PWA Focus).

---

### Phase 7 — Future Expansion (Post-v1.0)
* **Status:** **FUTURE POSSIBILITY**
* **Objective:** Advanced intelligence, storage providers, and platform extensions after v1.0 stability.
* **Main Features:**
  - Natural-language file search across all providers using local embeddings (e.g., ChromaDB/Qdrant + Ollama).
  - Semantic document querying and automated summarization.
  - Additional cloud providers: AWS S3, Nextcloud, WebDAV.
  - Native Android application with background auto-upload synchronization.
* **Dependencies:** Successful completion of Milestones 1–6.

---

## Developer Ownership & Collaboration Summary

| Milestone | Primary Owner | Secondary Reviewer | Primary Focus Area |
|---|---|---|---|
| **M1: Local Storage Foundation** | Developer 1 | Developer 2 | SQLite models, path sanitization, file indexing |
| **M2: Unified Storage Architecture** | Developer 2 | Developer 1 | StorageAdapter contract, capability flags, account model |
| **M3: First Cloud Integration (Google Drive)** | Developer 1 | Developer 2 | Google Drive API, OAuth token storage, inbox polling |
| **M4: Local AI Processing Pipeline** | Developer 2 | Developer 1 | Ollama client, prompt schemas, OCR, approval UI |
| **M5: Additional Providers (OneDrive/Dropbox/Box/MEGA)** | Split | Reciprocal | OneDrive (Dev 1), Dropbox & Box (Dev 2), Joint spike (MEGA) |
| **M6: Mobile & Advanced Sync** | Developer 2 | Developer 1 | PWA upload queue, offline resilience, conflict handling |

---

## Key Architectural Decisions Requiring Alignment

Before starting December implementation, the two developers must formally agree on:

1. **Async vs Sync Database Layer:** Whether to use SQLAlchemy 2.0 with synchronous SQLite or asynchronous `aiosqlite`.
2. **Local Credential Storage:** Mechanism for storing cloud OAuth refresh tokens and MEGA credentials securely on disk (e.g., OS keyring vs AES-GCM encrypted local store with user passphrase).
3. **Provider Priority & Roadmap Sequence:** Confirming Google Drive as initial target in M3, followed by OneDrive, Dropbox, and Box in M5, with MEGA scheduled as a subsequent E2EE integration spike.
4. **Approval Action Schema:** Harmonizing `POST /api/v1/approvals/{id}/action` payload between backend expectation and frontend submission.
