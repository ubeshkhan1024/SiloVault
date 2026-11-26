# SiloVault — Consolidated Project Blueprint

This document is the concise status snapshot for the project. Detailed design and implementation notes remain in `PROJECT_OVERVIEW.md`, `ARCHITECTURE.md`, `ROADMAP.md`, `DATA_FLOW.md`, `SECURITY.md`, and `API_CONTRACT.md`.

## Current Status — 25 November 2026

- **M0 — Foundation and governance:** Complete.
- **M1 — Local storage foundation:** Complete.
- **M2 — Unified storage architecture:** Complete.
- **M3.1 — Google Drive OAuth foundation:** Complete.
- **M3.2 — Google Drive integration and file-management reliability:** Implementation work completed to the current checkpoint. The generic `DELETE /api/v1/files/{file_id}` endpoint dispatches through the registered provider adapter; it is not LOCAL-only. Provider dispatch depends on a registered adapter — if an adapter is unavailable, the endpoint returns an appropriate unavailable response (HTTP 503). Google Drive automated tests use mocks and a fake keyring; do not claim live Google Drive production verification. Do not interpret adapter support as proof that every provider operation is exposed through every API endpoint.
- **HVSA H1.5 — Storage account foundation:** Implemented. Storage-account list, detail, create, and partial-update APIs; server-generated credential references; account-scoped Google Drive OAuth; per-account adapter caching with scoped invalidation; cross-account isolation tests; and a migration-owned partial unique index on trimmed credential references (current Alembic head `b7c4e1a92f30`). **HVSA H1.5.4 account-scoped file operations** are implemented: download, rename, logical move, delete/quarantine/restore, recovery and upload resolve the record's own storage account and fail closed, with no fallback to the provider registry; legacy records (`provider_account_id` `NULL`) keep the previous behavior. Account deletion and an account-management UI are not implemented.
- **M4.0 — AI architecture planning:** Complete.
- **M4.1 — AI database/API foundation:** The AI task lifecycle, worker processing, proposal storage, and the backend approval/rejection workflow are implemented and tested. The end-to-end AI task/approval test has passed. The full AI processing pipeline (OCR, image understanding, classification, semantic search, and approval-driven organization) is not yet complete.

## Verification Snapshot

Latest full test run (26 November 2026, excluding the preserved duplicate module `tests/test_m52_vision_runtime_new.py`): **1792 passed, 3 skipped, 0 failed, 1 warning** (157.86 seconds). The warning is the Starlette/FastAPI deprecation for `HTTP_422_UNPROCESSABLE_ENTITY` (replacement: `HTTP_422_UNPROCESSABLE_CONTENT`). This is an automated test result, not a claim of production readiness or complete live-provider coverage.

## Implemented Capabilities

- SQLite-backed unified file metadata and Alembic-managed schema.
- Local storage operations and activity history.
- Provider-neutral storage-operation interface, capability map, and registry.
- Google Drive OAuth using PKCE and a loopback callback.
- Refresh-token persistence through the operating-system keyring; access tokens remain in memory.
- Google Drive root-folder management and adapter-level file operations.
- Quarantine-based deletion and persistent recovery records with retry/lease fencing.
- File rename and logical-move APIs, with provider-specific behavior described in the architecture document.
- Storage-account management API (`GET` list/detail, `POST`, `PATCH`) with server-generated credential references, status-independent CRUD, and cross-account isolation.
- Account-scoped Google Drive OAuth and per-account adapter caching; a migration-owned partial unique index on normalized credential references.
- Account-scoped file operations (download, rename, logical move, delete/quarantine/restore, recovery and upload) routed through the record's own storage account, fail-closed with no provider-registry fallback; the optional `account_id` upload parameter is backward compatible.
- Initial AI database/API foundation work, including the AI task lifecycle, worker processing, proposal storage, and the backend approval/rejection workflow.

## Important Boundaries and Known Gaps

- OneDrive, Dropbox, Box, and MEGA adapters are not implemented.
- Cross-provider synchronization and mobile offline upload workflows are not complete.
- The user-facing AI pipeline and approval workflow are not complete. The backend approval/rejection workflow is implemented, but the frontend approval experience and the full AI processing pipeline (OCR, image understanding, classification, semantic search, and real AI-powered file organization) remain future work.
- A complete user-facing restore workflow is not yet available; recovery foundations exist for specific failure-recovery paths.
- The generic DELETE endpoint dispatches through the registered provider adapter; it is not LOCAL-only. Provider dispatch depends on a registered adapter — if an adapter is unavailable, the endpoint returns an appropriate unavailable response (HTTP 503). Google Drive coverage is mocked.
- Automated Google Drive tests primarily use mocked services. Automated success does not equal production certification.
- There is no storage-account `DELETE` endpoint and no account-management UI; disconnecting Google Drive keeps the account row.
- Business file endpoints are account-aware: a record that names a storage account resolves exactly that account, and a managed-account failure never falls back to the provider registry. Legacy records keep the previous provider-scoped behavior. `GET /api/v1/files` still lists all records; there is no `account_id` filter.
- Cloud uploads have no compensating delete: if the provider upload succeeds and the database commit fails, the provider file can be orphaned. LOCAL uploads are compensated through the same resolved adapter.
- Credential-reference uniqueness covers ASCII whitespace only; Unicode whitespace variants are handled by the Python-side validation, not the database index.
- AI model runtime (AI-M5.x) is foundation-only: the Florence-2 vision runtime needs optional `torch`/`transformers` plus model weights, the OCR adapter is not wired into task processing, and `PackManager` is a read-only foundation with no download, checksum-verified installation or UI. No AI capability is end-to-end functional, and there is no Ollama client.

## Development Safety Rules

- Preserve originals and avoid destructive provider actions during development.
- Do not commit secrets, `.env` files, databases, uploads, model files, or runtime artifacts.
- Apply database migrations explicitly and only after a verified backup.
- Do not push or release until the developer has reviewed the complete diff and repository status.

Last updated: 26 November 2026.
