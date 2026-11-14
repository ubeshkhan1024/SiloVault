"""Secure credential storage for SiloVault cloud providers.

Uses the operating system's secure credential store via the `keyring` library:
- Windows: Credential Manager
- macOS: Keychain
- Linux: Secret Service (gnome-keyring, kwallet, etc.)

Only refresh tokens are persisted. Access tokens are kept in memory only
and never written to disk or logs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import keyring
from keyring.errors import KeyringError, KeyringLocked, NoKeyringError

from backend.config import settings

logger = logging.getLogger(__name__)

# Keyring service name and account identifier
_KEYRING_SERVICE = "silovault"
_KEYRING_ACCOUNT = "google-drive"


class CredentialStorageError(Exception):
    """Raised when credential storage operations fail."""

    def __init__(self, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True)
class GoogleDriveCredentials:
    """Refresh-token credentials for Google Drive.

    Attributes:
        refresh_token: OAuth 2.0 refresh token (persisted to keyring).
        client_id: OAuth client ID (from config, not persisted).
        client_secret: OAuth client secret (from config, not persisted).
        scopes: Tuple of OAuth scopes granted (immutable).
        root_folder_id: Optional Google Drive folder ID for the SiloVault root folder.
            Empty string when not yet discovered. Persisted to keyring alongside
            the refresh token for reuse across sessions.
    """

    refresh_token: str
    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
    root_folder_id: str = ""

    def __post_init__(self) -> None:
        """Validate and normalize fields after initialization."""
        # Validate and normalize scopes to tuple for immutability
        if isinstance(self.scopes, tuple):
            scopes_tuple = self.scopes
        elif isinstance(self.scopes, list):
            scopes_tuple = tuple(self.scopes)
        else:
            raise TypeError(
                f"scopes must be a list or tuple of strings, got {type(self.scopes).__name__}"
            )

        # Validate each scope is a non-empty string
        for i, scope in enumerate(scopes_tuple):
            if not isinstance(scope, str):
                raise TypeError(f"scope at index {i} must be a string, got {type(scope).__name__}")
            if not scope or not scope.strip():
                raise ValueError(f"scope at index {i} cannot be empty or whitespace-only")

        # Normalize to stripped tuple
        normalized = tuple(s.strip() for s in scopes_tuple)
        if not normalized:
            raise ValueError("scopes must not be empty")

        object.__setattr__(self, "scopes", normalized)

        # Validate refresh_token
        if not isinstance(self.refresh_token, str):
            raise TypeError(f"refresh_token must be a string, got {type(self.refresh_token).__name__}")
        if not self.refresh_token or not self.refresh_token.strip():
            raise ValueError("refresh_token must be a non-empty string")

        # Normalize refresh_token
        object.__setattr__(self, "refresh_token", self.refresh_token.strip())

        # Validate root_folder_id if provided
        if self.root_folder_id and not isinstance(self.root_folder_id, str):
            raise TypeError(f"root_folder_id must be a string, got {type(self.root_folder_id).__name__}")
        if self.root_folder_id:
            stripped = self.root_folder_id.strip()
            if not stripped:
                raise ValueError("root_folder_id cannot be whitespace-only")
            object.__setattr__(self, "root_folder_id", stripped)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for keyring storage.

        Only the refresh token, scopes, and root_folder_id are stored;
        client credentials come from configuration at runtime.
        """
        data: dict[str, Any] = {
            "refresh_token": self.refresh_token,
            "scopes": list(self.scopes),  # JSON-serializable list
        }
        if self.root_folder_id:
            data["root_folder_id"] = self.root_folder_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoogleDriveCredentials:
        """Reconstruct from stored data and current config.

        Backward compatible: credentials stored without root_folder_id
        (M3.1 format) will have an empty root_folder_id.
        Missing scopes key uses configured defaults; null scopes value is rejected.

        Raises:
            TypeError: If scopes is not a list or tuple.
            ValueError: If any scope is empty or whitespace-only.
            KeyError: If required fields are missing.
        """
        # Handle scopes: missing key -> use defaults; present but null -> error
        if "scopes" not in data:
            scopes = settings.google_drive_scopes_list
        else:
            scopes = data["scopes"]
            if scopes is None:
                raise ValueError("scopes cannot be null")
            if isinstance(scopes, list):
                scopes = tuple(scopes)
            elif not isinstance(scopes, tuple):
                raise TypeError(f"scopes must be a list or tuple, got {type(scopes).__name__}")

        return cls(
            refresh_token=data["refresh_token"],
            client_id=settings.google_drive_client_id or "",
            client_secret=settings.google_drive_client_secret or "",
            scopes=scopes,
            root_folder_id=data.get("root_folder_id", ""),
        )

    def with_root_folder_id(self, folder_id: str) -> GoogleDriveCredentials:
        """Return a new credentials instance with the updated root_folder_id.

        Since GoogleDriveCredentials is frozen, this creates a new instance
        preserving all other fields. Use this when the root folder is
        discovered or created at runtime.

        Args:
            folder_id: Google Drive folder ID. Must be non-empty.

        Raises:
            ValueError: If folder_id is empty or whitespace-only.
        """
        if not isinstance(folder_id, str):
            raise TypeError(f"folder_id must be a string, got {type(folder_id).__name__}")
        if not folder_id or not folder_id.strip():
            raise ValueError("root_folder_id must be a non-empty string")
        return GoogleDriveCredentials(
            refresh_token=self.refresh_token,
            client_id=self.client_id,
            client_secret=self.client_secret,
            scopes=self.scopes,
            root_folder_id=folder_id.strip(),
        )


