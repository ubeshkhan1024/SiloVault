"""Focused tests for Google Drive credential storage (M3.2 Phase A).

Covers ``backend.storage.credentials``:

* ``GoogleDriveCredentials`` construction validation:
  scopes (list/tuple accepted, string/empty/whitespace/non-string rejected),
  refresh-token validation, frozen-dataclass immutability,
  and ``root_folder_id`` handling.
* ``with_root_folder_id()``: new-instance semantics, field preservation,
  and rejection of empty / whitespace-only / non-string folder IDs.
* Serialization: ``to_dict()`` / ``from_dict()`` roundtrip, root folder
  persistence, backward compatibility with M3.1 records (missing
  ``scopes`` key and missing ``root_folder_id``), and rejection of
  explicit ``scopes: null``.
* Keyring safety: invalid store attempts never overwrite existing valid
  entries, corrupted stored data returns ``None`` without exposing secrets
  in logs, and keyring availability errors still surface correctly.

All tests use the same in-memory keyring fixture convention as
``tests/test_google_drive_auth.py`` (monkeypatch on
``backend.storage.credentials._get_keyring``). No OS credential store and
no real Google credentials are used.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.config import settings
from backend.storage.credentials import (
    CredentialStorageError,
    GoogleDriveCredentials,
    load_google_drive_credentials,
    store_google_drive_credentials,
)

try:
    from keyring.errors import KeyringError, KeyringLocked
except ImportError:  # pragma: no cover - keyring is a hard dependency
    raise

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_keyring(monkeypatch: pytest.MonkeyPatch):
    """Replace ``_get_keyring`` with one that returns an in-memory dict."""

    class InMemoryKeyring:
        def __init__(self):
            self.store: dict[tuple[str, str], str] = {}

        def set_password(self, service: str, account: str, data: str) -> None:
            self.store[(service, account)] = data

        def get_password(self, service: str, account: str) -> str | None:
            return self.store.get((service, account))

        def delete_password(self, service: str, account: str) -> None:
            self.store.pop((service, account), None)

    fake_kr = InMemoryKeyring()

    def fake_get_keyring():
        return fake_kr

    monkeypatch.setattr("backend.storage.credentials._get_keyring", fake_get_keyring)
    monkeypatch.setattr("backend.storage.credentials.keyring", SimpleNamespace())

    yield fake_kr


def _make_credentials(**overrides) -> GoogleDriveCredentials:
    """Build valid credentials with sensible defaults, applying overrides."""
    params = dict(
        refresh_token="valid-refresh-token",
        client_id="cid",
        client_secret="cs",
        scopes=[DRIVE_FILE_SCOPE],
    )
    params.update(overrides)
    return GoogleDriveCredentials(**params)


# ---------------------------------------------------------------------------
# Tests: scopes validation and immutability
# ---------------------------------------------------------------------------


def test_scopes_list_normalized_to_tuple():
    """A list of scopes is accepted and stored as an immutable tuple."""
    creds = _make_credentials(scopes=[DRIVE_FILE_SCOPE, "extra.scope"])
    assert isinstance(creds.scopes, tuple)
    assert creds.scopes == (DRIVE_FILE_SCOPE, "extra.scope")


def test_scopes_tuple_accepted():
    """A tuple of scopes is accepted unchanged."""
    creds = _make_credentials(scopes=(DRIVE_FILE_SCOPE,))
    assert isinstance(creds.scopes, tuple)
    assert creds.scopes == (DRIVE_FILE_SCOPE,)


def test_scopes_string_rejected():
    """A bare string must not be accepted (would split character-wise)."""
    with pytest.raises(TypeError):
        _make_credentials(scopes="https://www.googleapis.com/auth/drive.file")


def test_scopes_empty_list_rejected():
    """An empty scopes container is rejected."""
    with pytest.raises(ValueError):
        _make_credentials(scopes=[])


def test_scopes_whitespace_only_rejected():
    """Whitespace-only scope entries are rejected."""
    with pytest.raises(ValueError):
        _make_credentials(scopes=["   "])


def test_scopes_non_string_entries_rejected():
    """Non-string scope entries are rejected."""
    with pytest.raises(TypeError):
        _make_credentials(scopes=[DRIVE_FILE_SCOPE, 42])


def test_scopes_tuple_content_cannot_be_mutated():
    """The stored scopes container itself is immutable."""
    creds = _make_credentials(scopes=[DRIVE_FILE_SCOPE])
    with pytest.raises(AttributeError):
        creds.scopes.append("injected.scope")  # type: ignore[union-attr]


def test_scopes_whitespace_stripped_on_normalization():
    """Leading/trailing whitespace on scope entries is stripped."""
    creds = _make_credentials(scopes=[f"  {DRIVE_FILE_SCOPE}  "])
    assert creds.scopes == (DRIVE_FILE_SCOPE,)


# ---------------------------------------------------------------------------
# Tests: refresh-token validation
# ---------------------------------------------------------------------------


def test_refresh_token_empty_rejected():
    with pytest.raises(ValueError):
        _make_credentials(refresh_token="")


def test_refresh_token_whitespace_only_rejected():
    with pytest.raises(ValueError):
        _make_credentials(refresh_token="   ")


def test_refresh_token_non_string_rejected():
    with pytest.raises(TypeError):
        _make_credentials(refresh_token=12345)  # type: ignore[arg-type]


def test_refresh_token_whitespace_stripped():
    creds = _make_credentials(refresh_token="  padded-token  ")
    assert creds.refresh_token == "padded-token"


# ---------------------------------------------------------------------------
# Tests: frozen dataclass immutability
# ---------------------------------------------------------------------------


def test_frozen_fields_cannot_be_reassigned():
    creds = _make_credentials()
    with pytest.raises(AttributeError):
        creds.refresh_token = "hijacked"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        creds.scopes = ("other",)  # type: ignore[misc]
    with pytest.raises(AttributeError):
        creds.root_folder_id = "x"  # type: ignore[misc]


def test_root_folder_id_default_is_empty():
    creds = _make_credentials()
    assert creds.root_folder_id == ""


def test_root_folder_id_whitespace_only_rejected_in_constructor():
    """A whitespace-only root_folder_id is rejected; empty is the valid default."""
    with pytest.raises(ValueError):
        _make_credentials(root_folder_id="   ")


def test_root_folder_id_non_string_rejected_in_constructor():
    with pytest.raises(TypeError):
        _make_credentials(root_folder_id=12345)  # type: ignore[arg-type]


def test_root_folder_id_stripped():
    creds = _make_credentials(root_folder_id="  folder-abc  ")
    assert creds.root_folder_id == "folder-abc"


# ---------------------------------------------------------------------------
# Tests: with_root_folder_id()
# ---------------------------------------------------------------------------


def test_with_root_folder_id_returns_new_instance():
    original = _make_credentials()
    updated = original.with_root_folder_id("folder-abc")

    assert updated is not original
    # Original is unchanged (frozen + new-instance semantics)
    assert original.root_folder_id == ""
    assert updated.root_folder_id == "folder-abc"


def test_with_root_folder_id_preserves_all_fields():
    original = _make_credentials(
        refresh_token="keep-me",
        client_id="keep-cid",
        client_secret="keep-cs",
        scopes=[DRIVE_FILE_SCOPE, "extra.scope"],
    )
    updated = original.with_root_folder_id("folder-xyz")

    assert updated.refresh_token == original.refresh_token
    assert updated.client_id == original.client_id
    assert updated.client_secret == original.client_secret
    assert updated.scopes == original.scopes
    assert updated.root_folder_id == "folder-xyz"


def test_with_root_folder_id_empty_rejected():
    creds = _make_credentials()
    with pytest.raises(ValueError):
        creds.with_root_folder_id("")


def test_with_root_folder_id_whitespace_only_rejected():
    creds = _make_credentials()
    with pytest.raises(ValueError):
        creds.with_root_folder_id("   ")


def test_with_root_folder_id_non_string_rejected():
    creds = _make_credentials()
    with pytest.raises(TypeError):
        creds.with_root_folder_id(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests: serialization / deserialization
# ---------------------------------------------------------------------------


def test_to_dict_from_dict_roundtrip():
    creds = _make_credentials(root_folder_id="folder-rt")
    data = creds.to_dict()
    restored = GoogleDriveCredentials.from_dict(data)

    assert restored.refresh_token == creds.refresh_token
    assert restored.scopes == creds.scopes
    assert restored.root_folder_id == "folder-rt"


def test_to_dict_omits_root_folder_id_when_empty():
    creds = _make_credentials()
    data = creds.to_dict()
    assert "root_folder_id" not in data


def test_to_dict_includes_root_folder_id_when_set():
    creds = _make_credentials(root_folder_id="folder-abc")
    data = creds.to_dict()
    assert data["root_folder_id"] == "folder-abc"


def test_to_dict_scopes_are_json_serializable_list():
    """Scopes serialize as a JSON list even though they are stored as a tuple."""
    creds = _make_credentials(scopes=[DRIVE_FILE_SCOPE, "extra.scope"])
    data = creds.to_dict()
    assert isinstance(data["scopes"], list)
    json.dumps(data)  # must not raise


def test_from_dict_missing_scopes_uses_config_defaults():
    """M3.1 records without a scopes key fall back to configured defaults."""
    data = {"refresh_token": "legacy-token"}
    creds = GoogleDriveCredentials.from_dict(data)
    assert creds.scopes == tuple(settings.google_drive_scopes_list)
    assert creds.root_folder_id == ""


def test_from_dict_null_scopes_rejected():
    """An explicit null scopes value is corrupted data, not a default."""
    data = {"refresh_token": "token", "scopes": None}
    with pytest.raises(ValueError):
        GoogleDriveCredentials.from_dict(data)


def test_from_dict_string_scopes_rejected():
    data = {"refresh_token": "token", "scopes": "drive.file"}
    with pytest.raises(TypeError):
        GoogleDriveCredentials.from_dict(data)


def test_from_dict_missing_refresh_token_rejected():
    data = {"scopes": [DRIVE_FILE_SCOPE]}
    with pytest.raises(KeyError):
        GoogleDriveCredentials.from_dict(data)


# ---------------------------------------------------------------------------
# Tests: keyring store / load roundtrip (Phase A additions)
# ---------------------------------------------------------------------------


def test_store_load_roundtrip_with_root_folder_id(_fake_keyring):
    creds = _make_credentials(root_folder_id="folder-persisted")
    store_google_drive_credentials(creds)

    loaded = load_google_drive_credentials()
    assert loaded is not None
    assert loaded.refresh_token == "valid-refresh-token"
    assert loaded.root_folder_id == "folder-persisted"


def test_load_m31_record_without_root_folder_id(_fake_keyring):
    """M3.1 records (no root_folder_id key) load with empty root_folder_id."""
    store_google_drive_credentials(_make_credentials())

    # Simulate an M3.1 record: strip the root_folder_id key (already absent
    # when empty, but assert the stored JSON shape explicitly).
    raw = json.loads(_fake_keyring.get_password("silovault", "google-drive"))
    assert "root_folder_id" not in raw

    loaded = load_google_drive_credentials()
    assert loaded is not None
    assert loaded.root_folder_id == ""


def test_with_root_folder_id_persisted_via_keyring(_fake_keyring):
    """Updating the root folder ID via keyring preserves refresh token/scopes."""
    original = _make_credentials(refresh_token="precious-token")
    store_google_drive_credentials(original)

    loaded = load_google_drive_credentials()
    assert loaded is not None
    updated = loaded.with_root_folder_id("new-folder-id")
    store_google_drive_credentials(updated)

    reloaded = load_google_drive_credentials()
    assert reloaded is not None
    assert reloaded.refresh_token == "precious-token"
    assert reloaded.scopes == original.scopes
    assert reloaded.root_folder_id == "new-folder-id"


# ---------------------------------------------------------------------------
# Tests: invalid store attempts never overwrite valid entries
# ---------------------------------------------------------------------------


def test_store_rejects_non_credentials_instance(_fake_keyring):
    """A non-GoogleDriveCredentials object is rejected before keyring access."""
    store_google_drive_credentials(_make_credentials(refresh_token="original"))

    with pytest.raises(TypeError):
        store_google_drive_credentials({"refresh_token": "evil"})  # type: ignore[arg-type]

    # Original entry is untouched
    loaded = load_google_drive_credentials()
    assert loaded is not None
    assert loaded.refresh_token == "original"


def test_store_invalid_credentials_does_not_overwrite(_fake_keyring):
    """Defense in depth: a corrupted instance reaching store() is rejected
    and the existing valid entry is preserved."""
    store_google_drive_credentials(_make_credentials(refresh_token="original"))

    # Bypass constructor validation to simulate a corrupted in-memory instance.
    corrupted = _make_credentials(refresh_token="valid-first")
    object.__setattr__(corrupted, "refresh_token", "")

    with pytest.raises(ValueError):
        store_google_drive_credentials(corrupted)

    loaded = load_google_drive_credentials()
    assert loaded is not None
    assert loaded.refresh_token == "original"


# ---------------------------------------------------------------------------
# Tests: corrupted stored data returns None safely
# ---------------------------------------------------------------------------


def _write_raw_keyring(_fake_keyring, raw: str) -> None:
    _fake_keyring.set_password("silovault", "google-drive", raw)


def test_load_malformed_json_returns_none(_fake_keyring):
    _write_raw_keyring(_fake_keyring, "{not valid json")
    assert load_google_drive_credentials() is None


def test_load_json_not_dict_returns_none(_fake_keyring):
    _write_raw_keyring(_fake_keyring, json.dumps(["a", "list"]))
    assert load_google_drive_credentials() is None


def test_load_missing_refresh_token_returns_none(_fake_keyring):
    _write_raw_keyring(_fake_keyring, json.dumps({"scopes": [DRIVE_FILE_SCOPE]}))
    assert load_google_drive_credentials() is None


def test_load_non_string_refresh_token_returns_none(_fake_keyring):
    _write_raw_keyring(
        _fake_keyring,
        json.dumps({"refresh_token": 999, "scopes": [DRIVE_FILE_SCOPE]}),
    )
    assert load_google_drive_credentials() is None


def test_load_null_scopes_returns_none(_fake_keyring):
    _write_raw_keyring(
        _fake_keyring,
        json.dumps({"refresh_token": "tok", "scopes": None}),
    )
    assert load_google_drive_credentials() is None


def test_load_wrong_scopes_type_returns_none(_fake_keyring):
    _write_raw_keyring(
        _fake_keyring,
        json.dumps({"refresh_token": "tok", "scopes": "drive.file"}),
    )
    assert load_google_drive_credentials() is None


def test_load_non_string_scope_entries_returns_none(_fake_keyring):
    _write_raw_keyring(
        _fake_keyring,
        json.dumps({"refresh_token": "tok", "scopes": [DRIVE_FILE_SCOPE, 42]}),
    )
    assert load_google_drive_credentials() is None


def test_load_wrong_root_folder_id_type_returns_none(_fake_keyring):
    _write_raw_keyring(
        _fake_keyring,
        json.dumps(
            {
                "refresh_token": "tok",
                "scopes": [DRIVE_FILE_SCOPE],
                "root_folder_id": 12345,
            }
        ),
    )
    assert load_google_drive_credentials() is None


def test_load_corrupted_data_does_not_expose_secrets_in_logs(
    _fake_keyring, caplog: pytest.LogCaptureFixture
):
    """Corrupted credential handling must not echo token contents to logs."""
    secret = "super-secret-refresh-token-value"
    _write_raw_keyring(
        _fake_keyring,
        json.dumps({"refresh_token": secret, "scopes": None}),
    )

    with caplog.at_level("WARNING"):
        result = load_google_drive_credentials()

    assert result is None
    for record in caplog.records:
        assert secret not in record.getMessage()


# ---------------------------------------------------------------------------
# Tests: keyring availability errors still surface
# ---------------------------------------------------------------------------


def test_load_keyring_locked_raises(monkeypatch: pytest.MonkeyPatch):
    def locked_get_keyring():
        raise CredentialStorageError("System credential store is locked.", cause=None)

    monkeypatch.setattr("backend.storage.credentials._get_keyring", locked_get_keyring)
    with pytest.raises(CredentialStorageError):
        load_google_drive_credentials()


def test_store_keyring_error_raises(_fake_keyring, monkeypatch: pytest.MonkeyPatch):
    def failing_set_password(service, account, data):
        raise KeyringError("simulated keyring write failure")

    monkeypatch.setattr(_fake_keyring, "set_password", failing_set_password)
    with pytest.raises(CredentialStorageError):
        store_google_drive_credentials(_make_credentials())


def test_load_keyring_error_raises(_fake_keyring, monkeypatch: pytest.MonkeyPatch):
    def failing_get_password(service, account):
        raise KeyringError("simulated keyring read failure")

    monkeypatch.setattr(_fake_keyring, "get_password", failing_get_password)
    with pytest.raises(CredentialStorageError):
        load_google_drive_credentials()
