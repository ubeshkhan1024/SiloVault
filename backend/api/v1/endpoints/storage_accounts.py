"""Account-scoped Google Drive OAuth endpoints (HVSA H1.4D).

Exposes Google Drive OAuth lifecycle operations for managed ``StorageAccount``
records, alongside (not replacing) the legacy provider-level endpoints in
``backend/api/v1/endpoints/google_drive.py``:

- POST   /api/v1/storage-accounts/{account_id}/google-drive/connect
- GET    /api/v1/storage-accounts/{account_id}/google-drive/status
- DELETE /api/v1/storage-accounts/{account_id}/google-drive/disconnect

Each endpoint resolves the managed account's ``credentials_ref`` on the
**server** and passes it to the existing scoped credential layer, so every
account owns a separate keyring entry and a separate SiloVault root folder.
The legacy single-account endpoints and the legacy keyring entry are never
touched by these operations.

Security notes
--------------
* The account identity never travels through the OAuth callback: the callback
  is validated by the existing state/PKCE machinery, and the account binding
  is the server-side request path, so a callback cannot be replayed against,
  or reassigned to, another account.
* Credentials are persisted through the OS keyring only; nothing is written to
  SQLite and no token, secret, or credential object is returned.
* A managed account without its own stored entry never falls back to the
  legacy ``"google-drive"`` entry.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from collections.abc import MutableMapping
from typing import Any, Callable, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api.v1.endpoints import google_drive as google_drive_endpoints
from backend.config import settings
from backend.database import get_db
from backend.models import StorageAccount
from backend.storage.account_adapter_manager import adapter_manager
from backend.storage.account_manager import (
    AccountManager,
    AccountStatus,
    _validate_provider_id,
)
from backend.storage.credentials import (
    CredentialStorageError,
    delete_google_drive_credentials,
    has_google_drive_credentials,
    load_google_drive_credentials,
)
from backend.storage.errors import StorageAuthenticationError, StorageUnavailableError
from backend.schemas import (
    StorageAccountCreate,
    StorageAccountListResponse,
    StorageAccountResponse,
    StorageAccountUpdate,
)
from backend.storage.google_drive import GoogleDriveAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/storage-accounts", tags=["storage-accounts"])

_GOOGLE_DRIVE_PROVIDER = "GOOGLE_DRIVE"
_LOCAL_PROVIDER = "LOCAL"

# In-memory adapters for connected managed accounts. Deliberately NOT part of
# the shared provider registry: the registry is provider-keyed (one slot per
# provider) and its "GOOGLE_DRIVE" slot stays reserved for the legacy
# connection. Routing business endpoints to a specific managed account is out
# of scope for H1.4D.
class _ManagedAdaptersProxy(MutableMapping):
    """MutableMapping proxy backed by central AccountAdapterManager.

    Ensures there is exactly one cache owner in the application runtime.
    Does NOT maintain a second dictionary.
    """

    def __getitem__(self, key: str) -> Any:
        with adapter_manager._cache_lock:
            if key not in adapter_manager._cache:
                raise KeyError(key)
            return adapter_manager._cache[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if value is None:
            adapter_manager.invalidate_account(key)
        else:
            adapter_manager.install(key, value)

    def __delitem__(self, key: str) -> None:
        with adapter_manager._cache_lock:
            if key not in adapter_manager._cache:
                raise KeyError(key)
        adapter_manager.invalidate_account(key)

    def __iter__(self):
        with adapter_manager._cache_lock:
            return iter(list(adapter_manager._cache.keys()))

    def __len__(self) -> int:
        with adapter_manager._cache_lock:
            return len(adapter_manager._cache)

    def clear(self) -> None:
        adapter_manager.invalidate_all()

    def get(self, key: str, default: Any = None) -> Any:
        with adapter_manager._cache_lock:
            return adapter_manager._cache.get(key, default)

    def pop(self, key: str, default: Any = ...) -> Any:
        with adapter_manager._cache_lock:
            if key in adapter_manager._cache:
                val = adapter_manager._cache[key]
                # Directly invalidate the generation and remove from cache
                # (cannot call invalidate_account() as it would deadlock)
                adapter_manager._account_generations[key] = (
                    adapter_manager._account_generations.get(key, 0) + 1
                )
                del adapter_manager._cache[key]
                return val
            if default is not ...:
                return default
            raise KeyError(key)


_managed_adapters: MutableMapping[str, GoogleDriveAdapter] = _ManagedAdaptersProxy()
_managed_lock = adapter_manager._cache_lock

# Per-account locks (HVSA H1.4E-A). A connect flow can take minutes (browser
# round trip), so a single global lock would serialize every Google Drive
# account.  Instead each account id owns a lock that serializes only that
# account's connect/disconnect operations; different accounts proceed in
# parallel.  Entries are reference-counted and removed as soon as no caller
# holds or waits on them, so the map stays bounded by in-flight accounts.
_account_locks: dict[str, list] = {}
_account_locks_guard = threading.Lock()


@contextmanager
def _account_lock(account_id: str) -> Iterator[None]:
    """Serialize connect/disconnect for one account, without a global lock.

    Holding this lock for the whole operation is what makes credential
    cleanup safe: a failing attempt cannot delete a newer attempt's token
    because the newer attempt cannot start until the older one has finished
    (and vice versa), and a disconnect cannot interleave with a connect.

    The reference count is taken before the wait and returned in ``finally``,
    and the map entry is only deleted when this caller is the last participant
    *and* the entry in the map is still the one it registered.  One window is
    not recoverable in pure Python: a ``KeyboardInterrupt`` delivered in the
    single bytecode between ``lock.acquire()`` returning and the ``acquired``
    flag being set would leave the underlying lock held.  Closing that window
    safely is not possible without re-designing around ``__enter__``/timeouts,
    so it is documented instead of engineered around.
    """
    with _account_locks_guard:
        entry = _account_locks.get(account_id)
        if entry is None:
            entry = [threading.Lock(), 0]
            _account_locks[account_id] = entry
        entry[1] += 1

    lock = entry[0]
    acquired = False
    try:
        # Acquisition is inside the try (HVSA H1.4F) so an interruption during
        # the wait still returns the reference taken above; otherwise a stuck
        # count would pin this map entry for the life of the process.
        lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        with _account_locks_guard:
            entry[1] -= 1
            if entry[1] == 0 and _account_locks.get(account_id) is entry:
                # No holder or waiter left: safe to drop this entry. The
                # identity check guarantees only the entry this caller
                # registered is removed, never a newer one.
                del _account_locks[account_id]


# ---------------------------------------------------------------------------
# Managed adapter bookkeeping
# ---------------------------------------------------------------------------


def _set_managed_adapter(
    account_id: str, adapter: Optional[GoogleDriveAdapter]
) -> None:
    """Store or clear the in-memory adapter for a managed account."""
    if adapter is None:
        adapter_manager.invalidate_account(account_id)
    else:
        adapter_manager.install(account_id, adapter)


def _get_managed_adapter(account_id: str) -> Optional[GoogleDriveAdapter]:
    """Return the in-memory adapter for a managed account, if any."""
    with adapter_manager._cache_lock:
        return adapter_manager._cache.get(account_id)


# ---------------------------------------------------------------------------
# Account and credential-reference resolution
# ---------------------------------------------------------------------------

#: Prefix of every server-generated account credential reference.
_CREDENTIALS_REF_PREFIX = "storage-account:"


def _get_google_drive_account(db: Session, account_id: str) -> StorageAccount:
    """Return the managed account, validating that it can use Drive OAuth.

    Raises
    ------
    HTTPException
        404 if the account does not exist; 400 if it is a LOCAL account or any
        provider other than GOOGLE_DRIVE.
    """
    account = AccountManager(db).get_account(account_id)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail=f"Storage account {account_id!r} not found.",
        )
    if account.provider_id == _LOCAL_PROVIDER:
        raise HTTPException(
            status_code=400,
            detail=(
                "Local storage accounts do not use Google Drive OAuth. "
                "Use an account whose provider is GOOGLE_DRIVE."
            ),
        )
    if account.provider_id != _GOOGLE_DRIVE_PROVIDER:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Provider {account.provider_id!r} is not supported by the "
                "Google Drive OAuth endpoints."
            ),
        )
    return account


def _account_credentials_ref(account: StorageAccount) -> Optional[str]:
    """Return the account's non-empty credential reference, if it has one."""
    ref = account.credentials_ref
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return None