def _get_keyring() -> keyring.Keyring:
    """Get the keyring backend, raising a clear error if unavailable."""
    try:
        kr = keyring.get_keyring()
        if kr is None:
            raise NoKeyringError("No keyring backend available")
        return kr
    except (NoKeyringError, KeyringError) as exc:
        raise CredentialStorageError(
            "System credential store is not available. "
            "On Linux, ensure a secret service (gnome-keyring, kwallet) is running. "
            "On Windows/macOS, the built-in store should work automatically.",
            cause=exc,
        ) from exc


def store_google_drive_credentials(credentials: GoogleDriveCredentials) -> None:
    """Persist Google Drive refresh token to the OS credential store.

    Args:
        credentials: GoogleDriveCredentials containing the refresh token.

    Raises:
        CredentialStorageError: If the keyring is locked, unavailable, or write fails.
        ValueError: If credentials are invalid (empty refresh token or scopes).
        TypeError: If credentials is not a GoogleDriveCredentials instance.
    """
    # Validate type before any keyring operation
    if not isinstance(credentials, GoogleDriveCredentials):
        raise TypeError(
            f"credentials must be a GoogleDriveCredentials instance, got {type(credentials).__name__}"
        )

    # The GoogleDriveCredentials constructor already validates fields,
    # but we add an explicit check for defense in depth
    if not credentials.refresh_token or not credentials.refresh_token.strip():
        raise ValueError("refresh_token must be a non-empty string")
    if not credentials.scopes:
        raise ValueError("scopes must not be empty")

    kr = _get_keyring()
    data = credentials.to_dict()
    try:
        kr.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, json.dumps(data))
        logger.info("Google Drive credentials stored in system keyring")
    except KeyringLocked as exc:
        raise CredentialStorageError(
            "System credential store is locked. Unlock your keyring/keychain and retry.",
            cause=exc,
        ) from exc
    except KeyringError as exc:
        raise CredentialStorageError(
            f"Failed to store credentials in system keyring: {exc}",
            cause=exc,
        ) from exc


def load_google_drive_credentials() -> Optional[GoogleDriveCredentials]:
    """Load Google Drive refresh token from the OS credential store.

    Returns:
        GoogleDriveCredentials if stored credentials exist and are valid, None otherwise.

    Raises:
        CredentialStorageError: If the keyring is locked or unavailable.
    """
    kr = _get_keyring()
    try:
        stored = kr.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    except KeyringLocked as exc:
        raise CredentialStorageError(
            "System credential store is locked. Unlock your keyring/keychain and retry.",
            cause=exc,
        ) from exc
    except KeyringError as exc:
        raise CredentialStorageError(
            f"Failed to load credentials from system keyring: {exc}",
            cause=exc,
        ) from exc

    if not stored:
        return None

    try:
        data = json.loads(stored)
    except json.JSONDecodeError:
        logger.warning("Corrupted credentials in keyring (invalid JSON); treating as absent")
        return None

    # Validate required fields and types before constructing credentials
    if not isinstance(data, dict):
        logger.warning("Corrupted credentials in keyring (not a dict); treating as absent")
        return None

    refresh_token = data.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        logger.warning("Corrupted credentials in keyring (invalid refresh_token); treating as absent")
        return None

    scopes = data.get("scopes")
    if scopes is not None and not isinstance(scopes, (list, tuple)):
        logger.warning("Corrupted credentials in keyring (invalid scopes type); treating as absent")
        return None
    if isinstance(scopes, (list, tuple)):
        for scope in scopes:
            if not isinstance(scope, str) or not scope.strip():
                logger.warning("Corrupted credentials in keyring (invalid scope entry); treating as absent")
                return None

    root_folder_id = data.get("root_folder_id", "")
    if root_folder_id is not None and not isinstance(root_folder_id, str):
        logger.warning("Corrupted credentials in keyring (invalid root_folder_id); treating as absent")
        return None

    try:
        return GoogleDriveCredentials.from_dict(data)
    except (TypeError, ValueError, KeyError) as exc:
        logger.warning("Corrupted credentials in keyring (validation failed); treating as absent")
        return None


def delete_google_drive_credentials() -> bool:
    """Delete Google Drive credentials from the OS credential store.

    Returns:
        True if credentials were deleted, False if none were stored.

    Raises:
        CredentialStorageError: If the keyring is locked or delete fails.
    """
    kr = _get_keyring()
    try:
        # Check if credentials exist first
        stored = kr.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        if not stored:
            return False

        kr.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        logger.info("Google Drive credentials deleted from system keyring")
        return True
    except KeyringLocked as exc:
        raise CredentialStorageError(
            "System credential store is locked. Unlock your keyring/keychain and retry.",
            cause=exc,
        ) from exc
    except KeyringError as exc:
        raise CredentialStorageError(
            f"Failed to delete credentials from system keyring: {exc}",
            cause=exc,
        ) from exc


def has_google_drive_credentials() -> bool:
    """Check if Google Drive credentials are stored without loading them.

    Returns:
        True if credentials exist in the keyring, False otherwise.
    """
    try:
        kr = _get_keyring()
        stored = kr.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        return bool(stored)
    except (KeyringError, KeyringLocked):
        return False