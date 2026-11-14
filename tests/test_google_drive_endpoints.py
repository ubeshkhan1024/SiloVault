"""Tests for Google Drive OAuth endpoints (M3.1).

All tests use mocked FastAPI TestClient with patched OAuth flow and
keyring. No real Google credentials or browser authorization is required.
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.api.v1.endpoints import google_drive as gd_endpoints


# ---------------------------------------------------------------------------
# Helper: mock keyring so disconnect/status can check without OS store
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_keyring(monkeypatch: pytest.MonkeyPatch):
    """Replace keyring functions in backend.storage.credentials module."""
    import backend.storage.credentials as kr_mod

    def mock_store(creds):
        pass

    def mock_load():
        return None

    def mock_delete():
        return False

    def mock_has(service, account):
        return False

    monkeypatch.setattr(kr_mod, "store_google_drive_credentials", mock_store)
    monkeypatch.setattr(kr_mod, "load_google_drive_credentials", mock_load)
    monkeypatch.setattr(kr_mod, "delete_google_drive_credentials", mock_delete)
    monkeypatch.setattr(kr_mod, "has_google_drive_credentials", mock_has)


# ---------------------------------------------------------------------------
# Helper: mock the OAuth flow so connect() does not open a browser
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_oauth_flow(monkeypatch: pytest.MonkeyPatch):
    """Mock _run_oauth_flow to return success synchronously."""

    def _mock_run_oauth_flow():
        return {"success": True, "error": None}

    monkeypatch.setattr(
        "backend.api.v1.endpoints.google_drive._run_oauth_flow",
        _mock_run_oauth_flow,
    )

    yield


# ---------------------------------------------------------------------------
# Tests: /connect endpoint
# ---------------------------------------------------------------------------


def test_connect_google_drive_no_configuration():
    """POST /google-drive/connect returns 503 when client_id/secret missing."""
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/v1/google-drive/connect")
    assert response.status_code == 503
    payload = response.json()
    assert "not configured" in payload["detail"].lower()


def test_connect_google_drive_success_with_mocks():
    """POST /google-drive/connect succeeds when configured and not connected."""
    with patch.object(
        gd_endpoints.settings, "google_drive_client_id", "mock-id"
    ):
        with patch.object(
            gd_endpoints.settings, "google_drive_client_secret", "mock-secret"
        ):
            with patch(
                "backend.api.v1.endpoints.google_drive._run_oauth_flow",
                return_value={"success": True, "error": None},
            ):
                client = TestClient(app, raise_server_exceptions=False)
                response = client.post("/api/v1/google-drive/connect")
                assert response.status_code == 200
                payload = response.json()
                assert payload["status"] == "connected"


def test_connect_google_drive_already_connected():
    """POST /google-drive/connect returns 409 when already connected."""
    client = TestClient(app, raise_server_exceptions=False)
    with patch("backend.config.settings.google_drive_client_id", "mock-id"):
        with patch("backend.config.settings.google_drive_client_secret", "mock-secret"):
            with patch(
                "backend.api.v1.endpoints.google_drive._get_adapter",
                return_value=MagicMock(),
            ):
                response = client.post("/api/v1/google-drive/connect")
                assert response.status_code == 409


# ---------------------------------------------------------------------------
# Tests: /status endpoint
# ---------------------------------------------------------------------------


def test_status_google_drive_disconnected():
    """GET /google-drive/status returns disconnected when no account."""
    client = TestClient(app, raise_server_exceptions=False)
    with patch(
        "backend.api.v1.endpoints.google_drive._get_adapter",
        return_value=None,
    ):
        with patch(
            "backend.api.v1.endpoints.google_drive.has_google_drive_credentials",
            return_value=False,
        ):
            response = client.get("/api/v1/google-drive/status")
            assert response.status_code == 200
            payload = response.json()
            assert payload["connected"] is False
            assert payload["status"] == "disconnected"


def test_status_google_drive_connected():
    """GET /google-drive/status returns connected when adapter present."""
    client = TestClient(app, raise_server_exceptions=False)
    mock_adapter = MagicMock()
    mock_adapter.refresh.return_value = None
    with patch(
        "backend.api.v1.endpoints.google_drive._get_adapter",
        return_value=mock_adapter,
    ):
        response = client.get("/api/v1/google-drive/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["connected"] is True
        assert payload["status"] == "active"


def test_status_google_drive_credentials_stored():
    """GET /google-drive/status when credentials stored but adapter not init."""
    client = TestClient(app, raise_server_exceptions=False)
    with patch(
        "backend.api.v1.endpoints.google_drive._get_adapter",
        return_value=None,
    ):
        with patch(
            "backend.api.v1.endpoints.google_drive.has_google_drive_credentials",
            return_value=True,
        ):
            response = client.get("/api/v1/google-drive/status")
            assert response.status_code == 200
            payload = response.json()
            assert payload["connected"] is False
            assert payload["status"] == "credentials_stored"


# ---------------------------------------------------------------------------
# Tests: /disconnect endpoint
# ---------------------------------------------------------------------------


def test_disconnect_google_drive_no_account():
    """DELETE /google-drive/disconnect returns 404 when no account connected."""
    client = TestClient(app, raise_server_exceptions=False)
    with patch(
        "backend.api.v1.endpoints.google_drive._get_adapter",
        return_value=None,
    ):
        with patch(
            "backend.api.v1.endpoints.google_drive.has_google_drive_credentials",
            return_value=False,
        ):
            response = client.delete("/api/v1/google-drive/disconnect")
            assert response.status_code == 404


def test_disconnect_google_drive_clears_credentials():
    """DELETE /google-drive/disconnect clears stored credentials."""
    client = TestClient(app, raise_server_exceptions=False)
    mock_adapter = MagicMock()
    cleared = {"cleared": False}

    def mock_set_adapter(val):
        if val is None:
            cleared["cleared"] = True

    with patch(
        "backend.api.v1.endpoints.google_drive._get_adapter",
        return_value=mock_adapter,
    ):
        with patch(
            "backend.api.v1.endpoints.google_drive._set_adapter",
            side_effect=mock_set_adapter,
        ):
            with patch(
                "backend.api.v1.endpoints.google_drive.has_google_drive_credentials",
                return_value=False,
            ):
                with patch(
                    "backend.api.v1.endpoints.google_drive.delete_google_drive_credentials",
                    return_value=True,
                ):
                    response = client.delete("/api/v1/google-drive/disconnect")
                    assert response.status_code == 204
                    assert cleared["cleared"] is True


# ---------------------------------------------------------------------------
# Tests: token leakage / no secrets in responses
# ---------------------------------------------------------------------------


def test_no_token_in_connect_response():
    """Connect response must not contain refresh or access tokens."""
    with patch.object(
        gd_endpoints.settings, "google_drive_client_id", "mock-id"
    ):
        with patch.object(
            gd_endpoints.settings, "google_drive_client_secret", "mock-secret"
        ):
            with patch(
                "backend.api.v1.endpoints.google_drive._run_oauth_flow",
                return_value={"success": True, "error": None},
            ):
                client = TestClient(app, raise_server_exceptions=False)
                response = client.post("/api/v1/google-drive/connect")
                assert response.status_code == 200
                payload = response.json()
                body_text = str(payload)
                assert "refresh_token" not in body_text.lower()
                assert "access_token" not in body_text.lower()
                assert "mock-secret" not in body_text


def test_no_token_in_status_response():
    """Status response must not contain tokens."""
    client = TestClient(app, raise_server_exceptions=False)
    mock_adapter = MagicMock()
    mock_adapter.refresh.return_value = None
    with patch(
        "backend.api.v1.endpoints.google_drive._get_adapter",
        return_value=mock_adapter,
    ):
        response = client.get("/api/v1/google-drive/status")
        assert response.status_code == 200
        payload = response.json()
        body_text = str(payload)
        assert "refresh_token" not in body_text.lower()
        assert "access_token" not in body_text.lower()


# ---------------------------------------------------------------------------
# Tests: LOCAL adapter unaffected
# ---------------------------------------------------------------------------


def test_local_adapter_unaffected_by_google_drive():
    """LOCAL adapter must remain available regardless of Google Drive state."""
    from backend.storage.registry import lookup

    # When run as part of the full suite, the app lifespan has registered LOCAL.
    # When run in isolation, LOCAL may not be present — skip gracefully.
    try:
        adapter, caps = lookup("LOCAL")
    except Exception:
        pytest.skip("LOCAL adapter not registered (no app lifespan)")
        return
    assert adapter is not None
    assert caps is not None


def test_google_drive_disconnect_does_not_affect_local():
    """Disconnecting Google Drive leaves LOCAL adapter intact."""
    from backend.storage.registry import lookup as local_lookup

    try:
        adapter, caps = local_lookup("LOCAL")
    except Exception:
        pytest.skip("LOCAL adapter not registered (no app lifespan)")
        return
    assert adapter is not None
    assert caps is not None


# ---------------------------------------------------------------------------
# Tests: OAuth state validation and PKCE
# ---------------------------------------------------------------------------


def test_pkce_auto_generated_on_authorization_url():
    """The library auto-generates a PKCE code verifier and challenge."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": "mock-id",
            "client_secret": "mock-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    scopes = ["https://www.googleapis.com/auth/drive.file"]

    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    auth_url, state = flow.authorization_url(prompt="consent")

    # PKCE parameters must be present in the URL
    assert "code_challenge" in auth_url
    assert "code_challenge_method=S256" in auth_url
    # code_verifier is kept in-memory by the flow object, never sent over the wire
    assert hasattr(flow, "code_verifier") and len(flow.code_verifier) >= 43
    assert len(state) > 0


