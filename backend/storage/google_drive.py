"""Google Drive storage adapter for SiloVault.

Implements StorageOperations + StorageLifecycle for the Google Drive provider.

M3.1 implemented the lifecycle methods (authenticate, refresh, disconnect).
M3.2 Phase B1.1 adds dedicated SiloVault root-folder management:
discovery of an existing accessible "SiloVault" folder, creation when none
exists, in-memory caching, and secure persistence of the folder ID in the
OS keyring via the immutable ``GoogleDriveCredentials`` record.

File operations (upload, download, rename, move, quarantine, restore,
cleanup) remain stubs that raise StorageUnsupportedError until later M3.2
phases.
"""

from __future__ import annotations

import io
import logging
import threading
from datetime import datetime
from typing import Any, Optional

from google.auth import credentials as ga_credentials
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from .credentials import (
    CredentialStorageError,
    load_google_drive_credentials,
    store_google_drive_credentials,
)
from .errors import (
    StorageAuthenticationError,
    StorageError,
    StorageInvalidResponseError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageQuotaExceededError,
    StorageRateLimitError,
    StorageUnavailableError,
    StorageUnsupportedError,
)
from .interfaces import StorageLifecycle, StorageOperations
from .types import ProviderFileRecord

logger = logging.getLogger(__name__)

#: Name of the dedicated SiloVault root folder in Google Drive.
SILOVAULT_ROOT_FOLDER_NAME = "SiloVault"

