"""Storage operation interfaces for SiloVault.

Defines two ABCs:

* :class:`StorageOperations` — mandatory file operations every provider must
  implement (upload, download, rename, move, quarantine/restore/cleanup).
* :class:`StorageLifecycle` — optional lifecycle hooks (authenticate, refresh,
  disconnect) that apply only to cloud providers.  LocalAdapter deliberately
  does not implement this interface, avoiding meaningless OAuth requirements.

Database-side responsibilities — listing files, metadata retrieval, and activity
logging — remain in the API/service layer and are intentionally excluded from
these interfaces.

Deletion safety protocol
------------------------
The approved deletion workflow is a three-phase transaction coordinated by the
service/API layer:

1. ``adapter.quarantine(provider_file_id) -> quarantine_id``
   Move the physical file into a quarantine area.  The adapter returns an
   opaque identifier that the service layer records alongside the database row.

2. Database deletion + commit in the service/API layer.

3a. On commit success: ``adapter.cleanup(quarantine_id)`` — permanently remove
     the quarantined file.
3b. On commit failure: rollback, then ``adapter.restore(quarantine_id)`` — move
     the file back to its original location so no physical file is orphaned.

The adapter never touches the database; it only performs physical filesystem or
cloud operations.  This keeps the interface provider-neutral: LocalAdapter moves
files within the local quarantine directory, while cloud adapters map the same
three operations onto their trash / recycle-bin semantics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import ProviderFileRecord


class StorageOperations(ABC):
    """Mandatory operations every storage adapter must support.

    File listing and metadata retrieval are database responsibilities and are
    not included here — see the API/service layer for those concerns.
    """

    @abstractmethod
    def upload(self, file_data: bytes, name: str, logical_path: str) -> ProviderFileRecord:
        """Upload a file to the storage provider.

        Parameters
        ----------
        file_data : bytes
            Raw plaintext bytes to store.
        name : str
            Original user-facing filename.
        logical_path : str
            Logical path within the provider namespace (e.g. ``"/Invoices/2026.pdf"``).

        Returns
        -------
        ProviderFileRecord
            Normalized record describing the newly stored file.

        Raises
        ------
        StorageConflictError
            If a file with the same logical path already exists and the
            provider does not allow overwrites.
        StorageQuotaExceededError
            If the provider reports insufficient remaining capacity.
        """
        ...

    @abstractmethod
    def download(self, provider_file_id: str) -> bytes:
        """Download a file by its provider-assigned identifier.

        Parameters
        ----------
        provider_file_id : str
            Opaque native identifier assigned by the provider.

        Returns
        -------
        bytes
            Raw plaintext bytes of the downloaded file.

        Raises
        ------
        StorageNotFoundError
            If no file exists under the given identifier.
        """
        ...

    @abstractmethod
    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        """Rename a file in place within the same logical path.

        Parameters
        ----------
        provider_file_id : str
            Opaque native identifier of the file to rename.
        new_name : str
            New user-facing filename.

        Returns
        -------
        ProviderFileRecord
            Updated record reflecting the new name.
        """
        ...

    @abstractmethod
    def move(self, source_provider_file_id: str, target_logical_path: str) -> ProviderFileRecord:
        """Move a file to a different logical path inside the same provider.

        Parameters
        ----------
        source_provider_file_id : str
            Opaque native identifier of the file to move.
        target_logical_path : str
            New logical path (directory prefix + filename).

        Returns
        -------
        ProviderFileRecord
            Updated record reflecting the new path.

        Raises
        ------
        StorageConflictError
            If the target path is occupied and the provider does not allow
            overwriting.
        """
        ...

    @abstractmethod
    def quarantine(self, provider_file_id: str) -> str:
        """Move a file into the provider's quarantine area.

        Phase 1 of the approved deletion safety protocol.  The service/API layer
        calls this BEFORE committing a database deletion.  The returned
        identifier is recorded alongside the database row so it can be passed
        verbatim to :meth:`restore` and :meth:`cleanup`.

        The adapter performs only a physical move (filesystem or cloud trash); it
        does **not** touch the database.

        Returns
        -------
        str
            Quarantine location identifier.  Opaque to SiloVault — handed back
            unchanged to :meth:`restore` and :meth:`cleanup`.

        Raises
        ------
        StorageNotFoundError
            If no file exists under the given identifier.
        """
        ...

    @abstractmethod
    def restore(self, quarantine_id: str) -> ProviderFileRecord:
        """Restore a quarantined file back to its original location.

        Phase 3b of the deletion safety protocol.  Called by the service/API layer
        when a database transaction fails AFTER :meth:`quarantine` succeeded, so
        the physical file is not orphaned by a half-committed deletion.

        Returns
        -------
        ProviderFileRecord
            Updated record reflecting the restored location.

        Raises
        ------
        StorageNotFoundError
            If no quarantined file exists under the given identifier.
        """
        ...

    @abstractmethod
    def cleanup(self, quarantine_id: str) -> None:
        """Permanently remove a file from quarantine.

        Phase 3a of the deletion safety protocol.  Called by the service/API layer
        AFTER a database transaction commits successfully, so the physical file
        is no longer needed.

        Raises
        ------
        StorageNotFoundError
            If no quarantined file exists under the given identifier.
        """
        ...


class StorageLifecycle(ABC):
    """Optional lifecycle hooks for cloud providers.

    Local storage does **not** implement this interface — authentication, token
    refresh, and disconnection are not meaningful concepts for the local
    filesystem backend.

    Cloud adapters that require sign-in, token rotation, or session cleanup
    should implement both :class:`StorageOperations` and this ABC.
    """

    @abstractmethod
    def authenticate(self, config: dict[str, Any]) -> None:
        """Authenticate with the provider using the supplied credentials.

        Parameters
        ----------
        config : dict[str, Any]
            Provider-specific credential payload (client ID, refresh token, etc.).
        """
        ...

    @abstractmethod
    def refresh(self) -> None:
        """Refresh an expired authentication token."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Clean up authenticated-session resources (tokens, handles, …)."""
        ...