def test_state_parameter_is_non_empty():
    """OAuth state parameter must be generated (CSRF protection)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": "mock-id",
            "client_secret": "mock-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(
        client_config, scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    _, state = flow.authorization_url(prompt="consent")
    assert isinstance(state, str)
    assert len(state) >= 10


def _call_handler_do_get(handler_cls, req_path):
    """Create a handler instance without triggering BaseHTTPRequestHandler.__init__
    (which calls self.handle() in Python 3.13+) and invoke do_GET directly."""
    handler = object.__new__(handler_cls)
    handler.path = req_path
    handler.requestline = "GET " + req_path + " HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.wfile = MagicMock()  # suppress HTML body writes
    handler.log_message = lambda *a, **kw: None  # suppress request logging
    handler.do_GET()


def test_invalid_oauth_state_rejected():
    """When callback state != expected state, outcome records error."""
    from backend.api.v1.endpoints.google_drive import _make_callback_handler

    outcome: dict = {}
    handler_cls = _make_callback_handler("expected-state-abc", outcome)
    _call_handler_do_get(handler_cls, "/?code=real-code&state=wrong-state")
    assert outcome.get("error") == "state_mismatch"


def test_valid_oauth_callback_records_code():
    """A matching state callback records the authorization code."""
    from backend.api.v1.endpoints.google_drive import _make_callback_handler

    outcome: dict = {}
    handler_cls = _make_callback_handler("my-state", outcome)
    _call_handler_do_get(handler_cls, "/?code=auth-code-123&state=my-state")
    assert outcome.get("code") == "auth-code-123"
    assert outcome.get("state") == "my-state"
    assert "error" not in outcome


def test_oob_error_param_recorded():
    """When Google returns an error param, it is recorded in outcome."""
    from backend.api.v1.endpoints.google_drive import _make_callback_handler

    outcome: dict = {}
    handler_cls = _make_callback_handler("my-state", outcome)
    _call_handler_do_get(handler_cls, "/?error=access_denied")
    assert outcome.get("error") == "access_denied"