def _ensure_credentials_ref(
    manager: AccountManager, account: StorageAccount
) -> str:
    """Resolve or assign this account's credential reference.

    The reference is generated server-side from the account's stable UUID, so
    a client can never select or influence which keyring entry is used.
    """
    ref = _account_credentials_ref(account)
    if ref is not None:
        return ref

    ref = f"{_CREDENTIALS_REF_PREFIX}{account.id}"
    manager.update_account(
        account.id,
        credentials_ref=ref,
        _on_commit=lambda aid: adapter_manager.invalidate_account(aid),
    )
    return ref


# ---------------------------------------------------------------------------
# Storage account read endpoints (HVSA H1.5.3)
# ---------------------------------------------------------------------------


@router.get("", response_model=StorageAccountListResponse)
async def list_storage_accounts(
    provider_id: Optional[str] = None,
    db: Session = Depends(get_db),
) -> StorageAccountListResponse:
    """List storage accounts, optionally filtered by provider.

    ``provider_id`` is validated with the same canonical rule as the provider
    registry, so an invalid value is rejected instead of silently producing an
    empty list.  Responses never include ``credentials_ref`` or any credential
    material.

    Returns:
        200 with every matching account.
        400 if ``provider_id`` is not a canonical provider identifier.
    """
    try:
        accounts = AccountManager(db).list_accounts(provider_id=provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    items = [StorageAccountResponse.model_validate(account) for account in accounts]
    return StorageAccountListResponse(items=items, total=len(items))


@router.get("/{account_id}", response_model=StorageAccountResponse)
async def get_storage_account(
    account_id: str,
    db: Session = Depends(get_db),
) -> StorageAccountResponse:
    """Retrieve one storage account by its stable UUID.

    Reads through ``AccountManager`` on every call (there is no separate
    account cache) and returns no credential reference or secret.

    Returns:
        200 with the account.
        404 if no account has this ID.
    """
    account = AccountManager(db).get_account(account_id)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail=f"Storage account with id '{account_id}' was not found.",
        )
    return StorageAccountResponse.model_validate(account)


