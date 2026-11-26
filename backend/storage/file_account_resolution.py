"""Account-aware adapter resolution for file operations (HVSA H1.5.4 Step 1).

File records carry an optional ``provider_account_id``.  This module owns the
single routing decision for turning such a record into a storage adapter:

* ``provider_account_id is None``
  -> the record is legacy/provider-level and is resolved through the existing
  provider registry (``backend.storage.registry.lookup``).
* ``provider_account_id is not None``
  -> the record belongs to exactly one managed ``StorageAccount`` and is
  resolved through ``AccountAdapterManager.resolve`` for that account ID.

Fail-closed rules
-----------------
* An unknown account raises :class:`StorageAccountNotFoundError`.
* A managed account whose provider differs from the record's provider raises
  :class:`StorageAccountProviderMismatchError`.
* Any failure while resolving a managed account raises
  :class:`StorageAccountNotConnectedError` (or propagates an existing
  :class:`~backend.storage.errors.StorageError` such as
  ``StorageUnavailableError`` unchanged).
* A managed-account failure **never** falls back to the legacy provider
  registry.  Only ``provider_account_id is None`` uses the registry.

The helper performs no storage operation, no credential access, and no
database mutation; it only resolves and returns an adapter instance.  It is
used by the file endpoints (download, delete, rename and move), the upload
endpoint and the recovery service, directly or through
:func:`backend.api.v1.endpoints.account_adapter_http.resolve_adapter_or_http`.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from backend.storage.account_adapter_manager import (
    AccountAdapterManager,
    adapter_manager,
)
from backend.storage.account_manager import AccountManager
from backend.storage.errors import StorageError
from backend.storage.interfaces import StorageOperations
from backend.storage.registry import lookup


# ---------------------------------------------------------------------------
# Typed resolution errors
# ---------------------------------------------------------------------------


class FileAccountResolutionError(StorageError):
    """Base error for a file record's adapter failing to resolve by account.

    Derives from :class:`~backend.storage.errors.StorageError` so existing
    storage error handling keeps a single catch point, while callers can still
    distinguish account-resolution failures from provider failures.

    Attributes
    ----------
    account_id : str
        The managed account ID the record pointed at.
    provider : str | None
        The provider recorded on the file record.
    """

    def __init__(
        self,
        detail: str,
        *,
        account_id: str,
        provider: Optional[str] = None,
        __cause__: Optional[Exception] = None,
    ) -> None:
        super().__init__(detail, provider=provider, __cause__=__cause__)
        self.account_id = account_id


class StorageAccountNotFoundError(FileAccountResolutionError):
    """The record references a managed account that does not exist.

    Maps naturally to **404 Not Found** at an API boundary.
    """

    def __init__(
        self,
        account_id: str,
        *,
        provider: Optional[str] = None,
        __cause__: Optional[Exception] = None,
    ) -> None:
        super().__init__(
            f"Storage account {account_id!r} was not found.",
            account_id=account_id,
            provider=provider,
            __cause__=__cause__,
        )


class StorageAccountProviderMismatchError(FileAccountResolutionError):
    """The account exists but belongs to a different provider than the record.

    Attributes
    ----------
    expected_provider : str
        The provider recorded on the file record.
    actual_provider : str
        The provider stored on the managed account.
    """

    def __init__(
        self,
        account_id: str,
        *,
        expected_provider: str,
        actual_provider: str,
        __cause__: Optional[Exception] = None,
    ) -> None:
        super().__init__(
            (
                f"Storage account {account_id!r} belongs to provider "
                f"{actual_provider!r}, not {expected_provider!r}."
            ),
            account_id=account_id,
            provider=expected_provider,
            __cause__=__cause__,
        )
        self.expected_provider = expected_provider
        self.actual_provider = actual_provider


class StorageAccountNotConnectedError(FileAccountResolutionError):
    """The managed account exists but cannot currently provide an adapter.

    Covers a disconnected account, an account without usable credentials, and
    an account whose adapter authentication failed.  The underlying
    ``ValueError`` raised by ``AccountAdapterManager.resolve`` is preserved as
    ``__cause__``.
    """

    def __init__(
        self,
        account_id: str,
        *,
        provider: Optional[str] = None,
        __cause__: Optional[Exception] = None,
    ) -> None:
        detail = f"Storage account {account_id!r} is not available."
        if __cause__ is not None:
            detail = f"{detail} {__cause__}"
        super().__init__(
            detail,
            account_id=account_id,
            provider=provider,
            __cause__=__cause__,
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_file_adapter(
    db: Session,
    record,
    *,
    manager: Optional[AccountAdapterManager] = None,
) -> StorageOperations:
    """Resolve the storage adapter for a file record.

    Parameters
    ----------
    db : sqlalchemy.orm.Session
        Session used only to look up the managed account.  The helper never
        writes to it.
    record : UnifiedFileRecord
        Any object exposing ``provider`` and ``provider_account_id``.
    manager : AccountAdapterManager, optional
        Account adapter manager to use.  Defaults to the process-wide
        ``adapter_manager``; tests inject an isolated instance or fake.

    Returns
    -------
    StorageOperations
        The exact adapter for the record: the registered provider adapter for
        a legacy record, or the account-scoped adapter for a managed record.

    Raises
    ------
    StorageAccountNotFoundError
        The record references an account that does not exist.
    StorageAccountProviderMismatchError
        The account's provider differs from the record's provider.
    StorageAccountNotConnectedError
        The account cannot currently provide an adapter (disconnected, missing
        credentials, or authentication failure).
    StorageUnavailableError
        Propagated unchanged when the account manager reports an unavailable
        credential store or repeated invalidation.
    StorageUnavailableError
        Propagated unchanged from the provider registry for a legacy record
        whose provider is not registered.
    """
    provider = record.provider
    account_id = record.provider_account_id

    # Legacy/provider-level record: unchanged pre-H1.5.4 behavior.
    if account_id is None:
        adapter, _capabilities = lookup(provider)
        return adapter

    # Managed record: resolve that exact account, or fail.  There is no
    # fallback branch below this point by design.
    account = AccountManager(db).get_account(account_id)
    if account is None:
        raise StorageAccountNotFoundError(account_id, provider=provider)

    if account.provider_id != provider:
        raise StorageAccountProviderMismatchError(
            account_id,
            expected_provider=provider,
            actual_provider=account.provider_id,
        )

    active_manager = adapter_manager if manager is None else manager
    try:
        return active_manager.resolve(account_id)
    except ValueError as exc:
        # AccountAdapterManager.resolve raises ValueError for a vanished
        # account, a non-connected status, missing credentials, and failed
        # authentication.  Wrap it without consulting the legacy registry.
        raise StorageAccountNotConnectedError(
            account_id,
            provider=provider,
            __cause__=exc,
        ) from exc


__all__ = [
    "FileAccountResolutionError",
    "StorageAccountNotFoundError",
    "StorageAccountProviderMismatchError",
    "StorageAccountNotConnectedError",
    "resolve_file_adapter",
]
