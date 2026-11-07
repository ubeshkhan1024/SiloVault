# SiloVault — Security Architecture & Threat Model

This document outlines the security principles, data protection controls, threat models,
and incident response protocols for **SiloVault**.

For overall project vision, see [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md). For system components, see [ARCHITECTURE.md](ARCHITECTURE.md). For development safety rules, see [AGENTS.md](../AGENTS.md).

> [!NOTE]
> **Status:** This document specifies **Proposed** and **Planned** security controls for future implementation. The repository currently contains documentation, rules, and configuration only.

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

## 3. Secrets Management

* **Zero Repository Secrets:** No credentials, API keys, service account keys, or OAuth client secrets may ever be committed to Git.
* **Storage Mechanisms (Planned):**
  - **Local Development / Node:** Managed via untracked `.env` files (excluded by `.gitignore`) or secure local OS credential stores (e.g., Windows Credential Manager).
  - **Production / Deployment:** Secure environment variables passed at runtime.
* **Redaction Rules:** Logging systems and AI prompt formatters must strip sensitive header keys, authorization tokens, and API credentials before writing logs or passing context to local models.

---

## 4. File & Input Security

Files uploaded remotely or processed locally must be treated as untrusted input.

* **File Validation:** Incoming files must be verified by content header (magic bytes) rather than relying solely on file extensions.
* **Path Traversal Defense:** Sanitization must be applied to all extracted filenames and user-provided paths to prevent directory traversal attacks (e.g., `../../`).
* **Non-Executable Sandbox:** Uploaded user files must never be executed by the local server or background processing scripts.
* **Immutability of Originals:** Original files in the Google Drive Mobile Inbox remain untouched until processing is complete and target destination writes are confirmed.
* **Temp File Hygiene:** Downloads to local disk use isolated temporary directories and are deleted immediately after processing or upon failure.

---

## 5. Local AI Security & Prompt Injection Defense

* **Network Binding:** Local AI engine (`Ollama`) and backend APIs (`FastAPI`) must bind exclusively to `localhost` (`127.0.0.1`). They must **never** be exposed directly to the public internet or external network interfaces without an approved secure proxy.
* **Untrusted Content Handling:** Text extracted via OCR or document parsing (e.g., PDFs, invoices, images) must be treated as untrusted data, not system instructions.
* **Prompt Injection Mitigations (Planned):**
  - Use structured input formats (e.g., JSON schemas, clear delimiter blocks) separating system prompts from user document text.
  - Constrain LLM outputs to validated JSON schemas (restricting actions to category/tag suggestions).
  - Enforce human approval for any consequential action suggested by the AI, mitigating malicious prompt injection embedded in uploaded documents.

---

## 6. Cloud & Network Security

* **Encrypted Transport:** All network communication (mobile app to cloud, local node to Google Drive API / Supabase) must use TLS 1.3 / HTTPS.
* **Least-Privilege Scopes:** Google OAuth requests must use minimal required permissions (e.g., per-app folder access or specific drive file scopes rather than full drive read/write where possible).
* **Token Lifecycle:** OAuth refresh tokens must be stored securely and refreshed automatically before expiration. Token revocation must be handled gracefully without corrupting local sync state.

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

* **Repository Hygiene:** `.gitignore` actively excludes `.env`, `*.db`, `uploads/`, `logs/`, `models/`, and `credentials/`.
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
