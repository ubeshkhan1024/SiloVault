"""AccountManager for SiloVault multi-account storage architecture.

Manages storage account identities, lifecycle, and account-to-adapter resolution.
The Account Manager owns account-specific adapter instances while the Provider
Registry stays at provider-level only (per design constraint).

Design notes
------------
* Receives an existing SQLAlchemy Session through dependency injection — does
  NOT create its own engine or session.
* Provider ID validation uses the existing pattern from registry.py:
  ``^[A-Z][A-Z0-9_]*$`` — validated via the same regex, no modification to
  registry.py required.
* Account status is NOT reachability: ``"connected"`` means SiloVault has valid
  credentials + active adapter, not that the provider is reachable.
* ``configuration`` JSON returns defensive copies to prevent cross-contamination.
* ``credentials_ref`` is opaque — the Account Manager does NOT load or store
  credentials; credential management remains in ``backend.storage.credentials``.
* Multiple accounts for the same provider are supported via separate
  ``storage_accounts`` rows with the same ``provider_id``.
* Status values are documented and finite: ``"pending"``, ``"connected"``,
  ``"disconnected"``, ``"error"``.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.models import StorageAccount


# ---------------------------------------------------------------------------
# Provider ID validation (mirrors registry.py pattern, no modifications)
# ---------------------------------------------------------------------------

_PROVIDER_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _validate_provider_id(provider_id: str) -> str:
    """Validate a provider identifier using the same rules as registry.py.

    Parameters
    ----------
    provider_id : str
        The provider ID to validate.

    Returns
    -------
    str
        The validated provider ID (unchanged).

    Raises
    ------
    TypeError
        If *provider_id* is not a string.
    ValueError
        If *provider_id* is empty, has whitespace, or does not match the
        canonical uppercase pattern.
    """
    if not isinstance(provider_id, str):
        raise TypeError(
            f"provider_id must be a string, got {type(provider_id).__name__}"
        )
    if not provider_id:
        raise ValueError("provider_id must be a non-empty string")
    if provider_id != provider_id.strip():
        raise ValueError("provider_id must not have leading or trailing whitespace")
    if not _PROVIDER_ID_PATTERN.fullmatch(provider_id):
        raise ValueError(
            "Provider ID must be uppercase ASCII letters, digits, and "
            "underscores, starting with a letter"
        )
    return provider_id


# ---------------------------------------------------------------------------
# Credential reference validation (HVSA H1.4E-A)
# ---------------------------------------------------------------------------

# Matches the ``storage_accounts.credentials_ref`` column width.
_CREDENTIALS_REF_MAX_LENGTH = 512


def _normalize_credentials_ref(credentials_ref: str) -> str:
    """Validate and normalize an account credential reference.

    The reference is the only thing that selects a keyring entry, so an empty
    or whitespace-variant reference would silently share another account's
    credentials.  References are therefore trimmed and length-checked here,
    consistently with ``backend.storage.credentials._derive_keyring_account``.

    Parameters
    ----------
    credentials_ref : str
        Caller-supplied reference.

    Returns
    -------
    str
        The trimmed reference.

    Raises
    ------
    TypeError
        If *credentials_ref* is not a string.
    ValueError
        If it is empty, whitespace-only, or longer than the column allows.
    """
    if not isinstance(credentials_ref, str):
        raise TypeError(
            f"credentials_ref must be a string, got {type(credentials_ref).__name__}"
        )
    normalized = credentials_ref.strip()
    if not normalized:
        raise ValueError("credentials_ref must not be empty or whitespace-only")
    if len(normalized) > _CREDENTIALS_REF_MAX_LENGTH:
        raise ValueError(
            f"credentials_ref must be at most {_CREDENTIALS_REF_MAX_LENGTH} characters"
        )
    return normalized


# ---------------------------------------------------------------------------
# Status constant set
# ---------------------------------------------------------------------------

class AccountStatus:
    """Documented account status values.

    An account is NOT "connected" merely because its database record exists.
    "connected" means SiloVault has valid credentials + active adapter.

    Attributes
    ----------
    PENDING : str
        Initial state after account creation; no credentials attempted yet.
    CONNECTED : str
        Account has valid credentials and active adapter instance.
    DISCONNECTED : str
        Credentials revoked or adapter deliberately detached.
    ERROR : str
        Credential or adapter failure; account is non-functional.
    """
    PENDING = "pending"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


# ---------------------------------------------------------------------------
# AccountManager
# ---------------------------------------------------------------------------


class AccountManager:
    """Manages storage account identities, lifecycle, and adapter resolution.

    Responsibilities
    ----------------
    * Create, retrieve, update, and delete account records in ``storage_accounts``.
    * Track account status without assuming provider reachability.
    * Support multiple accounts for the same provider.
    * Return defensive copies of configuration JSON to prevent cross-contamination.

    NOT Responsible For
    ------------------
    * Adapter construction or resolution (deferred to adapter factories).
    * Credential storage or retrieval (deferred to ``backend.storage.credentials``).
    * Role assignment (deferred to Role Manager).
    * Google Drive OAuth or provider-specific authentication.
    * Application lifespan lifecycle (FastAPI startup/shutdown).

    Parameters
    ----------
    session : sqlalchemy.orm.Session
        Existing database session injected by the caller. The Account Manager
        does NOT create or manage its own engine or session.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Core account CRUD
    # ------------------------------------------------------------------

    def create_account(
        self,
        provider_id: str,
        display_name: str,
        credentials_ref: Optional[str] = None,
        configuration: Optional[Dict[str, Any]] = None,
    ) -> StorageAccount:
        """Create a new storage account.

        Parameters
        ----------
        provider_id : str
            Canonical provider identifier (e.g. ``"LOCAL"``, ``"GOOGLE_DRIVE"``).
            Validated using the same rules as ``registry.py``.
        display_name : str
            User-defined account name.
        credentials_ref : Optional[str]
            Opaque reference to account-specific credentials.
            ``None`` for accounts that do not use credential-based auth (e.g. LOCAL).
        configuration : Optional[Dict[str, Any]]
            Provider-specific configuration stored as JSON. Defaults to empty dict.

        Returns
        -------
        StorageAccount
            The newly created account instance.

        Raises
        ------
        ValueError
            If *provider_id* is invalid, *display_name* is empty,
            *credentials_ref* is empty/whitespace-only, or *credentials_ref*
            is already used by another account.
        TypeError
            If *credentials_ref* is not a string.
        """
        # Validate provider ID
        validated_id = _validate_provider_id(provider_id)

        # Validate display name
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("display_name must be a non-empty string")

        # Validate the credential reference before it is stored. A duplicate
        # reference would let two accounts share (or delete) one keyring entry.
        normalized_ref: Optional[str] = None
        if credentials_ref is not None:
            normalized_ref = _normalize_credentials_ref(credentials_ref)
            self._reject_credentials_ref_conflict(normalized_ref)

        # Check for existing account with same provider_id + display_name combo
        # (we allow same provider multiple times, but validate inputs)
        existing = self._session.query(StorageAccount).filter(
            StorageAccount.provider_id == validated_id
        ).first()
        # Note: we allow multiple accounts per provider; no count limit.

        # Create the account
        account = StorageAccount(
            provider_id=validated_id,
            display_name=display_name.strip(),
            credentials_ref=normalized_ref,
            configuration=configuration or {},
            status=AccountStatus.PENDING,
        )
        self._session.add(account)
        self._session.commit()
        self._session.refresh(account)
        return account

    def get_account(self, account_id: str) -> Optional[StorageAccount]:
        """Retrieve an account by its stable UUID identity.

        Parameters
        ----------
        account_id : str
            Stable UUID (36-char format, ``str(uuid.uuid4())``).

        Returns
        -------
        Optional[StorageAccount]
            The account record, or ``None`` if not found.
        """
        if not isinstance(account_id, str) or not account_id:
            return None
        return self._session.get(StorageAccount, account_id)

    def list_accounts(
        self, provider_id: Optional[str] = None
    ) -> List[StorageAccount]:
        """List storage accounts, optionally filtered by provider.

        Parameters
        ----------
        provider_id : Optional[str]
            If provided, only return accounts for this provider.
            Validated using the same rules as ``registry.py``.

        Returns
        -------
        List[StorageAccount]
            List of account records (may be empty).
        """
        if provider_id is not None:
            validated_id = _validate_provider_id(provider_id)
            return (
                self._session.query(StorageAccount)
                .filter(StorageAccount.provider_id == validated_id)
                .all()
            )
        return self._session.query(StorageAccount).all()

    # ------------------------------------------------------------------
    # Account update
    # ------------------------------------------------------------------

    def update_account(
        self,
        account_id: str,
        *,
        display_name: Optional[str] = None,
        credentials_ref: Optional[str] = None,
        configuration: Optional[Dict[str, Any]] = None,
        _on_commit: Optional[callable] = None,
    ) -> StorageAccount:
        """Update an existing account's details.

        Parameters
        ----------
        account_id : str
            The account's stable UUID identity.
        display_name : Optional[str]
            New display name; ``None`` leaves unchanged.
        credentials_ref : Optional[str]
            New credential reference; ``None`` leaves unchanged.
        configuration : Optional[Dict[str, Any]]
            New configuration dict; ``None`` leaves unchanged.
        _on_commit : Optional[callable]
            Optional callable invoked after successful commit; receives
            ``account_id`` as its only argument. Used for cache invalidation
            when credentials_ref changes.

        Returns
        -------
        StorageAccount
            The updated account instance.

        Raises
        ------
        ValueError
            If *account_id* is not found, *display_name* is empty,
            *credentials_ref* is empty/whitespace-only, or *credentials_ref*
            is already used by a *different* account.
        TypeError
            If *credentials_ref* is not a string.
        """
        account = self._session.get(StorageAccount, account_id)
        if account is None:
            raise ValueError(f"Account {account_id!r} not found")

        # Track whether credentials_ref actually changes
        credentials_ref_changed = False

        # Track whether storage_root changes for LOCAL accounts
        storage_root_changed = False

        if display_name is not None:
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError("display_name must be a non-empty string")
            account.display_name = display_name.strip()

        if credentials_ref is not None:
            normalized_ref = _normalize_credentials_ref(credentials_ref)
            # Check if this is a real change
            if account.credentials_ref != normalized_ref:
                credentials_ref_changed = True
            # Re-assigning this account's own reference (including a
            # whitespace variant of it) stays allowed; colliding with another
            # account's reference does not.
            self._reject_credentials_ref_conflict(normalized_ref, account_id=account.id)
            account.credentials_ref = normalized_ref

        if configuration is not None:
            # Check if storage_root changes for LOCAL accounts
            if account.provider_id == "LOCAL":
                old_storage_root = (
                    account.configuration.get("storage_root")
                    if isinstance(account.configuration, dict)
                    else None
                )
                new_storage_root = (
                    configuration.get("storage_root") if isinstance(configuration, dict) else None
                )
                # Compare using Path conversion to match AdapterFactory behavior
                # This handles str vs Path comparisons correctly without filesystem access
                if isinstance(old_storage_root, (str, Path)) and isinstance(
                    new_storage_root, (str, Path)
                ):
                    if Path(old_storage_root) != Path(new_storage_root):
                        storage_root_changed = True
                elif old_storage_root != new_storage_root:
                    # One is None/invalid, other is valid - definitely a change
                    storage_root_changed = True
            account.configuration = configuration

        self._session.commit()

        # Post-commit callback for side effects (e.g., cache invalidation)
        # Invoke immediately after successful commit, before refresh, so that
        # cache invalidation happens even if refresh fails. Only invoke if
        # credentials_ref or storage_root (for LOCAL accounts) actually changed.
        if _on_commit is not None and (credentials_ref_changed or storage_root_changed):
            _on_commit(account_id)

        self._session.refresh(account)

        return account

    def _reject_credentials_ref_conflict(
        self,
        normalized_ref: str,
        *,
        account_id: Optional[str] = None,
    ) -> None:
        """Raise ``ValueError`` if another account already uses this reference.

        Comparison is done in Python on trimmed values so that legacy rows
        stored with surrounding whitespace (before this validation existed)
        are still recognised as collisions.  The number of accounts is small
        and this only runs on create/update, so a full scan is acceptable.

        Parameters
        ----------
        normalized_ref : str
            Already-normalized reference to test.
        account_id : Optional[str]
            Account allowed to keep the reference (its own); ``None`` on create.
        """
        rows = (
            self._session.query(StorageAccount.id, StorageAccount.credentials_ref)
            .filter(StorageAccount.credentials_ref.isnot(None))
            .all()
        )
        for other_id, other_ref in rows:
            if account_id is not None and other_id == account_id:
                continue
            if isinstance(other_ref, str) and other_ref.strip() == normalized_ref:
                raise ValueError(
                    "credentials_ref is already in use by another storage account"
                )

    # ------------------------------------------------------------------
    # Account status
    # ------------------------------------------------------------------

    def get_account_status(self, account_id: str) -> str:
        """Return the current status of an account.

        Parameters
        ----------
        account_id : str
            The account's stable UUID identity.

        Returns
        -------
        str
            One of: ``AccountStatus.PENDING``, ``AccountStatus.CONNECTED``,
            ``AccountStatus.DISCONNECTED``, ``AccountStatus.ERROR``.

        Raises
        ------
        ValueError
            If *account_id* is not found.
        """
        account = self._session.get(StorageAccount, account_id)
        if account is None:
            raise ValueError(f"Account {account_id!r} not found")
        return account.status  # type: ignore[return-value]

    def disconnect_account(self, account_id: str) -> StorageAccount:
        """Disconnect an account by clearing its status and adapter state.

        Parameters
        ----------
        account_id : str
            The account's stable UUID identity.

        Returns
        -------
        StorageAccount
            The updated account instance with status set to ``"disconnected"``.

        Raises
        ------
        ValueError
            If *account_id* is not found.
        """
        account = self._session.get(StorageAccount, account_id)
        if account is None:
            raise ValueError(f"Account {account_id!r} not found")
        account.status = AccountStatus.DISCONNECTED
        self._session.commit()
        self._session.refresh(account)
        return account

    def mark_connected(self, account_id: str) -> StorageAccount:
        """Mark an account as connected after successful authentication.

        Used by account-scoped provider flows (e.g. Google Drive OAuth) to
        record that valid credentials and an active adapter now exist for the
        account. It does not load, store, or validate credentials itself.

        Parameters
        ----------
        account_id : str
            The account's stable UUID identity.

        Returns
        -------
        StorageAccount
            The updated account instance with status set to ``"connected"``.

        Raises
        ------
        ValueError
            If *account_id* is not found.
        """
        account = self._session.get(StorageAccount, account_id)
        if account is None:
            raise ValueError(f"Account {account_id!r} not found")
        account.status = AccountStatus.CONNECTED
        self._session.commit()
        self._session.refresh(account)
        return account

    # ------------------------------------------------------------------
    # Configuration access (defensive copies)
    # ------------------------------------------------------------------

    def get_account_configuration(self, account_id: str) -> Dict[str, Any]:
        """Return a defensive copy of an account's configuration.

        Parameters
        ----------
        account_id : str
            The account's stable UUID identity.

        Returns
        -------
        Dict[str, Any]
            A deep copy of the configuration dict. Returns empty dict if
            the account has no configuration or is not found.

        Raises
        ------
        ValueError
            If *account_id* is not found.
        """
        account = self._session.get(StorageAccount, account_id)
        if account is None:
            raise ValueError(f"Account {account_id!r} not found")
        return copy.deepcopy(account.configuration or {})  # deep defensive copy