# ---------------------------------------------------------------------------
# Storage account create endpoint (HVSA H1.5.3)
# ---------------------------------------------------------------------------


def _new_credentials_ref() -> str:
    """Return a fresh server-generated credential reference.

    The reference is never accepted from a client, so a new account always
    gets a random reference produced here and never one a caller can choose,
    predict, or reuse.
    """
    return f"{_CREDENTIALS_REF_PREFIX}{uuid.uuid4()}"


def _ensure_json_serializable(configuration: Dict[str, Any]) -> None:
    """Reject configuration values that cannot be persisted as JSON.

    SQLAlchemy stores the ``JSON`` column with ``json.dumps``.  Validating it
    here turns a would-be 500 (non-serializable value) or a silently persisted
    invalid document (``NaN``/``Infinity``, which ``json.dumps`` allows by
    default) into a clear 422 before anything is written.
    """
    try:
        json.dumps(configuration, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "configuration must contain only JSON-serializable values "
                "(NaN, Infinity, and non-JSON objects are not allowed)."
            ),
        ) from exc


@router.post("", status_code=201, response_model=StorageAccountResponse)
async def create_storage_account(
    payload: StorageAccountCreate,
    db: Session = Depends(get_db),
) -> StorageAccountResponse:
    """Create a storage account with a server-generated credential reference.

    The client chooses the provider, display name, and configuration; it can
    never choose ``credentials_ref`` (the schema rejects it and the value is
    generated here).  Creation goes through ``AccountManager.create_account``,
    so provider validation, reference normalization, and duplicate detection
    stay in one place, and the request-scoped session owns the transaction.
    The new account starts ``pending`` with no credentials loaded and no
    adapter installed, and the response never includes credential material.

    Returns:
        201 with the created account.
        409 if the generated reference is already in use.
        422 if the provider, display name, or configuration is invalid.
    """
    try:
        _validate_provider_id(payload.provider_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _ensure_json_serializable(payload.configuration)

    manager = AccountManager(db)
    try:
        account = manager.create_account(
            provider_id=payload.provider_id,
            display_name=payload.display_name,
            credentials_ref=_new_credentials_ref(),
            configuration=payload.configuration,
        )
    except ValueError as exc:
        db.rollback()
        if "credentials_ref" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Another storage account already uses this credential "
                    "reference."
                ),
            ) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TypeError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        # Never leave a partial account or an unusable request session behind.
        db.rollback()
        raise

    return StorageAccountResponse.model_validate(account)


