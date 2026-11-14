"""Google Drive OAuth connection endpoints.

Implements the M3.1 OAuth flow for connecting a Google Drive account:
- POST /api/v1/google-drive/connect — initiate OAuth (returns authorization URL)
- GET  /api/v1/google-drive/status — check connection status
- DELETE /api/v1/google-drive/disconnect — revoke and clear credentials

The OAuth flow uses the installed-app loopback strategy with PKCE.
The local callback server runs on a random port and shuts down after
the authorization code is received.
"""

from __future__ import annotations

import logging
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException

from backend.config import settings
from backend.storage.credentials import (
    CredentialStorageError,
    GoogleDriveCredentials,
    delete_google_drive_credentials,
    has_google_drive_credentials,
    load_google_drive_credentials,
    store_google_drive_credentials,
)
from backend.storage.errors import StorageAuthenticationError
from backend.storage.google_drive import GoogleDriveAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/google-drive", tags=["google-drive"])

# Module-level state for the active connection
_adapter: GoogleDriveAdapter | None = None
_lock = threading.Lock()


def _get_adapter() -> GoogleDriveAdapter | None:
    """Return the current adapter instance or None."""
    return _adapter


def _set_adapter(adapter: GoogleDriveAdapter | None) -> None:
    global _adapter
    _adapter = adapter


def _pick_free_port() -> int:
    """Find a free port on localhost for the loopback server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_client_config() -> dict:
    """Build the client config dict expected by google-auth-oauthlib."""
    return {
        "installed": {
            "client_id": settings.google_drive_client_id,
            "client_secret": settings.google_drive_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth callback on the local loopback server."""

    # Set by the handler factory after the flow starts
    _expected_state: str = ""
    _result: dict | None = None
    _error: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params and "state" in params:
            code = params["code"][0]
            state = params["state"][0]

            if state != self._expected_state:
                self._error = "state_mismatch"
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    "<html><body><h1>OAuth state mismatch</h1></body></html>"
                )
                return

            self._result = {"code": code, "state": state}
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                "<html><body><h1>Google Drive Connected</h1>"
                "<p>You may close this window and return to SiloVault.</p>"
                "</body></html>"
            )
        elif "error" in params:
            err = params["error"][0]
            self._error = err
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                "<html><body><h1>OAuth Error: {}</h1></body></html>".format(err)
            )
        else:
            self._error = "no_params"
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                "<html><body><h1>Invalid callback</h1></body></html>"
            )

    def log_message(self, format, *args):
        # Suppress default request logging so callback query strings
        # (which contain the authorization code) never reach logs.
        pass


def _make_callback_handler(
    expected_state: str, outcome: dict
) -> type:
    """Build a per-flow HTTP handler class.

    The handler validates the OAuth ``state`` parameter (CSRF protection)
    and records the authorization code (or the error) in the shared
    *outcome* dict, which the flow inspects after the server stops.
    """

    class _Handler(_OAuthCallbackHandler):
        # Capture expected_state and outcome as module-level closure vars
        # so they are accessible inside do_GET without relying on class attrs
        # that would be shadowed by instance assignments.
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            if "code" in params and "state" in params:
                code = params["code"][0]
                state = params["state"][0]

                if state != expected_state:
                    outcome["error"] = "state_mismatch"
                    self._error = "state_mismatch"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        "<html><body><h1>OAuth state mismatch</h1></body></html>"
                    )
                    return

                outcome["code"] = code
                outcome["state"] = state
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    "<html><body><h1>Google Drive Connected</h1>"
                    "<p>You may close this window and return to SiloVault.</p>"
                    "</body></html>"
                )
            elif "error" in params:
                self._error = params["error"][0]
                outcome["error"] = self._error
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    "<html><body><h1>OAuth Error: {}</h1></body></html>".format(
                        params["error"][0]
                    )
                )
            else:
                self._error = "no_params"
                outcome["error"] = self._error
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    "<html><body><h1>Invalid callback</h1></body></html>"
                )

        def log_message(self, format, *args):
            pass

    return _Handler


