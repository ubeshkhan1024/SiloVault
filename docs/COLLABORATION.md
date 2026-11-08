# SiloVault — Collaboration Workflow

> Two-developer guide for working together on this repository on **GitHub**.
> Covers branch strategy, pull-request flow, setup steps, and rules that apply
> regardless of operating system.

## Prerequisites

Both developers share these baseline requirements:

| Requirement | Why |
|-------------|-----|
| GitHub account | Code review, pull requests, issue tracking |
| [Git](https://git-scm.com) installed | Source control; verify with `git --version` |
| [Python 3.11+](https://www.python.org/downloads/) installed | Backend runs on Python 3.11+ (`sys.version_info >= (3, 11)`) |
| A text editor or IDE | VS Code, PyCharm, Neovim, etc. — whatever you prefer |
| Access to the shared repo | Invited as collaborator on the GitHub repository |

Node.js is needed only if one developer wants to serve the frontend with
`npx serve`; the Python fallback works everywhere Python 3.11+ is available.

---

## 1. Git Branch Strategy

Every change lands through a named branch. No feature work goes directly into
`main`.

| Pattern | Purpose | Example |
|---------|---------|---------|
| `main` | Stable shared branch. Never push direct commits here. | — |
| `feature/<name>` | New functionality. One logical unit per branch. | `feature/google-drive-sync` |
| `fix/<name>` | Bug fixes for reported issues. | `fix/health-endpoint-missing-field` |
| `docs/<name>` | Documentation updates. | `docs/collaboration-guide` |
| `chore/<name>` | Maintenance: dependencies, scripts, CI config. | `chore/add-pytest-asyncio` |

### Rules

1. **Never commit straight to `main`.** Use a feature branch even for tiny
   changes so there is a review step.
2. **One branch per task.** If two unrelated fixes are needed, open two
   separate branches rather than mixing concerns.
3. **Name branches clearly.** Use lowercase, hyphens, and a verb-noun form.
   Avoid names like `my-changes` or `work-in-progress`.
4. **Keep `main` deployable at all times.** Before opening a PR, rebase
   or merge the latest `main` onto your branch so conflicts are minimised.

---

## 2. Pull Request Workflow

The workflow below is designed for two developers who review each other's
work. Every code change follows the same steps.

### Step-by-step

```
Developer A                           Developer B
───────────                           ───────────
1. git pull origin main              1. git pull origin main
2. git checkout -b feature/x         2. (works on own branch)
3. Make focused changes               3. Wait for review / work separately
4. Run relevant checks locally        4. git fetch origin && git pull
5. git push -u origin feature/x      5. Review opens automatically
6. Open PR on GitHub                  6. Review comments & feedback
7. Address feedback                   7. Request changes if needed
8. Re-test after feedback             8. Approval ✓
9. Merge via GitHub UI                9. Squash or merge on approval
10. Delete the feature branch          10. git pull origin main on both sides
```

### PR checklist (add to every PR description)

```markdown
## What
<!-- One sentence describing the change. -->

## Why
<!-- Business reason, not technical detail. -->

## How tested
<!-- Commands run and real results observed. Never invent test results. -->

## Files changed
<!-- List every file created, modified, or deleted. -->
```

### Review expectations

Each developer reviews the other's PR against these criteria:

- **Correctness:** Does the code do what it claims? Are the tests real, not
  faked?
- **Safety:** No secrets leaked? `.env` untouched? Original files preserved?
- **Scope:** Is the change limited to what was agreed, or did something
  drift beyond the brief?
- **Documentation:** Did docs and README reflect any behaviour change?
- **Architecture:** Does it violate AGENTS.md rules or established design?

Neither developer merges their own PR. The reviewing developer performs the
merge after approval.

---

## 3. Development Setup

The setup steps below are identical in intent but differ in shell syntax
between Windows and macOS.

### Clone the repository

```bash
# Both platforms
git clone https://github.com/<owner>/SiloVault.git
cd SiloVault
```

### Verify Python availability

```powershell
# Windows 11 PowerShell
python --version
# Expected: Python 3.11.x or newer
```

```bash
# macOS Terminal
python3 --version
# or: python --version  (if Homebrew python is preferred-path)
# Expected: Python 3.11.x or newer
```

> **macOS note:** Apple Silicon Macs ship with no system Python. Install one
> via [pyenv](https://github.com/pyenv/pyenv), [Homebrew](https://brew.sh/),
> or the official installer from python.org. Then set a default alias
> (`alias python=python3`) in `~/.zshrc` so the same commands work on both
> systems.

### Create and activate a virtual environment

```powershell
# Windows 11 PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If you get an execution-policy error, run once:
# Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

```bash
# macOS Terminal
python3 -m venv .venv
source .venv/bin/activate
```

Verify activation succeeded:

```bash
which python        # should point inside .venv
which pip           # should point inside .venv
```

### Install project dependencies

```bash
# Both platforms (virtual environment must be active)
pip install -r requirements.txt
# or, equivalent editable install (dev extras included):
# pip install -e ".[dev]"
```

Verified packages (installed versions on Windows at time of writing):

| Package | Version |
|---------|---------|
| fastapi | 0.162.2 |
| uvicorn | 0.54.0 |
| pydantic | 2.x |
| pydantic-settings | 2.x |
| sqlalchemy | 2.x |
| pytest | 9.2.0 |
| httpx | 0.28.1 |
| pytest-asyncio | 0.23.x |

### Start the backend (port 8000)

From the repository root, with the virtual environment **active**:

```powershell
# Windows 11 PowerShell
python -m uvicorn backend.main:app --reload
```

```bash
# macOS Terminal
python -m uvicorn backend.main:app --reload
```

> The `--reload` flag restarts the server when source files change. It is
> convenient during development but adds ~500 ms to response latency. Omit
> it for production use.

Health check (open in browser or curl):

```
http://127.0.0.1:8000/api/v1/health
http://127.0.0.1:8000/docs            # Swagger interactive API docs
```

> **CORS note:** The backend accepts requests from `http://localhost:3000`
> and `http://127.0.0.1:3000` only. Do not add arbitrary origins; see
> `backend/main.py` if you need to extend this list for a new setup.

### Start the frontend (port 3000)

The frontend is a plain static PWA (HTML + CSS + ES modules). It requires
no build step. Two serving options are available.

#### Option A — `npx serve` (preferred, richer MIME types)

Requires Node.js (`node` and `npx` on PATH). Available on both Windows and
macOS after installing [nodejs.org](https://nodejs.org).

```powershell
# Windows 11 PowerShell
npx serve frontend -l 3000
```

```bash
# macOS Terminal
npx serve frontend -l 3000
```

Open `http://localhost:3000` in a browser.

#### Option B — Python fallback (no Node required)

```powershell
# Windows 11 PowerShell
python -m http.server 3000 -d frontend
```

```bash
# macOS Terminal
python -m http.server 3000 -d frontend
```

> Caveat: Python's built-in HTTP server does not set correct MIME types for
> JavaScript and may refuse to serve `.js` files when the browser demands
> `application/javascript`. The ES module imports in `frontend/js/app.js`
> expect proper MIME types. If you see CORS or MIME-type errors, switch to
> Option A.

### Stopping servers

Both servers respond to `Ctrl+C` in their terminal window. If a process
escapes the foreground, find and kill it:

```powershell
# Windows 11 PowerShell
Get-Process python | Where-Object { $_.CommandLine -match 'uvicorn' } | Stop-Process
netstat -ano | Select-String ':8000'   # confirm port is free
netstat -ano | Select-String ':3000'
```

```bash
# macOS Terminal
pkill -f "uvicorn"
lsof -i :8000
lsof -i :3000
```

---

## 4. Collaboration Rules

These rules govern day-to-day interaction between the two developers. They
complement the AI-agent rules in [AGENTS.md](../AGENTS.md) — human operators
must also obey them.

### Scope and coordination

- **Agree on API contract changes before editing related code.** If Developer A
  plans to add a field to a request schema, Developer B must know before
  touching the corresponding frontend code or endpoint stub.
- **Avoid simultaneously editing the same file.** Claim files via the branch
  name or leave a quick chat message ("working on health.py") before you
  start. Conflicts on the same file are solvable but waste time.
- **Communicate major architectural changes.** Any shift to the data model,
  storage provider, authentication method, or deployment topology requires
  explicit alignment before implementation begins. See AGENTS.md §1.5.

### Pull-request discipline

- Keep PRs focused — one feature, one fix, one doc topic.
- Write a short PR description explaining **what** and **why**, not just the
  diff.
- Report actual test results. Never claim a pass that did not happen.
- Respond to review feedback promptly. Push incremental fixes to the same
  branch; the PR updates automatically.

### Commit hygiene

- Use clear, imperative commit messages: `feat: add health endpoint status`,
  not `update stuff`.
- Do not mix unrelated changes in a single commit.
- Squash minor fixup commits before merging.

---

## 5. Security Rules

Security rules from [AGENTS.md](../AGENTS.md) and [docs/SECURITY.md](SECURITY.md)
apply equally to both developers. Key points summarised here:

1. **Never commit `.env`, credentials, API keys, databases, uploaded user
   files, or model weights.** These are excluded by `.gitignore`; committing
   them would leak sensitive data publicly.
2. **Keep `.env.example` current.** When a supported configuration field is
   added to `backend/config.py`, add the matching variable to
   `.env.example` in the same commit so the template stays accurate.
3. **Do not expose local services to the public internet.** The backend binds
   to `127.0.0.1` (loopback only). The frontend uses `localhost:3000`.
   Local Ollama on `127.0.0.1:11434` is never tunnelled or forwarded.
4. **Preserve user originals.** Consequential file operations (move, rename,
   discard) require explicit human approval before execution.
5. **Review every PR for secret leakage.** Check diffs for bare strings that
   look like tokens, passwords, client IDs, or private keys. If found, do
   not merge until the credential is removed or redacted.

---

## 6. Cross-platform Notes

### Line endings (`.gitattributes`)

The repository contains `.gitattributes` with `* text=auto`. This tells Git
to convert CRLF ↔ LF on checkout depending on the platform, so both
developers see LF-ending files in their editors and no spurious diffs arise
from line-ending differences.

### Virtual environment paths

Windows uses backslashes and `Scripts\python.exe`; macOS uses forward slashes
and `bin/python`. The commands above are written for each shell. Adapt as
needed but keep the conceptual steps identical.

### Optional tools (not required)

| Tool | Platform | Required? | Note |
|------|----------|-----------|------|
| `npx` / Node.js | Windows, macOS | No | Only for `npx serve` frontend option |
| `pyenv` | macOS, Linux | No | Helpful for switching Python versions |
| VS Code + extensions | Both | No | Language support, linting, formatter |
| `black`, `ruff` | Both | No | Not yet pinned in requirements; consider adding for consistency |

---

## 7. Decisions Requiring Developer Agreement

Before the first collaborative session, discuss and agree on these items:

| Decision | Options | Status |
|----------|---------|--------|
| Branch protection on `main` | Require PR + review / allow force-push | Pending |
| Merge style for approved PRs | Squash / rebase / merge-commit | Pending |
| Who triggers release builds | Either developer / rotate weekly | Pending |
| Test-gate on merge | Require all tests green / human judgement OK | Pending |
| Default issue tracker usage | Labels, milestones, assignees | Pending |

Record decisions in [docs/ROADMAP.md](ROADMAP.md) under "Open questions" or a
dedicated "Decisions log" entry so both developers have a shared reference.
