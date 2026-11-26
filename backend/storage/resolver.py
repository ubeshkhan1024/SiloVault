"""Account-aware adapter resolver for SiloVault HVSA H1.5.

Acts as a compatibility façade delegating adapter caching, single-flight locking,
and resolution to the central ``AccountAdapterManager``.
"""

from __future__ import annotations

from typing import Callable, Optional

from sqlalchemy.orm import Session

from backend.storage.account_adapter_manager import (
    AccountAdapterManager,
    adapter_manager as default_adapter_manager,
)
from backend.storage.interfaces import StorageOperations


class AccountResolver:
    """Delegates account adapter resolution to the central ``AccountAdapterManager``.

    Provides backwards compatibility for code expecting ``AccountResolver(session_factory)``.
    Does NOT maintain a separate cache.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        manager: Optional[AccountAdapterManager] = None,
    ) -> None:
        """Initialize the resolver façade.

        Parameters
        ----------
        session_factory : callable
            A zero-argument callable returning a new SQLAlchemy ``Session``.
        manager : Optional[AccountAdapterManager]
            Optional manager instance. If omitted, creates a new manager instance
            configured with *session_factory* (so each test gets isolated state).
            The process-wide default manager is NOT used automatically to avoid
            cross-test contamination.
        """
        self._session_factory = session_factory
        if manager is not None:
            self._manager = manager
            if self._manager._session_factory is None:
                self._manager.configure(session_factory)
        else:
            # Create a fresh manager per resolver for test isolation
            self._manager = AccountAdapterManager(session_factory)

    def resolve(self, account_id: str) -> StorageOperations:
        """Resolve and return the adapter for the given account ID."""
        return self._manager.resolve(account_id)

    def invalidate_account(self, account_id: str) -> None:
        """Remove the cached adapter for a specific account."""
        self._manager.invalidate_account(account_id)

    def invalidate_all(self) -> None:
        """Remove all cached adapters from the manager."""
        self._manager.invalidate_all()

    def resolve_legacy(self, provider: str) -> StorageOperations:
        """Legacy fallback: resolve adapter using the provider-level registry."""
        return self._manager.resolve_legacy(provider)

    @property
    def cache(self) -> dict[str, StorageOperations]:
        """Return a read-only view of the current adapter cache."""
        return self._manager.cache

    # Internal property for legacy test compatibility (accessing _cache attribute)
    @property
    def _cache(self) -> dict[str, StorageOperations]:
        return self._manager.cache