# ---------------------------------------------------------------------------
# Storage account update endpoint (HVSA H1.5.3)
# ---------------------------------------------------------------------------


def _not_found(account_id: str) -> HTTPException:
    """Return the canonical 404 for an unknown storage account."""
    return HTTPException(
        status_code=404,
        detail=f"Storage account with id '{account_id}' was not found.",
    )


@router.patch("/{account_id}", response_model=StorageAccountResponse)
async def update_storage_account(
    account_id: str,
    payload: Optional[StorageAccountUpdate] = None,
    db: Session = Depends(get_db),
) -> StorageAccountResponse:
    """Partially update a storage account's display name or configuration.

    Only fields present in the request body are considered
    (``model_dump(exclude_unset=True)``); an empty body is a no-op.  ``None``
    keeps the existing value, matching ``AccountManager.update_account`` — a
    client cannot blank a display name or clear a configuration by sending
    ``null``.  ``provider_id`` and ``credentials_ref`` are not part of
    ``StorageAccountUpdate`` and are rejected as unknown fields, so neither the
    account's provider nor its keyring selector can change through this route.

    The update runs through ``AccountManager.update_account``, which owns the
    commit and decides whether the cached adapter must be invalidated: only a
    changed ``credentials_ref`` or a changed LOCAL ``storage_root`` does.  Cache
    invalidation therefore stays scoped to this account, and a failed commit
    (rolled back here) never invalidates anything.  Configuration is checked
    for JSON serializability before persistence, so ``NaN``, ``Infinity``, and
    non-JSON objects are rejected with 422 instead of a 500 or a silently
    invalid document.

    Returns:
        200 with the updated account.
        404 if no account has this ID.
        422 if the display name or configuration is invalid.
    """
    changes = payload.model_dump(exclude_unset=True) if payload is not None else {}
    configuration = changes.get("configuration")
    if configuration is not None:
        _ensure_json_serializable(configuration)

    manager = AccountManager(db)
    if manager.get_account(account_id) is None:
        raise _not_found(account_id)

    try:
        account = manager.update_account(
            account_id,
            display_name=changes.get("display_name"),
            configuration=configuration,
            _on_commit=lambda aid: adapter_manager.invalidate_account(aid),
        )
    except ValueError as exc:
        # AccountManager raises before mutating when the row is gone; keep the
        # public contract a 404 rather than a 422 for that race.
        db.rollback()
        if "not found" in str(exc).lower():
            raise _not_found(account_id) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TypeError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        # A failed commit must leave neither a partial update nor an
        # invalidated cache entry behind.
        db.rollback()
        raise

    return StorageAccountResponse.model_validate(account)


# ---------------------------------------------------------------------------
# Startup reconciliation (HVSA H1.4E-B)
# ---------------------------------------------------------------------------

