"""Domain types for the storage abstraction layer.

These are plain dataclasses used to carry file metadata between the provider
adapters and the API/service layers. They intentionally avoid importing any
SQLAlchemy, FastAPI, or ORM symbols so they remain portable across the
boundaries of the application.

No authentication or synchronization types live here — those are deferred
to M3 alongside ``AuthState``, ``ProviderSyncStatus``, and ``DeltaResult``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True, eq=True)
class ProviderFileRecord:
    """Normalized file record returned by every storage adapter.

    Fields with no default appear first so instances can be constructed
    positionally without ambiguity; optional fields carry sensible defaults.

    Because ``provider_metadata`` is a ``dict``, instances are **not** hashable
    and cannot be placed in sets or used as dictionary keys. This is acceptable —
    the record's primary role is as a value object carried between layers, not as
    a set element or dict key. Hashability can be restored later if the field type
    is changed to an immutable container.

    Attributes
    ----------
    provider_file_id:
        Native provider-assigned identifier (UUID, handle, path-string, etc.).
        Never interpreted by SiloVault — treated as an opaque string.
    name:
        Original user-facing filename.
    logical_path:
        Logical path within the provider's namespace (e.g. ``/Invoices/2026.pdf``).
    mime_type:
        MIME type detected or assigned at ingest time (e.g. ``application/pdf``).
    size_bytes:
        File size in bytes.
    sha256:
        SHA-256 hex digest computed locally over the plaintext bytes.
        ``None`` until the file has been downloaded or processed.
    provider_metadata:
        Free-form provider-specific metadata (web-view links, native hashes,
        share flags, revision counts, …). Defaults to an empty dict.
    modified_at:
        Last-modified timestamp reported by the provider. ``None`` when
        unavailable (common for local storage where mtime is not tracked).
    """

    provider_file_id: str
    name: str
    logical_path: str
    mime_type: str
    size_bytes: int
    sha256: Optional[str] = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    modified_at: Optional[datetime] = None
