# SiloVault — Security Architecture & Threat Model

This document outlines the security principles, data protection controls, threat models,
and incident response protocols for **SiloVault**.

For overall project vision, see [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md). For system components, see [ARCHITECTURE.md](ARCHITECTURE.md). For development safety rules, see [AGENTS.md](../AGENTS.md).

> [!NOTE]
> **Status:** This document specifies **Proposed** and **Planned** security controls for future implementation. The repository currently contains documentation, rules, configuration, and an **application skeleton** (FastAPI backend with 14 registered routes, PWA frontend shell with 6 views). All non-health endpoints are placeholder stubs with no business logic.

---

## 1. Core Security Principles

1. **Privacy by Design:** Local AI processing ensures that file contents and raw extractions never leave the primary laptop for model inference.
2. **Least Privilege:** Cloud credentials, API scopes, and system permissions are limited to the minimum necessary access required for file storage and metadata synchronization.
3. **Secure Defaults:** Services listen on loopback interfaces (`127.0.0.1`), secrets are excluded via `.gitignore`, and dangerous actions require explicit opt-in or human confirmation.
4. **Data Minimization:** Store only essential file metadata (hashes, sizes, MIME types, categories, tags) in index layers; do not duplicate file contents unnecessarily.
5. **Human Oversight:** Consequential operations (renaming, moving, deleting) require human approval before execution.

---

## 2. Authentication & Account Management

> [!IMPORTANT]
> No authentication mechanisms are currently implemented in code.

* **Proposed Account Model:** User authentication for the mobile client and Cloud Control Plane will rely on modern identity providers (e.g., Supabase Auth or OAuth 2.0).
* **Session Handling:** Mobile sessions will utilize short-lived JWT access tokens accompanied by securely stored refresh tokens.
* **Account Isolation:** Multi-user data structures (if introduced in future phases) must enforce strict database Row Level Security (RLS) to prevent cross-account data access.
* **Password Policy:** Plaintext passwords will never be stored or logged. Any authentication service must use strong password hashing algorithms (e.g., Argon2id or bcrypt).

---

## 3. Secrets & Provider Credentials Management

* **Zero Repository Secrets:** No credentials, API keys, service account keys, or OAuth client secrets may ever be committed to Git.
* **Storage Mechanisms (Planned):**
  - **Local Development / Node:** Managed via untracked `.env` files (excluded by `.gitignore`) or secure local OS credential stores (e.g., Windows Credential Manager / macOS Keychain).
  - **Multi-Cloud OAuth Tokens:** Refresh tokens for Google Drive, Microsoft OneDrive, Dropbox, and Box must be stored securely encrypted in the local database or local keychain.
  - **MEGA Key Management & E2EE Handling:** Unlike standard OAuth providers, MEGA relies on user-controlled client-side encryption. User master keys and derived session keys must be held strictly in local encrypted storage (OS credential store or encrypted local database). SiloVault must strictly respect MEGA's zero-knowledge model: it must never assume it can inspect cloud file contents without obtaining the decrypted content through an authorized client-side workflow. **Plaintext passwords must never be stored on disk, logged, or cached insecurely. Master keys must never be exported to files or transmitted outside the local node; only encrypted session credentials may be retained for automatic re-authentication.**
* **Frontend Credential Isolation:** Provider client secrets, access tokens, refresh tokens, and encryption keys are strictly maintained on the backend local node. **Tokens and keys are never exposed to the frontend client.**
* **Redaction Rules:** Logging systems and AI prompt formatters must strip sensitive header keys, authorization tokens, encryption keys, and API credentials before writing logs or passing context to local models.

---

## 4. File & Input Security

Files uploaded remotely or processed locally across any cloud provider must be treated as untrusted input.

* **File Validation:** Incoming files must be verified by content header (magic bytes) rather than relying solely on file extensions.
* **Path Traversal Defense:** Sanitization must be applied to all extracted filenames and user-provided paths to prevent directory traversal attacks (e.g., `../../`).
* **Non-Executable Sandbox:** Uploaded user files must never be executed by the local server or background processing scripts.
* **Immutability of Originals:** Original files in cloud intake inboxes (Google Drive, OneDrive, Dropbox, Box, MEGA) remain untouched until processing is complete and target destination writes are confirmed.
* **Temp File Hygiene & Data Remanence:** Downloads and decrypted plaintext payloads (such as decrypted MEGA streams) used for local AI processing and checksumming must reside in an isolated sandbox temporary directory with restricted filesystem permissions. Temporary files must be unlinked promptly upon processing completion or failure. Note: Standard filesystem file unlinking removes directory entries but does not guarantee immediate secure overwrite or cryptographic erasure on physical disk media due to OS file caching, flash translation layers, and wear leveling. High-security environments requiring zero data remanence must evaluate encrypted temp RAM drives or explicit zeroing wrappers where appropriate.

---

## 5. Local AI Security & Prompt Injection Defense