#: Outcome keys returned by :func:`restore_managed_adapters`.
_OUTCOME_RESTORED = "restored"
_OUTCOME_AUTHENTICATION_FAILED = "authentication_failed"
_OUTCOME_PROVIDER_UNAVAILABLE = "provider_unavailable"
_OUTCOME_CREDENTIAL_STORE_UNAVAILABLE = "credential_store_unavailable"
_OUTCOME_CREDENTIALS_MISSING = "credentials_missing"
_OUTCOME_UNEXPECTED_FAILURE = "unexpected_failure"


def restore_managed_adapters(
    session_factory: Callable[[], Session],
) -> dict[str, int]:
    """Re-attach managed Google Drive adapters after an application restart.

    Managed adapters live only in memory, so after a restart a managed account
    can have ``status = \"connected\"`` and a valid scoped keyring entry but no
    usable runtime adapter.  This function rebuilds those adapters from the
    durable state, one account at a time, without touching the legacy
    ``\"google-drive\"`` credential entry, the legacy adapter, or the shared
    provider registry.

    Failure classification (HVSA H1.4E-B):

    * authenticated -> adapter installed, database status untouched.
    * ``StorageAuthenticationError`` -> adapter not installed and the account
      is marked ``disconnected``; the keyring entry is **preserved** so the
      user can reconnect.
    * ``StorageUnavailableError`` -> transient provider failure: adapter not
      installed, status and credentials preserved.
    * ``CredentialStorageError`` -> locked/unavailable keyring: adapter not
      installed, status and credentials preserved.
    * missing credential entry -> adapter not installed and the account is
      marked ``disconnected``; the legacy entry is never used as a fallback.

    A failure for one account never prevents the remaining accounts from being
    restored.

    Parameters
    ----------
    session_factory : Callable[[], Session]
        Factory returning a new SQLAlchemy session. Each session this function
        opens is closed before it returns (including on error).

    Returns
    -------
    dict[str, int]
        Count of accounts per outcome key, for safe startup logging.
    """
    summary = {
        _OUTCOME_RESTORED: 0,
        _OUTCOME_AUTHENTICATION_FAILED: 0,
        _OUTCOME_PROVIDER_UNAVAILABLE: 0,
        _OUTCOME_CREDENTIAL_STORE_UNAVAILABLE: 0,
        _OUTCOME_CREDENTIALS_MISSING: 0,
        _OUTCOME_UNEXPECTED_FAILURE: 0,
    }

    # Short-lived read session: collect a snapshot of candidate accounts and
    # close it before any keyring or network work begins.
    db = session_factory()
    try:
        candidates = [
            (account.id, _account_credentials_ref(account))
            for account in AccountManager(db).list_accounts(_GOOGLE_DRIVE_PROVIDER)
            if account.status == AccountStatus.CONNECTED
            and account.credentials_ref is not None
        ]
    finally:
        db.close()

    for account_id, credentials_ref in candidates:
        try:
            with _account_lock(account_id):
                outcome = _restore_managed_account(
                    session_factory, account_id, credentials_ref
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Unexpected error restoring storage account %s (%s)",
                account_id,
                type(exc).__name__,
            )
            outcome = _OUTCOME_UNEXPECTED_FAILURE

        summary[outcome] = summary.get(outcome, 0) + 1

    return summary


