# SiloVault

> A privacy-focused, AI-powered unified cloud storage management platform.

**Status:** Foundation phase complete. Application skeleton implemented with FastAPI backend (14 registered routes) and PWA frontend shell. All endpoints except `GET /api/v1/health` are placeholder stubs.

## What SiloVault is

SiloVault is a privacy-first, unified cloud storage platform where **AI processing happens locally** on a
primary machine (Windows laptop), while **files and metadata live across user-connected cloud storage providers**
(initial support: Google Drive; planned: Microsoft OneDrive, Dropbox, Box, MEGA, and others). A lightweight mobile app
lets you upload files remotely to cloud inboxes; and a local AI pipeline classifies, tags, OCRs, extracts
metadata from, and organizes files across connected storage accounts from one central interface.

The guiding idea: keep the intelligence local and private, provide a single unified pane of glass across multiple
cloud storage providers via a modular provider-adapter architecture, keep originals safe and reversible, and
ensure AI never alters or deletes user files without explicit human approval.

## Core principles

These are confirmed project decisions. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
and [docs/SECURITY.md](docs/SECURITY.md) for the full treatment.

- **Unified Multi-Cloud Management.** Browse, search, organize, and manage files across multiple cloud storage
  providers (Google Drive, OneDrive, Dropbox, Box, MEGA) through a single interface without vendor lock-in.
- **Provider-Independent Architecture.** Storage adapters implement a common interface with explicit capability
  negotiation, keeping the core system decoupled from any specific provider.
- **Originals are sacred.** AI must never silently delete or overwrite original files.
- **Human approval** is required for organization changes that could be consequential (moves, renames, deletions).
- **Reversibility.** Operations should be undoable wherever possible with audit logging and rollback support.
- **Local AI.** AI processing (via Ollama) stays on the primary local node and is never exposed directly to the public internet.
- **Separation of concerns.** Cloud metadata, account management, and physical file storage are distinct, decoupled layers.
- **Offline-tolerant uploads.** Remote uploading works even when the primary laptop is offline.
- **No secrets in Git.** Credentials and OAuth tokens never live in the repository or leak to the frontend.

## Repository structure

```
SiloVault/
├── AGENTS.md            # Strict rules for AI coding agents working in this repo
├── README.md            # This file
├── .gitignore           # Excludes secrets, databases, user files, models, logs, etc.
├── requirements.txt     # Python dependencies
├── pyproject.toml       # Project metadata and build config
├── .env.example         # Environment variable template
├── backend/             # FastAPI application
│   ├── main.py          # App entry point
│   ├── config.py        # Pydantic settings
│   ├── database.py      # SQLAlchemy engine & session
│   ├── dependencies.py  # FastAPI dependencies
│   ├── api/v1/          # Versioned API routes (14 endpoints)
│   ├── models/          # SQLAlchemy models (placeholder)
│   ├── schemas/         # Pydantic schemas (placeholder)
│   ├── services/        # Business logic (placeholder)
│   ├── storage/         # Cloud storage adapters (placeholder)
│   └── ai/              # Local AI integration (placeholder)
├── frontend/            # PWA shell
│   ├── index.html       # Main HTML with 6 views
│   ├── css/style.css    # Styling
│   ├── js/              # ES modules (app, api, dashboard, files, uploads, settings)
│   ├── manifest.json    # PWA manifest
│   └── service-worker.js # Offline shell caching
├── docs/                # Project documentation (7 files)
├── tests/               # Pytest suite (6 tests)
├── data/                # Local data directory (gitignored)
├── uploads/             # Upload staging (gitignored)
├── models/              # Model weights (gitignored)
├── scripts/             # Utility scripts
└── scratch/             # Temporary files (gitignored)
```

## Documentation conventions

Throughout the docs, statements are labeled by status so that nothing is mistaken
for a final decision:

- **Confirmed** — a project decision that is settled.
- **Proposed** — an architectural choice under consideration, not yet finalized.
- **Future possibility** — an idea that still requires discussion.

The docs deliberately do **not** invent API endpoints, database schemas, credentials,
provider configurations, or implementation details that have not been decided.

## Development workflow

SiloVault is developed by **two developers** collaborating on **GitHub**. The
strict working rules for anyone (human or AI agent) modifying this repository are
defined in [AGENTS.md](AGENTS.md). In short: inspect before modifying, work on one
feature at a time, explain changes before implementing, ask before major
architectural changes, and never run destructive operations without approval.

A full two-developer collaboration guide — branch strategy, PR flow, setup for
Windows and macOS, and security rules — lives in
[docs/COLLABORATION.md](docs/COLLABORATION.md). New contributors should read it
first.

## Getting started

The application skeleton can be run locally:

**Backend:**
```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload
# Health check: http://127.0.0.1:8000/api/v1/health
# API docs: http://127.0.0.1:8000/docs
```

**Frontend (PWA):**
```bash
npx serve frontend -l 3000
# or: python -m http.server 3000 -d frontend
# Open http://localhost:3000
```

**Testing:**
```bash
pytest tests/
# 6 tests pass against placeholder endpoints
```

To contribute:
1. Read [AGENTS.md](AGENTS.md) and the docs under [docs/](docs/).
2. Pick a single, well-scoped task from [docs/ROADMAP.md](docs/ROADMAP.md).
3. Explain the planned file changes before implementing them.
4. Test the relevant functionality and report real results — never invented ones.

## License

Not yet decided. See [docs/ROADMAP.md](docs/ROADMAP.md) open questions.