* **Network Binding:** Local AI engine (`Ollama`) and backend APIs (`FastAPI`) must bind exclusively to loopback (`127.0.0.1`). They must **never** be exposed directly to the public internet or external network interfaces without an approved secure proxy.
* **Untrusted Content Handling:** Text extracted via OCR or document parsing (e.g., PDFs, invoices, images) must be treated as untrusted data, not system instructions.
* **Prompt Injection Mitigations (Planned):**
  - Use structured input formats (e.g., JSON schemas, clear delimiter blocks) separating system prompts from user document text.
  - Constrain LLM outputs to validated JSON schemas (restricting actions to category/tag suggestions).
  - Enforce human approval for any consequential action suggested by the AI, mitigating malicious prompt injection embedded in uploaded documents.
  - AI is strictly prohibited from silently deleting or overwriting files across any connected storage provider.

---

## 6. Cloud & Network Security

* **Encrypted Transport:** All network communication (mobile app to cloud, local node to Google Drive API, Microsoft Graph, Dropbox API, Box API, MEGA API, or Supabase) must use TLS 1.3 / HTTPS.
* **Least-Privilege Scopes:** OAuth requests must request only minimal required permissions (e.g., per-app folder access or specific drive file scopes rather than full tenant/drive read/write where possible).
* **Token Lifecycle:** OAuth refresh tokens must be stored securely and refreshed automatically before expiration. Token revocation must be handled gracefully without corrupting local sync state across providers.
* **SDK Licensing & Boundary Isolation:** The official MEGA SDK is licensed under the permissive **BSD 2-Clause "Simplified" License**, which does not impose copyleft obligations on SiloVault's proprietary codebase. *Note:* MEGAcmd and any third-party dependencies bundled with the SDK may have their own licenses (e.g., AGPLv3 for some components) and must be audited individually before integration. Do not assume all MEGA-related components share the same license. **Process isolation is an optional security and reliability design decision**, not a legal requirement: running MEGA operations in a separate worker process or via a clean IPC boundary (e.g., CLI subprocess or separate micro-service) limits the blast radius of SDK crashes, simplifies credential handling, and keeps the MEGA cryptographic boundary physically separate from SiloVault's core application code.

---

## 7. Database & Metadata Security

* **Local Database (SQLite):**
  - SQLite database files must reside in protected local user directory structures.
  - Database access must use parameterized queries (ORMs / prepared statements) to prevent SQL injection vulnerabilities.
* **Cloud Metadata (Proposed Supabase):**
  - Enforce Row Level Security (RLS) policies on metadata tables so users can only access their own file metadata records.
  - Database administrative keys must never be embedded in client-facing applications.

---

## 8. GitHub & Developer Safety

* **Repository Hygiene:** `.gitignore` actively excludes `.env`, `*.db`, `/uploads/`, `/logs/`, `/models/`, `/secrets/`, `/credentials/`, and other runtime artifacts at the repository root.
* **Dependency Auditing:** Dependencies introduced to the project must be audited for known vulnerabilities (e.g., using `pip-audit` or GitHub Dependabot).
* **Code Review:** All changes affecting authentication, file handling, network bindings, or secret storage require review against [AGENTS.md](../AGENTS.md).

---

## 9. Threat Model Matrix

| Threat | Impact | Proposed / Planned Mitigation | Status |
|---|---|---|---|
| **Exposing Local AI Endpoint to Internet** | Remote attackers execute unauthorized inference or access local host services. | Enforce strictly local loopback binding (`127.0.0.1`). No public tunnels or port forwards. | **Planned** |
| **Leaked OAuth Credentials in Git** | Attackers gain unauthorized access to cloud storage account. | Enforce `.gitignore` rules; use automated secret scanning tools before pushing code. | **Planned** |
| **Indirect Prompt Injection via Document OCR** | Malicious document text instructs AI to output unauthorized actions or renames. | Isolate system instructions from document context; mandate human approval for file operations. | **Planned** |
| **Path Traversal in File Renaming** | Attacker crafts filename containing `../` to write files to arbitrary system directories. | Strict path sanitization; restrict file writes to designated sandbox directories. | **Planned** |
| **Compromised Mobile Device Token** | Attacker uploads malicious files to Mobile Inbox. | Limit Mobile Inbox scope; validate file magic bytes; enforce user review on local node. | **Proposed** |
| **Compromised MEGA Master Key** | Attacker decrypts private files stored on MEGA. | Master key stored in encrypted OS keyring/database; never transmitted to frontend; no cloud sync of master keys. | **Planned** |
| **Accidental Overwrite / Deletion of Originals** | Permanent loss of user original files. | Enforce immutable originals policy; require copy-before-delete; store operation history for rollback. | **Planned** |
| **SQL Injection in Metadata Queries** | Unauthorized data reading, modification, or local database corruption. | Mandatory use of parameterized queries and ORM abstraction layers. | **Planned** |

---

## 10. Incident Response & Recovery Guidelines

In the event of a security incident or credential exposure:

1. **Credential Exposure (OAuth Keys, Tokens, API Keys):**
   - Immediately revoke the affected client ID, secret, or session token in the provider console (e.g., Google Cloud Console / Supabase Dashboard).
   - Rotate all compromised credentials and update local `.env` configuration.
   - Audit Git commit history to ensure secrets were not pushed; purge history if necessary.
2. **Unauthorized Access Detection:**
   - Terminate active user sessions in the Cloud Control Plane.
   - Review audit logs in local SQLite database to identify impacted files or metadata changes.
3. **Data Loss / Accidental Overwrite:**
   - Utilize local operation history logs to execute single-click rollback for file renames/moves.
   - Restore file copies from Google Drive version history or local backup directory if available.