def _restore_managed_account(
    session_factory: Callable[[], Session],
    account_id: str,
    credentials_ref: Optional[str],
) -> str:
    """Restore one managed account's adapter; return its outcome key.

    Called with the account's per-account lock held, so a concurrent
    connect/disconnect for the same account cannot interleave with it.
    """
    if credentials_ref is None:
        # Defensive: whitespace-only references are rejected on write, but a
        # legacy row is still not a usable credential selector.
        logger.warning(
            "Storage account %s has no usable credential reference; marking disconnected",
            account_id,
        )
        _mark_account_disconnected(session_factory, account_id)
        _set_managed_adapter(account_id, None)
        return _OUTCOME_CREDENTIALS_MISSING

    try:
        stored = load_google_drive_credentials(credentials_ref=credentials_ref)
    except CredentialStorageError as exc:
        # Infrastructure problem (locked/unavailable keyring): nothing about
        # this account's durable state is known to be wrong.
        logger.warning(
            "Credential store unavailable while restoring storage account %s (%s); "
            "status and credentials preserved",
            account_id,
            type(exc).__name__,
        )
        return _OUTCOME_CREDENTIAL_STORE_UNAVAILABLE

    if stored is None:
        logger.warning(
            "Stored credentials are missing for storage account %s; marking disconnected",
            account_id,
        )
        _mark_account_disconnected(session_factory, account_id)
        _set_managed_adapter(account_id, None)
        return _OUTCOME_CREDENTIALS_MISSING

    adapter = GoogleDriveAdapter(credentials_ref=credentials_ref)
    try:
        adapter.authenticate(
            {
                "refresh_token": stored.refresh_token,
                "client_id": stored.client_id,
                "client_secret": stored.client_secret,
                "scopes": list(stored.scopes),
            }
        )
    except StorageAuthenticationError as exc:
        # Revoked/invalid credentials: the stored entry stays in place so the
        # user can reconnect, but nothing is installed or reported usable.
        logger.warning(
            "Stored credentials for storage account %s were rejected (%s); "
            "marking disconnected",
            account_id,
            type(exc).__name__,
        )
        _mark_account_disconnected(session_factory, account_id)
        _set_managed_adapter(account_id, None)
        return _OUTCOME_AUTHENTICATION_FAILED
    except StorageUnavailableError as exc:
        # Transient: never downgrade the account or delete its credentials.
        logger.warning(
            "Provider unavailable while restoring storage account %s (%s); "
            "status and credentials preserved",
            account_id,
            type(exc).__name__,
        )
        return _OUTCOME_PROVIDER_UNAVAILABLE

    _set_managed_adapter(account_id, adapter)
    logger.info("Restored managed Google Drive adapter for storage account %s", account_id)
    return _OUTCOME_RESTORED


def _mark_account_disconnected(
    session_factory: Callable[[], Session], account_id: str
) -> None:
    """Persist ``disconnected`` for one account using its own short session.

    Credential entries are never deleted here: only the status changes.
    Failures are logged and swallowed so one account cannot abort the sweep.
    """
    db = session_factory()
    try:
        AccountManager(db).disconnect_account(account_id)
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Could not mark storage account %s disconnected (%s)",
            account_id,
            type(exc).__name__,
        )
    finally:
        db.close()


def _staging_credentials_ref(credentials_ref: str) -> str:
    """Return a unique staging credential reference for one connect attempt.

    HVSA H1.4F: the attempt writes its token here first, authenticates, and
    only then promotes it into *credentials_ref*.  The random suffix makes a
    collision with another attempt (or another account) practically impossible,
    and the value is never persisted in the database, returned to a client, or
    logged.
    """
    return f"{credentials_ref}:staging:{uuid.uuid4().hex}"


def _raise_oauth_failure(error: object, account_id: str) -> None:
    """Map a flow error code to an HTTP error without leaking details.

    Only the fixed error-code prefix (text before the first ``:``) is logged;
    library/detail text is never echoed to the client or written to logs.
    """
    error_code = str(error).split(":", 1)[0]
    logger.warning(
        "Managed Google Drive OAuth failed for account %s (error=%s)",
        account_id,
        error_code,
    )
    if error_code == "state_mismatch":
        raise HTTPException(status_code=400, detail="OAuth state validation failed.")
    if error_code == "timeout":
        raise HTTPException(status_code=408, detail="OAuth flow timed out.")
    if error_code == "no_refresh_token":
        raise HTTPException(
            status_code=400,
            detail=(
                "Google did not return a refresh token. "
                "Ensure 'offline' access type is configured."
            ),
        )
    raise HTTPException(status_code=500, detail="OAuth flow failed.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{account_id}/google-drive/connect")
