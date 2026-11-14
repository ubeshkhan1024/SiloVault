# SiloVault

> A privacy-focused, AI-powered unified cloud storage management platform.

**Status:** Milestone 3.2 (Google Drive Integration & File Management) complete. SQLite database with Alembic migrations, unified file metadata model, real CRUD endpoints (including `PATCH /files/{id}/rename` and `PATCH /files/{id}/move`), OAuth-based Google Drive connectivity, storage adapter registry, and background deletion recovery service are all implemented and verified.


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
├── alembic.ini          # Alembic migration configuration
├── .env.example         # Environment variable template
├── backend/             # FastAPI application
│   ├── main.py          # App entry point
│   ├── config.py        # Pydantic settings
│   ├── database.py      # SQLAlchemy engine & session
│   ├── dependencies.py  # FastAPI dependencies
│   ├── api/v1/          # Versioned API routes (14 endpoints)
│   ├── models/          # SQLAlchemy models
│   ├── schemas/         # Pydantic schemas
│   ├── services/        # Business logic (placeholder)
│   ├── storage/         # Cloud storage adapters (placeholder)
│   └── ai/              # Local AI integration (placeholder)
├── frontend/            # PWA shell
│   ├── index.html       # Main HTML with 6 views
│   ├── css/style.css    # Styling
│   ├── js/              # ES modules (app, api, dashboard, files, uploads, activity, settings)
│   ├── manifest.json    # PWA manifest
│   └── service-worker.js # Offline shell caching
├── docs/                # Project documentation
├── tests/               # Pytest suite
├── data/                # Local data directory (gitignored)
├── uploads/             # Upload staging (gitignored)
├── models/              # Model weights (gitignored)
├── scripts/             # Utility scripts
├── scratch/             # Temporary files (gitignored)
└── alembic/             # Alembic migrations (version-controlled)
    ├── env.py           # Migration environment — reads app settings at runtime
    └── versions/        # Migration revision scripts (commit these)
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
# 93 tests pass (1 skipped on Windows for platform-specific symlink check)
```

## Database migrations (Alembic)

SiloVault uses **Alembic** for version-controlled SQLite schema migrations. Migrations are **explicit, never automatic** — the FastAPI app does not run them on startup. This is intentional: migrations modify user data and must be reviewed before execution.

### Commands

Run all commands from the repository root with the virtual environment **active**:

```powershell
# Inspect the current revision of the live database
alembic current

# Show the full migration history
alembic history

# Upgrade the live database to the latest migration
alembic upgrade head

# Create a new migration after changing a model
alembic revision --autogenerate -m "describe the change"

# Downgrade one step (for local testing only — never on shared data)
alembic downgrade -1

# Reset to the very first migration
alembic downgrade base
```

### Workflow for adding or changing a model

1. Edit the SQLAlchemy model in `backend/models/`.
2. Run `alembic revision --autogenerate -m "description"` to scaffold the migration.
3. **Review the generated script** in `alembic/versions/` — do not blindly accept autogenerate output, especially for type changes or index modifications on SQLite.
4. Commit the migration file.
5. Run `alembic upgrade head` on any environment that needs the update.

### Temp-database workflow (autogenerate & tests)

Alembic reads `settings.database_url`, which defaults to `silo_vault.db`. To run autogenerate or migration tests against an isolated temporary database without touching the real one, override via the `DATABASE_PATH` environment variable — this is the same mechanism `pydantic-settings` uses:

```powershell
# Windows PowerShell
$env:DATABASE_PATH="test_migration.db"
alembic revision --autogenerate -m "my change"
```

All Alembic tests (`tests/test_alembic.py`) use this pattern so they never affect the production database.

### Backup expectation

Before running `alembic upgrade head` on a database containing real data, make a copy:

```powershell
Copy-Item silo_vault.db "silo_vault.db.bak_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
```

The `.db` files are excluded from Git by `.gitignore`; keep at least one recent backup before any schema-modifying operation.

---

To contribute:
1. Read [AGENTS.md](AGENTS.md) and the docs under [docs/](docs/).
2. Pick a single, well-scoped task from [docs/ROADMAP.md](docs/ROADMAP.md).
3. Explain the planned file changes before implementing them.
4. Test the relevant functionality and report real results — never invented ones.

## License

Not yet decided. See [docs/ROADMAP.md](docs/ROADMAP.md) open questions.
