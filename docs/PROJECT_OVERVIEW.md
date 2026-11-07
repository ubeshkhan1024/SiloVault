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

Give a small number of trusted users (initially the two developers) a file management
system where **AI organization happens privately on a local machine**, while **files
and metadata are stored and synchronized in the cloud** — without ever putting the
originals at risk of silent loss or alteration.

## 2. Vision

The long-term vision, as a set of capabilities. Each item is labeled by how settled it
is.

| # | Capability | Status |
|---|------------|--------|
| 1 | A primary **Windows laptop** acts as the local AI processing node. | **Confirmed** |
| 2 | A **lightweight mobile application** for uploading files remotely. | **Confirmed** (in scope) |
| 3 | **Google Drive integration** for cloud file storage and synchronization. | **Confirmed** as the initial provider |
| 4 | A dedicated **mobile-upload inbox**. | **Confirmed** (in scope) |
| 5 | A **cloud metadata and account management** layer. | **Confirmed** (in scope) |
| 6 | **Local AI-powered** file classification, tagging, OCR, metadata extraction, and organization. | **Confirmed** as a capability |
| 7 | **File history, activity logs, and synchronization tracking.** | **Confirmed** (in scope) |
| 8 | **Collaboration between two developers** using GitHub. | **Confirmed** |
| 9 | Future support for **additional devices and storage providers**. | **Future possibility** |

## 3. Goals

- Keep AI processing **local and private** on the primary laptop.
- Let users upload files **from a phone, even when the laptop is offline**.
- **Automate organization** (classify, tag, OCR, extract metadata) while keeping
  humans in control of consequential changes.
- Make every operation **safe and reversible** — originals are never silently lost.
- Keep **metadata and file storage decoupled** so either can evolve independently.
- Build for **reliability, privacy, maintainability, and future scalability**.
- Support a clean **two-developer GitHub collaboration** workflow.

## 4. Non-goals (for now)

These are explicitly out of scope at the foundation stage. Some may become future
possibilities, but none are planned yet.

- A general-purpose, multi-tenant SaaS product.
- Exposing the local AI node to the public internet.
- Automatic, unattended reorganization of user files without approval.
- Replacing or duplicating Google Drive's own storage/sync features.
- Inventing concrete API endpoints, database schemas, or provider configurations
  before they are designed and agreed.

## 5. Stakeholders

- **Two developer-owners** who build and use the system, collaborating on GitHub.
  *(Confirmed.)*
- End users beyond the two developers. *(Future possibility — not designed for yet.)*

## 6. Terminology

| Term | Meaning |
|------|---------|
| **SiloVault** | The overall file management system. |
| **Local AI node** | The primary Windows laptop where AI processing runs. |
| **Mobile app** | The lightweight client used to upload files remotely. |
| **Mobile-upload inbox** | The dedicated intake area for files uploaded from the mobile app, awaiting processing. |
| **Cloud metadata layer** | The layer that stores file metadata and account information in the cloud, separate from file bytes. |
| **Storage provider** | A service that stores actual file bytes. Google Drive is the initial provider. |
| **Originals** | The unmodified files as uploaded. Treated as immutable by default. |
| **Organization** | Classifying, tagging, OCR, metadata extraction, and arranging files. |
| **Consequential change** | Any operation that could meaningfully move, rename, reorganize, or discard user files and therefore requires human approval. |

## 7. Current status

The project is at the **foundation stage**: the repository holds documentation,
development rules (see [../AGENTS.md](../AGENTS.md)), configuration
([../.gitignore](../.gitignore)), and an **application skeleton**:

- **FastAPI backend** with 14 registered API routes under `/api/v1/` (health, system status, files, uploads, sync, AI processing, approvals, activity). All endpoints except `GET /api/v1/health` return placeholder responses.
- **PWA frontend shell** with 6 navigable views (Dashboard, Files, Uploads, Sync, Activity, Settings), service worker for offline caching, and ES module architecture.
- **Test scaffolding** with 6 passing tests against placeholder endpoints.

No business logic, database models, cloud integrations, or AI processing is implemented yet. The next steps are defined in [ROADMAP.md](ROADMAP.md).