def connect_account_google_drive(
    account_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Connect a managed Google Drive account via the OAuth loopback flow.

    Synchronous on purpose: FastAPI runs it in a worker thread, so the
    per-account lock below never blocks the event loop and this account's
    connect/disconnect operations cannot interleave.

    Credentials are staged under a per-attempt reference, authenticated, and
    only then promoted into this account's permanent ``credentials_ref``; the
    resulting adapter is scoped to that permanent reference (HVSA H1.4F).  An
    existing credential therefore survives a failed or interrupted reconnect.
    The legacy connection and the shared provider registry are left untouched.

    Returns:
        200 with safe account metadata on success.
        400 if the account is not a Google Drive account or OAuth validation fails.
        404 if the account does not exist.
        408 if the OAuth flow timed out.
        409 if the account cannot be given a unique credential reference.
        500 if the flow failed or its status could not be persisted.
        503 if Google Drive OAuth is not configured.
    """
    with _account_lock(account_id):
        return _connect_managed_account(account_id, db)


def _connect_managed_account(account_id: str, db: Session) -> dict:
    """Body of :func:`connect_account_google_drive`, under the account lock.

    Credential lifecycle (HVSA H1.4F): stage -> authenticate -> promote.  Every
    cleanup below targets this attempt's staging reference only, so neither the
    account's previously stored credential nor any other account's entry can be
    removed by a failure here.
    """
    account = _get_google_drive_account(db, account_id)
    # Capture the stable identity immediately: after a rollback the ORM instance
    # can be expired or detached, so error handling must never read
    # ``account.id`` again (that access could issue another query or raise).
    stable_account_id = account.id

    if not settings.google_drive_client_id or not settings.google_drive_client_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google Drive OAuth is not configured. "
                "Set GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET in .env."
            ),
        )

    manager = AccountManager(db)
    try:
        credentials_ref = _ensure_credentials_ref(manager, account)
    except ValueError as exc:
        # The generated reference is derived from the account's own UUID, so
        # this is only reachable if the stored data is already inconsistent.
        db.rollback()
        logger.error(
            "Could not assign a unique credential reference to storage account %s (%s)",
            stable_account_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail="Another storage account already uses this credential reference.",
        ) from exc

    # Unique to this attempt; never persisted, returned, or logged.
    staging_ref = _staging_credentials_ref(credentials_ref)

    try:
        result = google_drive_endpoints._run_oauth_flow(
            credentials_ref=staging_ref,
            promote_to=credentials_ref,
        )
    except Exception as exc:  # pragma: no cover - defensive
        google_drive_endpoints._discard_scoped_credentials(staging_ref)
        logger.error(
            "Managed Google Drive OAuth flow raised for account %s (%s)",
            stable_account_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="OAuth flow failed.") from exc

    if not result.get("success"):
        # The flow removes its own staging entry; this is an idempotent safety
        # net for any early-return path that bypassed it.
        google_drive_endpoints._discard_scoped_credentials(staging_ref)
        _raise_oauth_failure(result.get("error", "unknown"), stable_account_id)

    try:
        account = manager.mark_connected(stable_account_id)
    except Exception as exc:
        # The new credential was already authenticated and promoted, but the
        # durable "connected" state was not persisted.  Roll back, drop only
        # this attempt's staging entry, and never install the adapter: an
        # account must not appear usable when its status could not be stored.
        # The promoted credential is retained (it is valid and belongs to this
        # account); nothing that previously worked is deleted.
        db.rollback()
        _set_managed_adapter(stable_account_id, None)
        google_drive_endpoints._discard_scoped_credentials(staging_ref)
        logger.error(
            "Failed to persist connected status for storage account %s: %s: %s",
            stable_account_id,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to persist account status.",
        ) from exc

    adapter = result.get("adapter")
    if isinstance(adapter, GoogleDriveAdapter):
        _set_managed_adapter(stable_account_id, adapter)

    return {
        "account_id": stable_account_id,
        "provider_id": account.provider_id,
        "display_name": account.display_name,
        "status": account.status,
        "connected": True,
        "credentials_present": has_google_drive_credentials(
            credentials_ref=credentials_ref
        ),
        "message": "Google Drive account connected successfully.",
    }


@router.get("/{account_id}/google-drive/status")
async def get_account_google_drive_status(
    account_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Report safe connection metadata for a managed Google Drive account.

    Reads only this account's keyring entry (never the legacy entry) and makes
    no provider call. Tokens, credential objects, and client secrets are never
    returned.

    The response distinguishes three independent facts (HVSA H1.4E-B):

    * ``status`` - the persisted database status (downgraded to
      ``"disconnected"`` when this account's own credential entry is gone).
    * ``credentials_present`` - whether the account-scoped keyring entry exists.
    * ``adapter_present`` - whether an authenticated in-memory adapter for this
      account is currently installed.  The legacy adapter never counts.

    ``connected`` is the runtime answer: it is true only when the database says
    ``connected`` **and** the account's credentials exist **and** its managed
    adapter is installed.  A freshly restarted process therefore reports
    ``connected: false`` until startup reconciliation has rebuilt the adapter.

    Returns:
        200 with connection status information.
        400 if the account is not a Google Drive account.
        404 if the account does not exist.
    """
    account = _get_google_drive_account(db, account_id)
    credentials_ref = _account_credentials_ref(account)

    credentials_present = (
        credentials_ref is not None
        and has_google_drive_credentials(credentials_ref=credentials_ref)
    )

    status = account.status
    if status == AccountStatus.CONNECTED and not credentials_present:
        # Do not report "connected" without this account's own stored entry;
        # never fall back to the legacy entry to justify a connected status.
        status = AccountStatus.DISCONNECTED

    # Only this account's managed adapter counts; the legacy provider-level
    # adapter is deliberately never consulted here.
    adapter_present = _get_managed_adapter(account.id) is not None

    return {
        "account_id": account.id,
        "provider_id": account.provider_id,
        "display_name": account.display_name,
        "status": status,
        "connected": status == AccountStatus.CONNECTED and adapter_present,
        "credentials_present": credentials_present,
        "adapter_present": adapter_present,
    }


@router.delete("/{account_id}/google-drive/disconnect", status_code=204)
def disconnect_account_google_drive(
    account_id: str,
    db: Session = Depends(get_db),
) -> None:
    """Disconnect a managed Google Drive account.

    Synchronous so the per-account lock does not block the event loop; a
    disconnect waits for an in-flight connect on the same account and then
    removes exactly that connect's credentials.

    Deletes only this account's scoped credential entry, drops its in-memory
    adapter, and marks the account ``"disconnected"``. The ``StorageAccount``
    record, Drive files, other managed accounts, and the legacy credential
    slot are never affected. Safe to call repeatedly.

    Returns:
        204 No Content.
        400 if the account is not a Google Drive account.
        404 if the account does not exist.
        500 if the stored credentials could not be cleared.
    """
    with _account_lock(account_id):
        _disconnect_managed_account(account_id, db)


def _disconnect_managed_account(account_id: str, db: Session) -> None:
    """Body of :func:`disconnect_account_google_drive`, under the account lock."""
    account = _get_google_drive_account(db, account_id)
    credentials_ref = _account_credentials_ref(account)

    if credentials_ref is not None:
        try:
            delete_google_drive_credentials(credentials_ref=credentials_ref)
        except CredentialStorageError as exc:
            logger.error(
                "Failed to clear scoped Google Drive credentials for account %s",
                account.id,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to clear stored credentials.",
            ) from exc

    adapter = _get_managed_adapter(account.id)
    if adapter is not None:
        adapter.disconnect()
    _set_managed_adapter(account.id, None)

    AccountManager(db).disconnect_account(account.id)
    return None
