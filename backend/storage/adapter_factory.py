"""AdapterFactory for SiloVault multi-account storage architecture.

Creates adapter instances from ``StorageAccount`` objects based on
``provider_id``.  Only currently implemented adapters are supported:
``LOCAL`` and ``GOOGLE_DRIVE``.  Each call produces a fresh, independent
adapter instance — accounts using the same provider never share an adapter.

Design constraints (honoured from earlier inspections)
------------------------------------------------------
* Receives a ``StorageAccount`` object — no database queries, no credential
  retrieval, no authentication, no status modification.
* ``LOCAL`` adapter is constructed with ``configuration["storage_root"]`` from
  the account.  Missing or invalid ``storage_root`` raises ``ValueError``.
* ``GOOGLE_DRIVE`` adapter is constructed without credentials or authentication
  (the adapter's ``_credentials`` attribute will be ``None``).
* Unsupported provider IDs raise ``StorageUnsupportedError`` with the provider
  ID in its context.
* No ``CAPABILITY_MAP`` lookup is used to decide what is supported.
* No provider-account composite keys in the Registry — the Factory stays
  provider-level only.
* Multiple accounts using the same provider always get distinct adapter
  instances.
"""

from __future__ import annotations

from pathlib import Path

from backend.storage.errors import StorageUnsupportedError
from backend.storage.google_drive import GoogleDriveAdapter
from backend.storage.local import LocalAdapter


# ---------------------------------------------------------------------------
# AdapterFactory
# ---------------------------------------------------------------------------

class AdapterFactory:
    """Creates adapter instances from StorageAccount objects."""

    def __init__(self, account: "StorageAccount") -> None:
        if not hasattr(account, "provider_id"):
            raise TypeError(
                "AdapterFactory requires a StorageAccount instance with provider_id"
            )
        if not hasattr(account, "configuration"):
            raise TypeError(
                "AdapterFactory requires a StorageAccount instance with configuration"
            )
        if not isinstance(account.provider_id, str) or not account.provider_id:
            raise TypeError(
                "StorageAccount must have a valid provider_id"
            )

        self._account = account

    # --------------------------------------------------------------
    # Adapter creation
    # --------------------------------------------------------------

    def create_adapter(self) -> "StorageOperations":
        """Construct and return an adapter appropriate for this account.

        Returns
        -------
        StorageOperations
            A fresh adapter instance.

        Raises
        ------
        ValueError
            If the account is a ``LOCAL`` account and its
            ``configuration`` does not contain a valid ``storage_root``.
        StorageUnsupportedError
            If ``provider_id`` is not ``"LOCAL"`` or ``"GOOGLE_DRIVE"``.
        """
        provider_id = self._account.provider_id

        if provider_id == "LOCAL":
            return self._create_local_adapter()
        if provider_id == "GOOGLE_DRIVE":
            return self._create_google_drive_adapter()

        raise StorageUnsupportedError(
            operation="adapter creation",
            provider=provider_id,
        )

    # --------------------------------------------------------------
    # LOCAL adapter creation
    # --------------------------------------------------------------

    def _create_local_adapter(self) -> LocalAdapter:
        """Create a ``LocalAdapter`` from the account's configuration.

        The ``storage_root`` must be present in ``configuration`` and must be
        a non-empty string (or convertible to a ``Path``).

        Returns
        -------
        LocalAdapter
            A new local adapter instance.

        Raises
        ------
        ValueError
            If ``configuration`` is missing ``storage_root`` or it is empty.
        """
        configuration = self._account.configuration

        if not isinstance(configuration, dict):
            raise ValueError(
                "LOCAL account configuration must be a dict"
            )

        storage_root = configuration.get("storage_root")

        if not isinstance(storage_root, (str, Path)):
            raise ValueError("storage_root must be a non-empty string or Path")

        if not str(storage_root).strip():
            raise ValueError("storage_root must be a non-empty string or Path")

        # Normalise to ``Path`` — ``LocalAdapter.__init__`` accepts ``str | Path``.
        storage_root_path = Path(storage_root)
        if not storage_root_path.as_posix():
            raise ValueError(
                "LOCAL account configuration \"storage_root\" must be a non-empty string"
            )

        # Each call creates a fresh instance; accounts using the same provider
        # never share an adapter.
        return LocalAdapter(storage_root=storage_root_path)

    # --------------------------------------------------------------
    # GOOGLE_DRIVE adapter creation
    # --------------------------------------------------------------

    def _create_google_drive_adapter(self) -> GoogleDriveAdapter:
        """Create a ``GoogleDriveAdapter`` without credentials or authentication.

        The adapter's ``_credentials`` attribute will be ``None``; the caller
        must authenticate separately (e.g. via ``authenticate()``) before performing
        file operations.

        The account's ``credentials_ref`` (HVSA H1.4C) is passed to the adapter
        so that credential loads/stores and root-folder persistence are scoped
        to that account's own keyring entry. ``None`` preserves the legacy
        single-account entry.

        Returns
        -------
        GoogleDriveAdapter
            A new Google Drive adapter instance with ``_credentials is None``.
        """
        # Each call creates a fresh instance; accounts using the same provider
        # never share an adapter.
        return GoogleDriveAdapter(
            credentials=None,
            credentials_ref=getattr(self._account, "credentials_ref", None),
        )