"""Provider capability model for the storage abstraction layer.

Defines the feature flags each provider advertises so the application can
decide at runtime which operations to attempt.  Values that have not yet
been confirmed against official provider documentation carry the
``CapabilityStatus.UNKNOWN`` marker instead of a fabricated fact.

Design constraints
------------------
* Required fields appear before any field with a default value.
* ``frozen=True`` ensures immutability — callers may read but never mutate
  a capability snapshot after construction.
* No imports beyond the standard library; this module must remain free of
  SQLAlchemy, HTTP-client, or cloud-SDK dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, Optional


class AddressingMode(str, Enum):
    """How the provider identifies and addresses files."""

    UNKNOWN = "unknown"          # Unverified — await official documentation
    ID_BASED = "id_based"        # Confirmed id-centric providers
    PATH_BASED = "path_based"    # Confirmed path-centric providers
    HANDLE_BASED = "handle_based"  # Confirmed handle-centric providers
    MIXED = "mixed"              # Confirmed dual-addressing providers


class CapabilityStatus(Enum):
    """Three-valued flag for capabilities that are not yet verified.

    Used for every boolean-ish provider feature until the adapter's M3
    integration task confirms the behaviour against official API/SDK docs.
    ``UNKNOWN`` intentionally does not coerce to ``True`` or ``False`` —
    code that needs a concrete decision must inspect the value explicitly.
    """

    UNKNOWN = auto()
    TRUE = auto()
    FALSE = auto()

    def is_known(self) -> bool:
        """Return ``True`` only when the capability has been verified."""
        return self is not CapabilityStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Capability dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCapabilities:
    """Feature profile for a single storage provider.

    Parameters
    ----------
    addressing_mode:
        How the provider addresses files within its namespace.
    native_hash_algorithm:
        Hash name the provider computes natively (e.g. ``"md5"``).
        ``None`` when the algorithm has not been verified.
        SiloVault always computes SHA-256 locally regardless.
    supports_trash:
        Whether the provider offers a soft-delete / trash recycle bin.
    supports_delta_sync:
        Whether the provider exposes an incremental-change API.
    supports_revisions:
        Whether the provider retains file version history.
    supports_search:
        Whether the provider supports server-side keyword search.
    supports_chunked_upload:
        Whether the provider accepts uploads broken into chunks.
    supports_resumable_upload:
        Whether the provider supports resuming interrupted uploads.
    requires_decryption:
        Whether the provider's payload must be decrypted locally
        before any processing.
    """

    addressing_mode: AddressingMode
    native_hash_algorithm: Optional[str]
    supports_trash: CapabilityStatus
    supports_delta_sync: CapabilityStatus
    supports_revisions: CapabilityStatus
    supports_search: CapabilityStatus
    supports_chunked_upload: CapabilityStatus
    supports_resumable_upload: CapabilityStatus
    requires_decryption: CapabilityStatus = CapabilityStatus.FALSE


# ---------------------------------------------------------------------------
# Canonical capability registry
# ---------------------------------------------------------------------------

CAPABILITY_MAP: Dict[str, ProviderCapabilities] = {
    "LOCAL": ProviderCapabilities(
        addressing_mode=AddressingMode.PATH_BASED,
        native_hash_algorithm=None,
        supports_trash=CapabilityStatus.FALSE,
        supports_delta_sync=CapabilityStatus.FALSE,
        supports_revisions=CapabilityStatus.FALSE,
        supports_search=CapabilityStatus.FALSE,
        supports_chunked_upload=CapabilityStatus.FALSE,
        supports_resumable_upload=CapabilityStatus.FALSE,
        requires_decryption=CapabilityStatus.FALSE,
    ),
    "GOOGLE_DRIVE": ProviderCapabilities(
        addressing_mode=AddressingMode.UNKNOWN,
        native_hash_algorithm=None,
        supports_trash=CapabilityStatus.UNKNOWN,
        supports_delta_sync=CapabilityStatus.UNKNOWN,
        supports_revisions=CapabilityStatus.UNKNOWN,
        supports_search=CapabilityStatus.UNKNOWN,
        supports_chunked_upload=CapabilityStatus.UNKNOWN,
        supports_resumable_upload=CapabilityStatus.UNKNOWN,
        requires_decryption=CapabilityStatus.UNKNOWN,
    ),
    "ONEDRIVE": ProviderCapabilities(
        addressing_mode=AddressingMode.UNKNOWN,
        native_hash_algorithm=None,
        supports_trash=CapabilityStatus.UNKNOWN,
        supports_delta_sync=CapabilityStatus.UNKNOWN,
        supports_revisions=CapabilityStatus.UNKNOWN,
        supports_search=CapabilityStatus.UNKNOWN,
        supports_chunked_upload=CapabilityStatus.UNKNOWN,
        supports_resumable_upload=CapabilityStatus.UNKNOWN,
        requires_decryption=CapabilityStatus.UNKNOWN,
    ),
    "DROPBOX": ProviderCapabilities(
        addressing_mode=AddressingMode.UNKNOWN,
        native_hash_algorithm=None,
        supports_trash=CapabilityStatus.UNKNOWN,
        supports_delta_sync=CapabilityStatus.UNKNOWN,
        supports_revisions=CapabilityStatus.UNKNOWN,
        supports_search=CapabilityStatus.UNKNOWN,
        supports_chunked_upload=CapabilityStatus.UNKNOWN,
        supports_resumable_upload=CapabilityStatus.UNKNOWN,
        requires_decryption=CapabilityStatus.UNKNOWN,
    ),
    "BOX": ProviderCapabilities(
        addressing_mode=AddressingMode.UNKNOWN,
        native_hash_algorithm=None,
        supports_trash=CapabilityStatus.UNKNOWN,
        supports_delta_sync=CapabilityStatus.UNKNOWN,
        supports_revisions=CapabilityStatus.UNKNOWN,
        supports_search=CapabilityStatus.UNKNOWN,
        supports_chunked_upload=CapabilityStatus.UNKNOWN,
        supports_resumable_upload=CapabilityStatus.UNKNOWN,
        requires_decryption=CapabilityStatus.UNKNOWN,
    ),
    "MEGA": ProviderCapabilities(
        addressing_mode=AddressingMode.UNKNOWN,
        native_hash_algorithm=None,
        supports_trash=CapabilityStatus.UNKNOWN,
        supports_delta_sync=CapabilityStatus.UNKNOWN,
        supports_revisions=CapabilityStatus.UNKNOWN,
        supports_search=CapabilityStatus.UNKNOWN,
        supports_chunked_upload=CapabilityStatus.UNKNOWN,
        supports_resumable_upload=CapabilityStatus.UNKNOWN,
        requires_decryption=CapabilityStatus.UNKNOWN,
    ),
}
