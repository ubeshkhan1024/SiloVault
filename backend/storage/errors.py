"""Storage-layer exception hierarchy for SiloVault.

All exceptions inherit from :class:`StorageError`, providing a single catch
point for callers while still allowing granular handling of specific failure
modes.  Each concrete class documents when it is raised so that endpoint code
can map it to the correct HTTP status without importing FastAPI or any
provider SDK.

Design constraints
------------------
* No imports beyond the standard library — this module must remain
  independent of FastAPI, SQLAlchemy, httpx, and every cloud SDK.
* When an adapter wraps an underlying exception, the original is attached as
  ``__cause__`` (via ``raise … from exc``) so the full traceback is preserved.
* Constructors accept optional positional/context keywords so callers can
  annotate errors with provider name, file identifier, or retry-after delay
  when such information is available.
"""

from __future__ import annotations

from typing import Optional


class StorageError(Exception):
    """Base exception for all failures originating in the storage layer.

    Catch this exception at API boundaries to translate provider-specific
    failures into consistent HTTP responses without leaking internal type
    names in error bodies.

    Attributes
    ----------
    provider : str | None
        Canonical provider name (e.g. ``"LOCAL"``, ``"GOOGLE_DRIVE"``).
        Set by subclasses when the error originates from a specific backend.
    detail : str
        Human-readable description of what went wrong.
    """

    def __init__(
        self,
        detail: str = "",
        *,
        provider: Optional[str] = None,
        __cause__: Optional[Exception] = None,
    ) -> None:
        self.provider = provider
        super().__init__(detail or self.__class__.__name__)
        if __cause__ is not None:
            self.__cause__ = __cause__


# ---------------------------------------------------------------------------
# Concrete error types
# ---------------------------------------------------------------------------


class StorageNotFoundError(StorageError):
    """Raised when the requested file or folder does not exist.

    Maps to **404 Not Found**.

    Parameters
    ----------
    provider_file_id : str
        The opaque identifier the provider uses for the missing resource.
    """

    def __init__(
        self,
        provider_file_id: str,
        *,
        provider: Optional[str] = None,
        __cause__: Optional[Exception] = None,
    ) -> None:
        super().__init__(
            f"File not found: {provider_file_id!r}",
            provider=provider,
            __cause__=__cause__,
        )
        self.provider_file_id = provider_file_id


class StoragePermissionError(StorageError):
    """Raised when the provider rejects the request due to insufficient
    permissions (e.g. file locked, read-only folder).

    Maps to **403 Forbidden**.
    """


class StorageQuotaExceededError(StorageError):
    """Raised when the provider reports that the account has exceeded its
    storage or transfer quota.

    Maps to **413 Payload Too Large** or **507 Insufficient Storage**,
    depending on whether the limit is size-based or aggregate-quota-based.

    Parameters
    ----------
    limit_bytes : int | None
        The provider-reported quota ceiling, if available.
    used_bytes : int | None
        The provider-reported current usage, if available.
    """

    def __init__(
        self,
        *,
        limit_bytes: Optional[int] = None,
        used_bytes: Optional[int] = None,
        provider: Optional[str] = None,
        __cause__: Optional[Exception] = None,
    ) -> None:
        parts = ["Quota exceeded"]
        if limit_bytes is not None:
            parts.append(f"limit={limit_bytes} bytes")
        if used_bytes is not None:
            parts.append(f"used={used_bytes} bytes")
        super().__init__(
            detail=" ".join(parts),
            provider=provider,
            __cause__=__cause__,
        )
        self.limit_bytes = limit_bytes
        self.used_bytes = used_bytes


class StorageConflictError(StorageError):
    """Raised when the provider cannot fulfil the request because a
    conflicting resource already exists (e.g. duplicate filename at target
    path, or attempting to overwrite an immutable original).

    Maps to **409 Conflict**.
    """


class StorageUnsupportedError(StorageError):
    """Raised when the caller requests an operation the provider does not
    support (e.g. moving a file across different storage buckets where the
    provider lacks cross-bucket move).

    Maps to **501 Not Implemented** or **400 Bad Request** depending on
    whether the operation is universally unsupported or only unsupported for
    the current provider configuration.

    Parameters
    ----------
    operation : str
        The unsupported operation name (e.g. ``"MOVE"``).
    """

    def __init__(
        self,
        operation: str,
        *,
        provider: Optional[str] = None,
        __cause__: Optional[Exception] = None,
    ) -> None:
        super().__init__(
            f"Operation not supported: {operation!r}",
            provider=provider,
            __cause__=__cause__,
        )
        self.operation = operation


class StorageAuthenticationError(StorageError):
    """Raised when authentication or token validation fails — expired refresh
    token, invalid client secret, revoked access, or missing credentials.

    Maps to **401 Unauthorized**.
    """


class StorageRateLimitError(StorageError):
    """Raised when the provider returns a 429 / rate-limit response.

    Maps to **429 Too Many Requests**.

    Parameters
    ----------
    retry_after_seconds : int | None
        Value of the ``Retry-After`` header, if present.
    """

    def __init__(
        self,
        *,
        retry_after_seconds: Optional[int] = None,
        provider: Optional[str] = None,
        __cause__: Optional[Exception] = None,
    ) -> None:
        parts = ["Rate limited"]
        if retry_after_seconds is not None:
            parts.append(f"retry after {retry_after_seconds}s")
        super().__init__(
            detail=" ".join(parts),
            provider=provider,
            __cause__=__cause__,
        )
        self.retry_after_seconds = retry_after_seconds


class StorageUnavailableError(StorageError):
    """Raised when the provider is unreachable (network down, 5xx response,
    DNS failure).

    Maps to **502 Bad Gateway** or **503 Service Unavailable**.
    """


class StorageInvalidResponseError(StorageError):
    """Raised when the provider returns a response that cannot be parsed
    or does not match the expected schema (malformed JSON, missing required
    fields, unexpected status code in the 2xx range).

    Maps to **502 Bad Gateway**.

    Parameters
    ----------
    body : str | None
        Raw response body snippet for diagnostics.
    status_code : int | None
        HTTP status code received, if available.
    """

    def __init__(
        self,
        *,
        body: Optional[str] = None,
        status_code: Optional[int] = None,
        provider: Optional[str] = None,
        __cause__: Optional[Exception] = None,
    ) -> None:
        parts = ["Invalid provider response"]
        if status_code is not None:
            parts.append(f"(HTTP {status_code})")
        detail = " ".join(parts)
        super().__init__(
            detail=detail,
            provider=provider,
            __cause__=__cause__,
        )
        self.body = body
        self.status_code = status_code
