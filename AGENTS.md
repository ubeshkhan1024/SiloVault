# AGENTS.md — Development Rules for SiloVault

These rules apply to **all** work in this repository — human developers and AI
coding agents alike. They are strict and non-negotiable. When a rule conflicts with
speed or convenience, the rule wins.

## 1. General working rules

1. **Inspect before modifying.** Read and understand the existing files, behavior,
   and documentation before changing anything. Never modify code or docs you have
   not looked at.
2. **Work on one feature at a time.** Keep changes scoped and isolated. Do not bundle
   unrelated changes into a single change set.
3. **Preserve working functionality.** Do not break what already works. If a change
   risks existing behavior, call it out explicitly and protect the old behavior.
4. **Explain planned file changes before implementation.** Before writing code, state
   which files will be created, modified, or deleted and why. Wait for confirmation
   when the change is non-trivial.
5. **Ask before major architectural changes.** Any change that affects the overall
   architecture, data model, storage layout, security posture, or provider
   integrations requires explicit approval first.
6. **Never execute destructive operations without approval.** No deleting data,
   dropping tables, force-pushing, overwriting files, or similar — without explicit
   sign-off.
7. **Test relevant functionality after changes.** After a change, exercise the affected
   functionality and report what was actually tested.
8. **Never invent test results.** Only report real, observed outcomes. If something was
   not tested, say so. Do not claim a pass that did not happen.
9. **Protect credentials, databases, and original files.** Treat secrets, local
   databases, and user originals as protected. Never log, commit, print, or copy them
   into the repository or into outputs.
10. **Keep documentation updated.** If a change affects behavior, architecture, or
    decisions, update the relevant docs in the same change so the docs never drift.

## 2. Project-specific safety rules

These reflect SiloVault's core product principles (see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):

- **Originals are immutable by default.** Nothing in the system — including AI — may
  silently delete or overwrite an original file. Organization must operate on copies,
  metadata, or reversible moves.
- **Human approval for consequential changes.** Any operation that would meaningfully
  reorganize, rename, move, or discard user files must require explicit human approval.
- **Reversibility first.** Design operations to be undoable. Prefer moves that record
  their source, copies that keep the original, and metadata that can be rolled back.
- **Local AI stays local.** The AI processing node runs on the primary laptop and must
  never be exposed directly to the public internet. Do not add public endpoints,
  tunnels, or public ports for it without an approved, reviewed design.
- **Separate metadata from storage.** Do not couple the cloud metadata/account layer to
  the physical file storage in a way that breaks their independence.

## 3. Boundaries and hygiene

- **No secrets in the repository.** Never add credentials, API keys, tokens, or
  connection strings to tracked files. Use the patterns in
  [docs/SECURITY.md](docs/SECURITY.md).
- **Respect `.gitignore`.** Do not commit virtual environments, caches, local
  databases, database backups, uploaded user files, model weights, temporary files,
  or logs that may contain private information.
- **Stay inside the workspace.** Do not modify files outside the current project
  workspace unless explicitly asked.
- **Prefer small, reviewable changes.** Favor diffs that a second developer can read
  and verify quickly.

## 4. Definition of done

A task is only done when:

- The planned changes were explained and (where required) approved.
- The change is scoped to the task and preserves prior working behavior.
- Relevant functionality was tested and the real results are reported.
- Documentation is updated to match the new behavior.
- No secrets, user data, or original files were exposed or committed.
