"""Shared HTTP mapping for account-aware adapter resolution.

File and upload endpoints must translate the typed account-resolution errors
from :mod:`backend.storage.file_account_resolution` into the same HTTP
statuses and the same deliberately generic messages (no account IDs,
credential references, tokens, or chained exception text).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.storage.errors import StorageUnavailableError
from backend.storage.file_account_resolution import (
    StorageAccountNotConnectedError,
    StorageAccountNotFoundError,
    StorageAccountProviderMismatchError,
    resolve_file_adapter,
)
from backend.storage.interfaces import StorageOperations


#: Fixed public message for every storage-unavailable response.
#:
#: ``StorageUnavailableError`` details originate in the storage and account
#: layers and can contain internal account IDs, credential references, keyring
#: or provider diagnostics.  That text is never echoed to a client; the
#: original exception is preserved as ``__cause__`` for internal diagnostics
#: (logs and tracebacks only).
STORAGE_UNAVAILABLE_DETAIL = "Storage provider is temporarily unavailable."


def resolve_adapter_or_http(db: Session, record) -> StorageOperations:
    """Resolve a record's adapter, mapping account failures to HTTP errors.

    ``record`` is any object exposing ``provider`` and ``provider_account_id``
    (a ``UnifiedFileRecord`` or a request-shaped target).  Legacy records
    (``provider_account_id`` is NULL) resolve through the provider registry;
    managed records resolve through exactly their own storage account and
    never fall back to the registry.
    """
    try:
        return resolve_file_adapter(db, record)
    except StorageAccountNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="The storage account for this file was not found.",
        ) from exc
    except StorageAccountProviderMismatchError as exc:
        raise HTTPException(
            status_code=409,
            detail="The storage account does not match this file's storage provider.",
        ) from exc
    except StorageAccountNotConnectedError as exc:
        raise HTTPException(
            status_code=503,
            detail="The storage account for this file is not available.",
        ) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=STORAGE_UNAVAILABLE_DETAIL,
        ) from exc
