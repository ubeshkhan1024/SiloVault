# SiloVault — Project Overview

This document describes **what** SiloVault is and **why** it exists. It covers the
mission, vision, goals, non-goals, and shared terminology. Architectural *how* is
covered in [ARCHITECTURE.md](ARCHITECTURE.md); data movement in
[DATA_FLOW.md](DATA_FLOW.md); safety in [SECURITY.md](SECURITY.md); and the plan in
[ROADMAP.md](ROADMAP.md).

Status labels used throughout:

- **Confirmed** — a settled project decision.
- **Proposed** — a choice under consideration, not yet final.
- **Future possibility** — an idea that still requires discussion.

## 1. Mission

Give users (initially the two developers) a unified multi-cloud file management platform
where **AI organization happens privately on a local machine**, while **files across multiple
cloud storage providers (Google Drive, OneDrive, Dropbox, Box, MEGA)** are unified into a single
dashboard — without ever putting the originals at risk of silent loss or alteration.

## 2. Vision

The long-term vision, as a set of capabilities. Each item is labeled by how settled it
is.

| # | Capability | Status |
|---|------------|--------|
| 1 | A primary **Windows laptop** acts as the local AI processing node. | **Confirmed** |
| 2 | A **lightweight mobile application** for uploading files remotely. | **Confirmed** (in scope) |
| 3 | **Google Drive integration** as the initial cloud storage provider. | **Confirmed** as the initial provider |
| 4 | **Provider Adapter Architecture** for modular, multi-cloud storage connections. | **Planned** |
| 5 | **Multi-provider cloud storage support** (Microsoft OneDrive, Dropbox, Box, MEGA). | **Planned** |
| 6 | A dedicated **mobile-upload intake inbox** per connected provider or account. | **Confirmed** (in scope) |
| 7 | A **cloud metadata and account management** layer decoupling file bytes from metadata. | **Confirmed** (in scope) |
| 8 | **Local AI-powered** file classification, tagging, OCR, metadata extraction, and organization suggestions via Ollama. | **Confirmed** as a capability |
| 9 | **Natural-language file search** and semantic document querying across providers. | **Future possibility** |
| 10 | **File history, activity logs, cross-provider synchronization, and rollback tracking.** | **Confirmed** (in scope) |
| 11 | **Collaboration between two developers** using GitHub. | **Confirmed** |
| 12 | Future support for **additional devices and storage providers** (e.g., S3, Nextcloud). | **Future possibility** |

## 3. Goals

- Provide a **unified multi-cloud dashboard** to browse, search, and manage files across Google Drive, OneDrive, Dropbox, Box, and MEGA.
- Respect provider-specific security models, especially **MEGA's client-side end-to-end encryption (E2EE)**, by ensuring decryption occurs only locally through authorized user workflows.
- Keep AI processing **local and private** on the primary machine using Ollama.
- Keep storage providers **modular and independent** via a common Provider Adapter Interface.
- Let users upload files **from a phone, even when the local laptop is offline**.
- **Automate organization** (classify, tag, OCR, extract metadata, duplicate detection) while keeping
  humans in control of consequential changes.
- Make every operation **safe and reversible** — originals are never silently lost or overwritten.
- Preserve **provider-specific file IDs and metadata** while mapping to a common internal file representation.
- Keep **metadata, account connections, and physical file storage decoupled** so providers can evolve independently.
- Build for **reliability, privacy, maintainability, and future scalability**.
- Support a clean **two-developer GitHub collaboration** workflow.

## 4. Non-goals (for now)

These are explicitly out of scope at the foundation stage. Some may become future
possibilities, but none are planned yet.

- A general-purpose, multi-tenant SaaS product.
- Exposing the local AI node or Ollama directly to the public internet.
- Automatic, unattended reorganization or deletion of user files without explicit approval.
- Storing or caching full file payloads indefinitely on the local processing node (files remain in cloud storage).
- Hardcoding provider logic or cloud credentials directly into the application core.
- Replacing or duplicating individual cloud providers' native collaboration features.
- Inventing concrete database schemas or provider configurations before they are designed and agreed.

## 5. Stakeholders

- **Two developer-owners** who build and use the system, collaborating on GitHub.
  *(Confirmed.)*
- End users beyond the two developers. *(Future possibility — not designed for yet.)*

## 6. Terminology

| Term | Meaning |
|------|---------|
| **SiloVault** | The overall privacy-focused unified cloud file management platform. |
| **Local AI node** | The primary Windows laptop where local AI processing (Ollama) and core orchestration run. |
| **Mobile app** | The lightweight PWA client used to upload files remotely to cloud inboxes. |
| **Mobile-upload inbox** | The dedicated intake folder for files uploaded from mobile, awaiting local processing. |
| **Unified Storage Service** | The backend service coordinating file operations and queries across multiple connected storage providers. |
| **Provider Adapter** | A modular implementation conforming to the standard storage interface for a specific cloud provider (Google Drive, OneDrive, Dropbox, Box, MEGA). |
| **Cloud metadata layer** | The layer storing file metadata, tags, and account information in the cloud, separate from file binaries. |
| **Storage provider** | A cloud service storing actual file bytes (Google Drive, Microsoft OneDrive, Dropbox, Box, MEGA). |
| **Originals** | The unmodified files as uploaded. Treated as immutable by default. |
| **Organization** | Classifying, tagging, OCR, metadata extraction, duplicate detection, and arranging files. |
| **Consequential change** | Any operation that could meaningfully move, rename, reorganize, or discard user files and therefore requires human approval. |

## 7. Current status

The project is at the **foundation stage**: the repository holds documentation,
development rules (see [../AGENTS.md](../AGENTS.md)), configuration
([../.gitignore](../.gitignore)), and an **application skeleton**:

- **FastAPI backend** with 14 registered API routes under `/api/v1/` (health, system status, files, uploads, sync, AI processing, approvals, activity). All endpoints except `GET /api/v1/health` return placeholder responses.
- **PWA frontend shell** with 6 navigable views (Dashboard, Files, Uploads, Sync, Activity, Settings), service worker for offline caching, and ES module architecture.
- **Test scaffolding** with 6 passing tests against placeholder endpoints.

No business logic, database models, cloud integrations, or AI processing is implemented yet. The next steps are defined in [ROADMAP.md](ROADMAP.md).