def _run_oauth_flow() -> dict:
    """Run the full OAuth loopback flow synchronously.

    Returns a dict with 'success' (bool) and 'error' (str|None).
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    port = _pick_free_port()
    redirect_uri = f"http://localhost:{port}/"

    client_config = _build_client_config()
    scopes = settings.google_drive_scopes_list

    flow = InstalledAppFlow.from_client_config(
        client_config,
        scopes=scopes,
        redirect_uri=redirect_uri,
    )

    # Generate authorization URL (PKCE code verifier/challenge is
    # auto-generated by the library: 128-char verifier, S256 challenge).
    auth_url, state = flow.authorization_url(prompt="consent")

    # Per-flow outcome shared with the callback handler.
    outcome: dict = {}
    handler = _make_callback_handler(state, outcome)

    server = HTTPServer(("127.0.0.1", port), handler)
    server.timeout = 300  # 5 minute timeout

    # Open browser
    try:
        webbrowser.open(auth_url, new=1)
    except Exception:
        logger.warning("Could not open browser automatically")

    # Wait for callback (single request, then the server is closed).
    server.handle_request()
    server.server_close()

    # Check result — read from shared outcome dict, not class attrs
    error = outcome.get("error") or getattr(handler, "_error", None)
    if error:
        return {"success": False, "error": error}

    if "code" not in outcome:
        return {"success": False, "error": "timeout"}

    # Exchange code for tokens. The authorization_response must be the
    # exact callback URI Google redirected to (the library requires an
    # https scheme string, so we mirror the library's own convention).
    try:
        auth_response = f"https://localhost:{port}/?code={outcome['code']}&state={outcome['state']}"
        flow.fetch_token(authorization_response=auth_response)
    except Exception as exc:
        return {"success": False, "error": f"token_exchange_failed: {exc}"}

    creds = flow.credentials

    if not creds or not creds.refresh_token:
        return {"success": False, "error": "no_refresh_token"}

    # Store credentials securely
    gd_creds = GoogleDriveCredentials(
        refresh_token=creds.refresh_token,
        client_id=settings.google_drive_client_id or "",
        client_secret=settings.google_drive_client_secret or "",
        scopes=scopes,
    )

    try:
        store_google_drive_credentials(gd_creds)
    except CredentialStorageError as exc:
        return {"success": False, "error": f"credential_storage_failed: {exc}"}

    # Initialize the adapter
    adapter = GoogleDriveAdapter()
    try:
        adapter.authenticate(
            {
                "refresh_token": creds.refresh_token,
                "client_id": settings.google_drive_client_id or "",
                "client_secret": settings.google_drive_client_secret or "",
                "scopes": scopes,
            }
        )
    except StorageAuthenticationError as exc:
        # Clean up stored credentials on auth failure
        try:
            delete_google_drive_credentials()
        except CredentialStorageError:
            pass
        return {"success": False, "error": f"authentication_failed: {exc}"}

    with _lock:
        _set_adapter(adapter)

    return {"success": True, "error": None}


@router.post("/connect")
async def connect_google_drive():
    """Initiate Google Drive OAuth connection.

    Starts the installed-app loopback OAuth flow with PKCE.
    Opens the user's browser to Google's consent screen.
    The local callback server listens on a random port.

    Returns:
        200 with {"status": "started", "message": "..."} on success.
        409 if a connection is already active.
        503 if OAuth is not configured.
    """

    # Check if already connected
    if _get_adapter() is not None:
        raise HTTPException(
            status_code=409,
            detail="Google Drive is already connected. Disconnect first.",
        )

    # Check configuration
    if not settings.google_drive_client_id or not settings.google_drive_client_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google Drive OAuth is not configured. "
                "Set GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET in .env."
            ),
        )

    # Run OAuth flow (blocking operation)
    import asyncio

    try:
        result = await asyncio.to_thread(_run_oauth_flow)
    except Exception as exc:
        logger.error("OAuth flow failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"OAuth flow failed: {exc}",
        ) from exc

    if not result["success"]:
        error = result.get("error", "unknown")
        if error == "state_mismatch":
            raise HTTPException(status_code=400, detail="OAuth state validation failed.")
        elif error == "timeout":
            raise HTTPException(status_code=408, detail="OAuth flow timed out.")
        elif error == "no_refresh_token":
            raise HTTPException(
                status_code=400,
                detail="Google did not return a refresh token. "
                "Ensure 'offline' access type is configured.",
            )
        else:
            raise HTTPException(status_code=500, detail=f"OAuth flow failed: {error}")

    return {
        "status": "connected",
        "message": "Google Drive account connected successfully.",
    }


@router.get("/status")
async def get_connection_status():
    """Check Google Drive connection status.

    Returns:
        200 with connection status information.
        Never includes tokens or secrets.
    """
    adapter = _get_adapter()

    if adapter is None:
        # Check if credentials exist in keyring but adapter isn't initialized
        if has_google_drive_credentials():
            return {
                "connected": False,
                "status": "credentials_stored",
                "message": "Credentials exist but adapter is not initialized. "
                "The application may need a restart.",
            }
        return {
            "connected": False,
            "status": "disconnected",
            "message": "No Google Drive account is connected.",
        }

    # Adapter is active — verify token validity
    try:
        adapter.refresh()
        return {
            "connected": True,
            "status": "active",
            "message": "Google Drive account is connected and active.",
            "scopes": settings.google_drive_scopes_list,
        }
    except StorageAuthenticationError:
        return {
            "connected": False,
            "status": "token_invalid",
            "message": "Stored credentials are no longer valid. Please reconnect.",
        }
    except Exception as exc:
        return {
            "connected": True,
            "status": "degraded",
            "message": f"Connection exists but token refresh failed: {exc}",
        }


@router.delete("/disconnect", status_code=204)
async def disconnect_google_drive():
    """Disconnect Google Drive and clear stored credentials.

    Returns:
        204 No Content on success.
        404 if no account is connected.
    """
    adapter = _get_adapter()

    if adapter is None and not has_google_drive_credentials():
        raise HTTPException(
            status_code=404,
            detail="No Google Drive account is connected.",
        )

    # Clear in-memory adapter
    if adapter is not None:
        adapter.disconnect()

    with _lock:
        _set_adapter(None)

    # Delete stored credentials
    try:
        delete_google_drive_credentials()
    except CredentialStorageError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to clear stored credentials: {exc}",
        ) from exc

    return None  # 204 No Content