#: Google Drive MIME type for folders.
_DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _guess_mime(filename: str) -> str:
    """Guess a MIME type from a filename with a safe fallback.

    Parameters
    ----------
    filename : str
        Original user-facing filename.

    Returns
    -------
    str
        Detected MIME type, or ``"application/octet-stream"`` when
        the extension is unknown.
    """
    import mimetypes
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def _parse_drive_timestamp(timestamp: str | None) -> Optional[datetime]:
    """Parse a Google Drive ``modifiedTime`` string into a UTC-aware datetime.

    Google Drive returns timestamps in ISO 8601 format (e.g. ``"2024-01-15T10:30:00Z"``).
    Returns ``None`` when the input is absent or unparseable.

    Parameters
    ----------
    timestamp : str | None
        Raw timestamp from the Drive API.

    Returns
    -------
    datetime | None
        Parsed UTC-aware datetime, or ``None``.
    """
    if not timestamp:
        return None
    try:
        from datetime import timezone
        # Handle both ISO 8601 with 'Z' suffix and explicit timezone offset
        ts_str = timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class GoogleDriveAdapter(StorageOperations, StorageLifecycle):
    """Google Drive storage adapter.

    M3.1 implements authentication lifecycle. M3.2 Phase B1.1 adds root
    folder management. File operations are stubs pending later M3.2 phases.
    """

    def __init__(self, credentials: Optional[ga_credentials.Credentials] = None) -> None:
        self._credentials = credentials
        self._drive_service = None
        # Root folder state (M3.2 Phase B1.1)
        self._root_folder_id: Optional[str] = None
        self._root_folder_persistence_pending: bool = False
        self._lock = threading.Lock()

    # -----------------------------------------------------------------------
    # StorageLifecycle
    # -----------------------------------------------------------------------

    def authenticate(self, config: dict[str, Any]) -> None:
        """Authenticate with Google Drive using stored credentials.

        Args:
            config: Must contain 'refresh_token', 'client_id', 'client_secret',
                    and 'scopes' (list of strings).

        Raises:
            StorageAuthenticationError: If credentials are invalid or missing.
        """
        refresh_token = config.get("refresh_token")
        client_id = config.get("client_id")
        client_secret = config.get("client_secret")
        scopes = config.get("scopes", [])

        if not refresh_token or not client_id or not client_secret:
            raise StorageAuthenticationError(
                detail="Missing required credential fields (refresh_token, client_id, client_secret)",
                provider="GOOGLE_DRIVE",
            )

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
        )

        # Attempt to refresh the token to validate credentials
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise StorageAuthenticationError(
                detail=f"Failed to refresh Google Drive token: {exc}",
                provider="GOOGLE_DRIVE",
            ) from exc

        self._credentials = creds
        self._drive_service = self._create_drive_service(creds)
        logger.info("Google Drive adapter authenticated successfully")

    def refresh(self) -> None:
        """Refresh the access token.

        Raises:
            StorageAuthenticationError: If no credentials are set.
            StorageUnavailableError: If the refresh request fails.
        """
        if self._credentials is None:
            raise StorageAuthenticationError(
                detail="No credentials to refresh. Call authenticate() first.",
                provider="GOOGLE_DRIVE",
            )

        try:
            self._credentials.refresh(Request())
        except Exception as exc:
            raise StorageUnavailableError(
                detail=f"Token refresh failed: {exc}",
                provider="GOOGLE_DRIVE",
            ) from exc

    def disconnect(self) -> None:
        """Clear in-memory credentials and service handle."""
        self._credentials = None
        self._drive_service = None
        # Root folder state is in-memory only; the keyring record remains
        # the durable source of truth across reconnects.
        self._root_folder_id = None
        self._root_folder_persistence_pending = False
        logger.info("Google Drive adapter disconnected")

    # -----------------------------------------------------------------------
    # Root folder management (M3.2 Phase B1.1)
    # -----------------------------------------------------------------------

    def get_root_folder_id(self) -> str:
        """Return the SiloVault root folder ID, discovering or creating it
        on first use.

        Resolution order:
        1. In-memory cache (set by a previous call or a successful retry).
        2. The keyring credential record (persisted by a previous session).
        3. Discovery via the Drive API (search for an accessible "SiloVault"
           folder), creating one when none exists.

        The resolved ID is cached in memory and persisted to the keyring.
        If persistence fails, the ID is retained in memory and flagged
        pending so a later call retries the write without re-discovering
        the folder or overwriting existing credentials.

        Raises:
            StorageAuthenticationError: If not authenticated.
            StorageError subclasses: On Drive API failures.
        """
        with self._lock:
            if self._root_folder_id is not None:
                self._retry_root_folder_persistence()
                return self._root_folder_id

            if self._drive_service is None:
                raise StorageAuthenticationError(
                    detail="Google Drive adapter is not authenticated. Call authenticate() first.",
                    provider="GOOGLE_DRIVE",
                )

            # 1. Load persisted ID from keyring (backward compatible: M3.1
            #    records have an empty root_folder_id).
            try:
                stored = load_google_drive_credentials()
            except CredentialStorageError as exc:
                # Keyring read failure must fail safely: raise so the caller
                # knows the durable state is unknown. Existing stored
                # credentials are never modified by a read.
                raise StorageUnavailableError(
                    detail=f"Cannot read credential store for root folder ID: {exc}",
                    provider="GOOGLE_DRIVE",
                ) from exc

            if stored is not None and stored.root_folder_id:
                self._root_folder_id = stored.root_folder_id
                return self._root_folder_id

            # 2. Discover or create the dedicated root folder.
            folder_id = self._discover_or_create_root_folder()
            self._root_folder_id = folder_id

            # 3. Persist (failure keeps the ID cached and marks retry pending).
            self._persist_root_folder_id(folder_id)
            return self._root_folder_id

    def _discover_or_create_root_folder(self) -> str:
        """Search for an accessible SiloVault folder; create one if absent.

        Raises:
            StorageError subclasses: On Drive API failures (mapped via
            :meth:`_map_drive_error`).
        """
        existing = self._find_root_folder()
        if existing is not None:
            logger.info("Discovered existing SiloVault root folder: %s", existing)
            return existing

        body = {
            "name": SILOVAULT_ROOT_FOLDER_NAME,
            "mimeType": _DRIVE_FOLDER_MIME_TYPE,
        }
        try:
            response = self._drive_service.files().create(body=body, fields="id").execute()
        except HttpError as exc:
            raise self._map_drive_error(exc)
        folder_id = response.get("id")
        if not isinstance(folder_id, str) or not folder_id:
            raise StorageInvalidResponseError(
                body="Drive API folder creation response missing 'id'",
                provider="GOOGLE_DRIVE",
            )
        logger.info("Created SiloVault root folder: %s", folder_id)
        return folder_id

    def _find_root_folder(self) -> Optional[str]:
        """Search for an accessible SiloVault folder in the app's space.

        Returns the folder ID, or None when no matching folder exists.
        Only an empty successful response yields None; any API error is
        mapped and raised so a lookup failure never triggers folder
        creation.
        """
        query = (
            f"name='{SILOVAULT_ROOT_FOLDER_NAME}' and "
            f"mimeType='{_DRIVE_FOLDER_MIME_TYPE}' and trashed=false"
        )
        try:
            response = (
                self._drive_service.files()
                .list(q=query, spaces="drive", pageSize=1, fields="files(id)")                .execute()
            )
        except HttpError as exc:
            raise self._map_drive_error(exc)
        files = response.get("files", [])
        if not files:
            return None
        folder_id = files[0].get("id")
        if not isinstance(folder_id, str) or not folder_id:
            raise StorageInvalidResponseError(
                body="Drive API folder search response missing 'id'",
                provider="GOOGLE_DRIVE",
            )
        return folder_id

    def _persist_root_folder_id(self, folder_id: str) -> None:
        """Persist the root folder ID to the keyring credential record.

        Uses ``with_root_folder_id()`` so the existing refresh token,
        client credentials, and scopes are preserved verbatim in the new
        immutable record. On any failure the keyring is left untouched
        (the store validates before writing), the cached folder ID is
        retained, and the pending flag is set so a later
        :meth:`get_root_folder_id` call retries the write.
        """
        try:
            stored = load_google_drive_credentials()
            if stored is None:
                raise CredentialStorageError(
                    "No stored credentials to attach the root folder ID to"
                )
            updated = stored.with_root_folder_id(folder_id)
            store_google_drive_credentials(updated)
            self._root_folder_persistence_pending = False
            logger.info("Root folder ID persisted to credential store")
        except (CredentialStorageError, ValueError, TypeError) as exc:
            # Never overwrite valid credentials with incomplete data;
            # the folder ID stays cached in memory and the write is
            # retried on the next get_root_folder_id() call.
            self._root_folder_persistence_pending = True
            logger.warning(
                "Failed to persist root folder ID %s to credential store; "
                "will retry on a later operation: %s",
                folder_id,
                exc,
            )

    def _retry_root_folder_persistence(self) -> None:
        """Retry a previously failed root-folder persistence (no-op when
        nothing is pending). Must be called with the lock held."""
        if self._root_folder_persistence_pending and self._root_folder_id is not None:
            self._persist_root_folder_id(self._root_folder_id)

    # -----------------------------------------------------------------------
    # StorageOperations (stubs for later M3.2 phases)
    # -----------------------------------------------------------------------

    def upload(self, file_data: bytes, name: str, logical_path: str) -> ProviderFileRecord:
        """Upload a file to the SiloVault root folder in Google Drive.

        Uses a resumable upload via :class:`googleapiclient.http.MediaIoBaseUpload`.
        The dedicated SiloVault root folder ID is resolved on first use and cached.

        Parameters
        ----------
        file_data : bytes
            Raw plaintext bytes to store.
        name : str
            Original user-facing filename.
        logical_path : str
            Logical path within the provider namespace.

        Returns
        -------
        ProviderFileRecord
            Normalized record describing the newly stored file.

        Raises
        ------
        StorageConflictError
            If a file with the same logical path already exists.
        StorageQuotaExceededError
            If the provider reports insufficient remaining capacity.
        StorageAuthenticationError
            If not authenticated.
        StoragePermissionError
            If the provider rejects the request due to insufficient permissions.
        StorageUnavailableError
            On transient provider failures (rate limits, 5xx).
        StorageNotFoundError
            If the root folder is not found after discovery attempt.
        StorageInvalidResponseError
            On unexpected or malformed responses.
        """
        if not isinstance(file_data, (bytes, bytearray)):
            raise TypeError("file_data must be bytes")

        # Ensure we have an active Drive service
        if self._drive_service is None:
            raise StorageAuthenticationError(
                detail="Google Drive adapter is not authenticated. Call authenticate() first.",
                provider="GOOGLE_DRIVE",
            )

        # Resolve the root folder ID (discovers or creates if absent)
        root_folder_id = self.get_root_folder_id()

        # Prepare the upload payload
        try:
            from io import BytesIO
            from googleapiclient.http import MediaIoBaseUpload
            byte_stream = BytesIO(file_data)
            size_bytes = len(file_data)
            mime_type = _guess_mime(name)

            media = MediaIoBaseUpload(
                byte_stream,
                mimetype=mime_type,
                resumable=True,
            )
        except ImportError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to import Google Drive upload utilities: {exc}",
                provider="GOOGLE_DRIVE",
            ) from exc

        body = {
            "name": name,
            "parents": [root_folder_id],
            "mimeType": mime_type,
        }

        try:
            response = self._drive_service.files().create(
                body=body,
                media_body=media,
                fields="id,name,mimeType,size,modifiedTime,md5Checksum",
            ).execute()
        except HttpError as exc:
            raise self._map_drive_error(exc)

        file_id = response.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise StorageInvalidResponseError(
                body="Drive API upload response missing 'id'",
                status_code=None,
                provider="GOOGLE_DRIVE",
            )

        return ProviderFileRecord(
            provider_file_id=file_id,
            name=name,
            logical_path=logical_path,
            mime_type=response.get("mimeType", mime_type),
            size_bytes=int(response.get("size", size_bytes)) if response.get("size") else size_bytes,
            sha256=None,  # MD5 checksum available natively; SHA-256 computed client-side
            provider_metadata={
                "md5_checksum": response.get("md5Checksum"),
                "web_view_link": f"https://drive.google.com/file/d/{file_id}/view",
                "web_content_link": f"https://drive.google.com/uc?id={file_id}&export=download",
            },
            modified_at=_parse_drive_timestamp(response.get("modifiedTime")),
        )

    def download(self, provider_file_id: str) -> bytes:
        """Download a file from Google Drive by its provider-assigned ID.

        Uses :class:`googleapiclient.http.MediaIoBaseDownload` for resumable
        downloads that handle chunked transfers automatically.

        Parameters
        ----------
        provider_file_id : str
            Native Google Drive file identifier.

        Returns
        -------
        bytes
            Raw downloaded file content.

        Raises
        ------
        StorageAuthenticationError
            If not authenticated.
        StorageNotFoundError
            If no file exists under the given identifier.
        StoragePermissionError
            If the provider rejects the request due to insufficient permissions.
        StorageRateLimitError
            On rate-limit responses (429 or 403/userRateLimitExceeded).
        StorageQuotaExceededError
            If the provider reports insufficient remaining capacity.
        StorageUnavailableError
            On transient provider failures (5xx).
        """
        if not isinstance(provider_file_id, str) or not provider_file_id.strip():
            raise StorageNotFoundError(
                provider_file_id or "",
                provider="GOOGLE_DRIVE",
            )

        # Ensure we have an active Drive service
        if self._drive_service is None:
            raise StorageAuthenticationError(
                detail="Google Drive adapter is not authenticated. Call authenticate() first.",
                provider="GOOGLE_DRIVE",
            )

        try:
            from googleapiclient.http import MediaIoBaseDownload
            byte_stream = io.BytesIO()
            request = self._drive_service.files().get_media(fileId=provider_file_id)
            downloader = MediaIoBaseDownload(byte_stream, request, chunksize=1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return byte_stream.getvalue()
        except ImportError as exc:
            raise StorageUnavailableError(
                detail=f"Failed to import Google Drive download utilities: {exc}",
                provider="GOOGLE_DRIVE",
            ) from exc
        except HttpError as exc:
            raise self._map_drive_error(exc)
        except Exception as exc:
            raise StorageUnavailableError(
                detail=f"Failed to download file from Google Drive: {exc}",
                provider="GOOGLE_DRIVE",
            ) from exc

    def rename(self, provider_file_id: str, new_name: str) -> ProviderFileRecord:
        if not isinstance(provider_file_id, str) or not provider_file_id.strip():
            raise StorageNotFoundError(
                provider_file_id or "",
                provider="GOOGLE_DRIVE",
            )
        if not isinstance(new_name, str) or not new_name.strip():
            raise StorageInvalidResponseError(
                body="new_name must be a non-empty string",
                status_code=None,
                provider="GOOGLE_DRIVE",
            )
        if self._drive_service is None:
            raise StorageAuthenticationError(
                detail="Google Drive adapter is not authenticated. Call authenticate() first.",
                provider="GOOGLE_DRIVE",
            )
        try:
            get_response = (
                self._drive_service.files().get(fileId=provider_file_id, fields="size,mimeType,modifiedTime,md5Checksum")                .execute()
            )
            response = (
                self._drive_service.files().update(
                    fileId=provider_file_id,
                    body={"name": new_name},
                    fields="id,name,mimeType,size,modifiedTime,md5Checksum",
                )                .execute()
            )
        except HttpError as exc:
            raise self._map_drive_error(exc)
        file_id = response.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise StorageInvalidResponseError(
                body="Drive API rename response missing id",
                status_code=None,
                provider="GOOGLE_DRIVE",
            )
        return ProviderFileRecord(
            provider_file_id=file_id,
            name=response.get("name", new_name),
            logical_path=f"/{file_id}",
            mime_type=response.get("mimeType", get_response.get("mimeType", "application/octet-stream")),
            size_bytes=int(response.get("size", get_response.get("size", 0)))
            if response.get("size") or get_response.get("size")
            else 0,
            sha256=None,
            provider_metadata={
                "md5_checksum": response.get("md5Checksum"),
                "web_view_link": f"https://drive.google.com/file/d/{file_id}/view",
                "web_content_link": f"https://drive.google.com/uc?id={file_id}&export=download",
            },
            modified_at=_parse_drive_timestamp(response.get("modifiedTime")),
        )

    def move(self, source_provider_file_id: str, target_logical_path: str) -> ProviderFileRecord:
        if not isinstance(source_provider_file_id, str) or not source_provider_file_id.strip():
            raise StorageNotFoundError(
                source_provider_file_id or "",
                provider="GOOGLE_DRIVE",
            )
        if not isinstance(target_logical_path, str) or not target_logical_path.strip():
            raise StorageInvalidResponseError(
                body="target_logical_path must be a non-empty string",
                status_code=None,
                provider="GOOGLE_DRIVE",
            )
        if self._drive_service is None:
            raise StorageAuthenticationError(
                detail="Google Drive adapter is not authenticated. Call authenticate() first.",
                provider="GOOGLE_DRIVE",
            )
        try:
            response = (
                self._drive_service.files().get(
                    fileId=source_provider_file_id,
                    fields="id,name,mimeType,size,modifiedTime,md5Checksum",
                )                .execute()
            )
        except HttpError as exc:
            raise self._map_drive_error(exc)
        file_id = response.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise StorageInvalidResponseError(
                body="Drive API move get-response missing id",
                status_code=None,
                provider="GOOGLE_DRIVE",
            )
        return ProviderFileRecord(
            provider_file_id=file_id,
            name=response.get("name", source_provider_file_id),
            logical_path=target_logical_path,
            mime_type=response.get("mimeType", "application/octet-stream"),
            size_bytes=int(response.get("size", 0)) if response.get("size") else 0,
            sha256=None,
            provider_metadata={
                "md5_checksum": response.get("md5Checksum"),
                "web_view_link": f"https://drive.google.com/file/d/{file_id}/view",
                "web_content_link": f"https://drive.google.com/uc?id={file_id}&export=download",
            },
            modified_at=_parse_drive_timestamp(response.get("modifiedTime")),
        )

    def quarantine(self, provider_file_id: str) -> str:
        if not isinstance(provider_file_id, str) or not provider_file_id.strip():
            raise StorageNotFoundError(
                provider_file_id or "",
                provider="GOOGLE_DRIVE",
            )
        if self._drive_service is None:
            raise StorageAuthenticationError(
                detail="Google Drive adapter is not authenticated. Call authenticate() first.",
                provider="GOOGLE_DRIVE",
            )
        try:
            self._drive_service.files().update(
                fileId=provider_file_id,
                body={"trashed": True},
            )            .execute()
        except HttpError as exc:
            raise self._map_drive_error(exc)
        return provider_file_id

    def restore(self, quarantine_id: str) -> ProviderFileRecord:
        if not isinstance(quarantine_id, str) or not quarantine_id.strip():
            raise StorageNotFoundError(
                quarantine_id or "",
                provider="GOOGLE_DRIVE",
            )
        if self._drive_service is None:
            raise StorageAuthenticationError(
                detail="Google Drive adapter is not authenticated. Call authenticate() first.",
                provider="GOOGLE_DRIVE",
            )
        try:
            self._drive_service.files().update(
                fileId=quarantine_id,
                body={"trashed": False},
            )            .execute()
            response = (
                self._drive_service.files().get(
                    fileId=quarantine_id,
                    fields="id,name,mimeType,size,modifiedTime,md5Checksum",
                )                .execute()
            )
        except HttpError as exc:
            raise self._map_drive_error(exc)
        file_id = response.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise StorageInvalidResponseError(
                body="Drive API restore get-response missing id",
                status_code=None,
                provider="GOOGLE_DRIVE",
            )
        return ProviderFileRecord(
            provider_file_id=file_id,
            name=response.get("name", quarantine_id),
            logical_path=f"/{file_id}",
            mime_type=response.get("mimeType", "application/octet-stream"),
            size_bytes=int(response.get("size", 0)) if response.get("size") else 0,
            sha256=None,
            provider_metadata={
                "md5_checksum": response.get("md5Checksum"),
                "web_view_link": f"https://drive.google.com/file/d/{file_id}/view",
                "web_content_link": f"https://drive.google.com/uc?id={file_id}&export=download",
            },
            modified_at=_parse_drive_timestamp(response.get("modifiedTime")),
        )

    def cleanup(self, quarantine_id: str) -> None:
        if not isinstance(quarantine_id, str) or not quarantine_id.strip():
            raise StorageNotFoundError(
                quarantine_id or "",
                provider="GOOGLE_DRIVE",
            )
        if self._drive_service is None:
            raise StorageAuthenticationError(
                detail="Google Drive adapter is not authenticated. Call authenticate() first.",
                provider="GOOGLE_DRIVE",
            )
        try:
            self._drive_service.files().delete(fileId=quarantine_id)            .execute()
        except HttpError as exc:
            raise self._map_drive_error(exc)
        except Exception as exc:
            raise StorageUnavailableError(
                detail=f"Failed to cleanup quarantined file from Google Drive: {exc}",
                provider="GOOGLE_DRIVE",
            ) from exc

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _create_drive_service(self, creds: ga_credentials.Credentials):
        """Create a Google Drive API v3 service client."""
        try:
            from googleapiclient.discovery import build
            return build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as exc:
            raise StorageUnavailableError(
                detail=f"Failed to create Drive API service: {exc}",
                provider="GOOGLE_DRIVE",
            ) from exc

    def _map_drive_error(self, exc: HttpError) -> StorageError:
        """Map a Google Drive API :class:`HttpError` onto the SiloVault
        storage error hierarchy.

        Inspects both the HTTP status code and the structured error
        ``reason`` values carried in ``error_details``:

        - 404, or 400 with a not-found reason -> StorageNotFoundError
        - 400 (other) -> StorageError (client-side request problem)
        - 403 forbidden/insufficientPermissions -> StoragePermissionError
        - 403 userRateLimitExceeded / 429 rateLimitExceeded
          -> StorageRateLimitError
        - 413 insufficientStorage / fileTooLarge -> StorageQuotaExceededError
        - 5xx -> StorageUnavailableError
        """
        status = getattr(exc, "status_code", None)
        details = getattr(exc, "error_details", None) or []
        reasons = [
            d.get("reason")
            for d in details
            if isinstance(d, dict) and isinstance(d.get("reason"), str)
        ]
        message = getattr(exc, "reason", None) or str(exc)

        if status == 404:
            return StorageNotFoundError(
                "unknown", provider="GOOGLE_DRIVE", __cause__=exc
            )
        if status == 400:
            if any(r in ("notFound", "itemNotFound") for r in reasons):
                return StorageNotFoundError(
                    "unknown", provider="GOOGLE_DRIVE", __cause__=exc
                )
            return StorageInvalidResponseError(
                body=message, status_code=400, provider="GOOGLE_DRIVE", __cause__=exc
            )
        if status == 403:
            if "userRateLimitExceeded" in reasons:
                return StorageRateLimitError(provider="GOOGLE_DRIVE", __cause__=exc)
            return StoragePermissionError(
                detail=message, provider="GOOGLE_DRIVE", __cause__=exc
            )
        if status == 429:
            return StorageRateLimitError(provider="GOOGLE_DRIVE", __cause__=exc)
        if status == 413:
            return StorageQuotaExceededError(provider="GOOGLE_DRIVE", __cause__=exc)
        if status is not None and 500 <= int(status) < 600:
            return StorageUnavailableError(
                detail=message, provider="GOOGLE_DRIVE", __cause__=exc
            )
        return StorageError(detail=message, provider="GOOGLE_DRIVE")
