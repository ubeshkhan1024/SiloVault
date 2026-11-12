"""Provider registry for SiloVault's storage abstraction layer.

Provides explicit registration and lookup of storage adapters keyed by a
canonical provider identifier.  Registration happens during application lifespan
setup; there is no import-time self-registration.

Usage pattern
-------------
1. Create an adapter instance::

       adapter = LocalAdapter(...)

2. Register it during FastAPI lifespan setup::

       from backend.storage.registry import register
       from backend.storage.capabilities import CAPABILITY_MAP
       register("LOCAL", adapter, CAPABILITY_MAP["LOCAL"])

3. Look up later from endpoint code::

       from backend.storage.registry import lookup
       adapter, caps = lookup("LOCAL")

   Or grab just the capability snapshot::

       from backend.storage.registry import get_capabilities
       caps = get_capabilities("LOCAL")

Unknown or invalid provider identifiers raise :exc:`~backend.storage.errors.StorageUnavailableError`.

Provider ID rules
-----------------
* Provider IDs are **case-sensitive** and must be uppercase strings containing
  only ASCII letters, digits, and underscores (e.g. ``"LOCAL"``,
  ``"GOOGLE_DRIVE"``).
* Leading or trailing whitespace is rejected.
* Empty strings are rejected.
* Non-string types raise :exc:`TypeError`.
* The registry does not restrict registration to a fixed set of known providers;
  new provider IDs matching the pattern are accepted.
"""

from __future__ import annotations

import re
from typing import Tuple

from .errors import StorageUnavailableError
from .interfaces import StorageOperations
from .capabilities import ProviderCapabilities


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_adapter_map: dict[str, StorageOperations] = {}
_capability_map: dict[str, ProviderCapabilities] = {}


# ---------------------------------------------------------------------------
# Provider ID validation
# ---------------------------------------------------------------------------

# Canonical pattern: uppercase ASCII letters, digits, underscores. Must start with a letter.
_PROVIDER_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _validate_provider_id(provider_id: str) -> str:
    """Validate a provider identifier.

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
    StorageUnavailableError
        If *provider_id* is empty, has leading/trailing whitespace, or does not
        match the canonical uppercase pattern.
    """
    if not isinstance(provider_id, str):
        raise TypeError(
            f"provider_id must be a string, got {type(provider_id).__name__}"
        )
    if not provider_id:
        raise StorageUnavailableError(
            detail="Provider ID must be a non-empty string",
            provider=provider_id,
        )
    if provider_id != provider_id.strip():
        raise StorageUnavailableError(
            detail="Provider ID must not have leading or trailing whitespace",
            provider=provider_id,
        )
    if not _PROVIDER_ID_PATTERN.fullmatch(provider_id):
        raise StorageUnavailableError(
            detail=(
                "Provider ID must be uppercase ASCII letters, digits, and "
                "underscores, starting with a letter"
            ),
            provider=provider_id,
        )
    return provider_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register(
    provider_id: str,
    adapter: StorageOperations,
    capabilities: ProviderCapabilities,
) -> None:
    """Register a storage adapter under the given canonical provider ID.

    Parameters
    ----------
    provider_id : str
        Canonical identifier (e.g. ``"LOCAL"``, ``"GOOGLE_DRIVE"``).
    adapter : StorageOperations
        Adapter instance implementing the mandatory operations interface.
    capabilities : ProviderCapabilities
        Feature profile describing what this provider supports.

    Raises
    ------
    StorageUnavailableError
        If a different adapter is already registered under this provider ID.
        Call :func:`replace` to overwrite an existing registration deliberately.
    TypeError
        If *adapter* is not an instance of :class:`StorageOperations` or
        *capabilities* is not a :class:`ProviderCapabilities` instance.
    """
    provider_id = _validate_provider_id(provider_id)
    if not isinstance(adapter, StorageOperations):
        raise TypeError(
            f"adapter must be a StorageOperations instance, got {type(adapter).__name__}"
        )
    if not isinstance(capabilities, ProviderCapabilities):
        raise TypeError(
            f"capabilities must be a ProviderCapabilities instance, got {type(capabilities).__name__}"
        )
    if provider_id in _adapter_map:
        raise StorageUnavailableError(
            detail=(
                f"Provider {provider_id!r} is already registered. "
                f"Call replace() to overwrite deliberately."
            ),
            provider=provider_id,
        )
    _adapter_map[provider_id] = adapter
    _capability_map[provider_id] = capabilities


def replace(
    provider_id: str,
    adapter: StorageOperations,
    capabilities: ProviderCapabilities,
) -> None:
    """Replace an existing provider registration.

    Unlike :func:`register`, this method silently overwrites any prior
    registration for the same provider ID — use it only when you intend to
    swap implementations deliberately (e.g. during testing or hot-reload).

    Parameters
    ----------
    provider_id : str
        Canonical identifier to replace.
    adapter : StorageOperations
        New adapter instance.
    capabilities : ProviderCapabilities
        New capability profile.

    Raises
    ------
    TypeError
        If *adapter* or *capabilities* have wrong types.
    StorageUnavailableError
        If the provider ID has never been registered.
    """
    provider_id = _validate_provider_id(provider_id)
    if provider_id not in _adapter_map:
        raise StorageUnavailableError(
            detail=f"Provider {provider_id!r} is not registered. Call register() first.",
            provider=provider_id,
        )
    if not isinstance(adapter, StorageOperations):
        raise TypeError(
            f"adapter must be a StorageOperations instance, got {type(adapter).__name__}"
        )
    if not isinstance(capabilities, ProviderCapabilities):
        raise TypeError(
            f"capabilities must be a ProviderCapabilities instance, got {type(capabilities).__name__}"
        )
    _adapter_map[provider_id] = adapter
    _capability_map[provider_id] = capabilities


def remove(provider_id: str) -> None:
    """Remove a provider registration entirely.

    Parameters
    ----------
    provider_id : str
        Canonical identifier to remove.

    Raises
    ------
    StorageUnavailableError
        If the provider ID is not currently registered.
    """
    provider_id = _validate_provider_id(provider_id)
    if provider_id not in _adapter_map:
        raise StorageUnavailableError(
            detail=f"Provider {provider_id!r} is not registered.",
            provider=provider_id,
        )
    del _adapter_map[provider_id]
    del _capability_map[provider_id]


def lookup(provider_id: str) -> Tuple[StorageOperations, ProviderCapabilities]:
    """Look up a registered adapter and its capabilities.

    Parameters
    ----------
    provider_id : str
        Canonical identifier to look up.

    Returns
    -------
    tuple[StorageOperations, ProviderCapabilities]
        The adapter instance and its immutable capability profile.

    Raises
    ------
    StorageUnavailableError
        If no adapter is registered under this provider ID.
    """
    provider_id = _validate_provider_id(provider_id)
    if provider_id not in _adapter_map:
        raise StorageUnavailableError(
            detail=(
                f"Unregistered provider: {provider_id!r}. "
                f"Call register() before lookup()."
            ),
            provider=provider_id,
        )
    return _adapter_map[provider_id], _capability_map[provider_id]


def get_capabilities(provider_id: str) -> ProviderCapabilities:
    """Convenience accessor returning only the capability profile.

    Parameters
    ----------
    provider_id : str
        Canonical identifier to look up.

    Returns
    -------
    ProviderCapabilities
        Immutable feature profile for the provider.

    Raises
    ------
    StorageUnavailableError
        If the provider is not registered.
    """
    provider_id = _validate_provider_id(provider_id)
    _, caps = lookup(provider_id)
    return caps