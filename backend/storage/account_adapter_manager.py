"""Central AccountAdapterManager for SiloVault HVSA H1.5.2A.

Owns account-scoped adapter caching, resolution, single-flight locking per account,
and generation-guarded cache invalidation.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from backend.models.storage_account import StorageAccount
from backend.storage.account_manager import AccountManager, AccountStatus
from backend.storage.adapter_factory import AdapterFactory
from backend.storage.credentials import (
    CredentialStorageError,
    load_google_drive_credentials,
)
from backend.storage.errors import StorageUnavailableError, StorageUnsupportedError
from backend.storage.interfaces import StorageOperations
from backend.storage.registry import lookup


class AccountAdapterManager:
    """Central manager owning account-scoped adapter caching and resolution.

    Responsibilities
    ----------------
    * Maintain the single authoritative runtime cache of account-scoped adapters.
    * Provide thread-safe resolution of storage adapters given an ``account_id``.
    * Ensure single-flight resolution per account so concurrent requests for the
      same account do not trigger duplicate credential loads or authentications.
    * Allow independent resolution for different accounts concurrently.
    * Enforce generation-based protection against stale cache installations if
      invalidation occurs during adapter construction.
    * Explicitly load account-scoped credentials for cloud providers (e.g. Google
      Drive) without falling back to legacy credentials.
    * Reject unauthenticated or failed adapters from being cached.
    """

    def __init__(self, session_factory: Optional[Callable[[], Session]] = None) -> None:
        self._session_factory = session_factory
        self._cache: Dict[str, StorageOperations] = {}
        self._cache_lock = threading.Lock()
        self._account_generations: Dict[str, int] = {}
        self._global_generation: int = 0

        # Per-account resolution locks for single-flight resolution
        self._resolving_locks: Dict[str, list] = {}
        self._resolving_guard = threading.Lock()

    def configure(self, session_factory: Callable[[], Session]) -> None:
        """Configure the database session factory for account status checks.

        Must be called during application startup before ``resolve`` is invoked.
        """
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Single-flight per-account lock helper
    # ------------------------------------------------------------------

    def _get_account_resolution_lock(self, account_id: str) -> Tuple[threading.Lock, list]:
        """Get or create a reference-counted resolution lock for an account."""
        with self._resolving_guard:
            entry = self._resolving_locks.get(account_id)
            if entry is None:
                entry = [threading.Lock(), 0]
                self._resolving_locks[account_id] = entry
            entry[1] += 1
            return entry[0], entry

    def _release_account_resolution_lock(self, account_id: str, entry: list, lock: threading.Lock, acquired: bool) -> None:
        """Release resolution lock and clean up reference count."""
        if acquired:
            lock.release()
        with self._resolving_guard:
            entry[1] -= 1
            if entry[1] == 0 and self._resolving_locks.get(account_id) is entry:
                del self._resolving_locks[account_id]

    # ------------------------------------------------------------------
    # Resolution API
    # ------------------------------------------------------------------

    def resolve(self, account_id: str) -> StorageOperations:
        """Resolve and return the storage adapter for the given account ID.

        Parameters
        ----------
        account_id : str
            The stable UUID of the ``StorageAccount``.

        Returns
        -------
        StorageOperations
            An authenticated storage adapter for the specified account.

        Raises
        ------
        RuntimeError
            If ``session_factory`` has not been configured.
        ValueError
            If the account does not exist, is not connected, or missing credentials/authentication.
        StorageUnavailableError
            If the credential store is locked/unavailable, or repeated invalidation occurs.
        """
        if self._session_factory is None:
            raise RuntimeError(
                "AccountAdapterManager has not been configured with a session_factory"
            )

        max_retries = 3
        for attempt in range(max_retries + 1):
            # 1. Quick cache check under cache lock - ONLY read, no DB ops
            with self._cache_lock:
                cached = self._cache.get(account_id)

            if cached is not None:
                # Validate DB status OUTSIDE cache lock
                session = self._session_factory()
                try:
                    account = AccountManager(session).get_account(account_id)
                    if account is not None and account.status == AccountStatus.CONNECTED:
                        return cached
                finally:
                    session.close()

                # Account disconnected or missing - evict under lock
                with self._cache_lock:
                    # Only evict if still the same cached instance (prevents
                    # evicting a concurrently installed replacement)
                    current = self._cache.get(account_id)
                    if current is cached:
                        del self._cache[account_id]
                # Fall through to re-resolution

            # 2. Acquire per-account single-flight resolution lock
            lock, lock_entry = self._get_account_resolution_lock(account_id)
            acquired = False
            try:
                lock.acquire()
                acquired = True

                # Double-check cache inside per-account lock
                with self._cache_lock:
                    cached = self._cache.get(account_id)

                if cached is not None:
                    # Validate DB status OUTSIDE cache lock
                    session = self._session_factory()
                    try:
                        account = AccountManager(session).get_account(account_id)
                        if account is not None and account.status == AccountStatus.CONNECTED:
                            return cached
                    finally:
                        session.close()

                    # Evict under lock with identity check
                    with self._cache_lock:
                        current = self._cache.get(account_id)
                        if current is cached:
                            del self._cache[account_id]

                    # Fall through to re-resolution

                # Get generation BEFORE releasing lock (race window closed)
                with self._cache_lock:
                    start_gen = (
                        self._account_generations.get(account_id, 0),
                        self._global_generation,
                    )

                # 3. Load account from database (outside all locks)
                session = self._session_factory()
                try:
                    account_manager = AccountManager(session)
                    account = account_manager.get_account(account_id)

                    if account is None:
                        raise ValueError(f"Storage account {account_id!r} not found")

                    if account.status != AccountStatus.CONNECTED:
                        raise ValueError(
                            f"Storage account {account_id!r} is not connected "
                            f"(status: {account.status})"
                        )

                    provider_id = account.provider_id
                    credentials_ref = getattr(account, "credentials_ref", None)
                    account_config = account.configuration
                finally:
                    session.close()

                # 4. Construct adapter using AdapterFactory ONLY (without lock)
                factory = AdapterFactory(account)
                adapter = factory.create_adapter()

                # 5. Cloud provider credential loading and authentication (Google Drive)
                # OUTSIDE cache lock
                if provider_id == "GOOGLE_DRIVE":
                    if not credentials_ref or not isinstance(credentials_ref, str) or not credentials_ref.strip():
                        raise ValueError(
                            f"Storage account {account_id!r} has no credentials_ref"
                        )

                    try:
                        stored_creds = load_google_drive_credentials(
                            credentials_ref=credentials_ref
                        )
                    except CredentialStorageError as exc:
                        raise StorageUnavailableError(
                            detail=f"Credential store unavailable for account {account_id!r}: {exc}",
                            provider=provider_id,
                        ) from exc

                    if stored_creds is None:
                        raise ValueError(
                            f"Storage account {account_id!r} has no stored credentials in keyring"
                        )

                    config = {
                        "refresh_token": stored_creds.refresh_token,
                        "client_id": stored_creds.client_id,
                        "client_secret": stored_creds.client_secret,
                        "scopes": list(stored_creds.scopes),
                    }

                    try:
                        adapter.authenticate(config)
                    except Exception as exc:
                        # Failed authentication — NEVER cache this adapter.
                        # This resolution never installed its adapter (install
                        # happens only after successful authentication), so the
                        # only entry that could be present is a NEWER adapter
                        # installed concurrently by another caller. Evict it only
                        # if it is still the very adapter we built (identity),
                        # which is never true here — so a concurrently installed
                        # replacement is never removed by a stale failure.
                        with self._cache_lock:
                            if self._cache.get(account_id) is adapter:
                                del self._cache[account_id]
                        raise ValueError(
                            f"Storage account {account_id!r} authentication failed: {exc}"
                        ) from exc

                # 6. Check generations and install in cache under cache_lock
                with self._cache_lock:
                    current_gen = (
                        self._account_generations.get(account_id, 0),
                        self._global_generation,
                    )
                    if current_gen == start_gen:
                        self._cache[account_id] = adapter
                        return adapter

                    # Invalidation occurred during construction — retry if attempts remain
                    if attempt < max_retries:
                        continue

                raise StorageUnavailableError(
                    detail=(
                        f"Storage account {account_id!r} was invalidated "
                        f"repeatedly during adapter construction."
                    ),
                    provider=provider_id,
                )

            finally:
                self._release_account_resolution_lock(account_id, lock_entry, lock, acquired)

        raise StorageUnavailableError(
            detail=f"Failed to resolve adapter for account {account_id!r} after {max_retries} retries.",
            provider="UNKNOWN",
        )

    # ------------------------------------------------------------------
    # Installation and Invalidation
    # ------------------------------------------------------------------

    def install(self, account_id: str, adapter: StorageOperations) -> None:
        """Explicitly install a pre-constructed/authenticated adapter into the cache."""
        with self._cache_lock:
            self._account_generations[account_id] = (
                self._account_generations.get(account_id, 0) + 1
            )
            self._cache[account_id] = adapter

    def invalidate_account(self, account_id: str) -> None:
        """Evict the cached adapter for a specific account."""
        with self._cache_lock:
            self._account_generations[account_id] = (
                self._account_generations.get(account_id, 0) + 1
            )
            self._cache.pop(account_id, None)

    def invalidate_all(self) -> None:
        """Evict all cached adapters from the manager."""
        with self._cache_lock:
            self._global_generation += 1
            self._cache.clear()

    # ------------------------------------------------------------------
    # Legacy Fallback
    # ------------------------------------------------------------------

    def resolve_legacy(self, provider: str) -> StorageOperations:
        """Legacy fallback using provider-level registry."""
        try:
            adapter, _ = lookup(provider)
            return adapter
        except Exception as exc:
            raise StorageUnavailableError(
                detail=(
                    f"Provider {provider!r} is not registered in legacy registry."
                ),
                provider=provider,
            ) from exc

    # ------------------------------------------------------------------
    # Cache Inspection Property
    # ------------------------------------------------------------------

    @property
    def cache(self) -> Dict[str, StorageOperations]:
        """Return a defensive copy of the cached adapters map."""
        with self._cache_lock:
            return dict(self._cache)


# Global singleton instance for application runtime
adapter_manager = AccountAdapterManager()
