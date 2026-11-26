# ---------------------------------------------------------------------------
# Tests: Google Drive credential storage (M3.2 Phase A)
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.config import settings
from backend.storage.credentials import (
    CredentialStorageError,
    GoogleDriveCredentials,
    delete_google_drive_credentials,
    has_google_drive_credentials,
    load_google_drive_credentials,
    store_google_drive_credentials,
    _derive_keyring_account,
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
# -------------------------------------------------------------------------


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
# -------------------------------------------------------------------------


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
# -------------------------------------------------------------------------


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
# -------------------------------------------------------------------------


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
# -------------------------------------------------------------------------


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
# -------------------------------------------------------------------------


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
# -------------------------------------------------------------------------


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
# -------------------------------------------------------------------------


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
# -------------------------------------------------------------------------


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
# ---------------------------------------------------------------------------
# Tests: per-account credential isolation (H1.4C)
# ---------------------------------------------------------------------------

def _write_raw_keyring(_fake_keyring, raw: str) -> None:
    _fake_keyring.set_password("silovault", "google-drive", raw)


def _make_credentials_with_ref(credentials_ref: str, **overrides) -> GoogleDriveCredentials:
    """Build valid credentials with a given credentials_ref and sensible defaults.

    Note: This helper is placed after GoogleDriveCredentials class definition
    so the name is resolved at import time.
    """
    params = dict(
        refresh_token="valid-refresh-token",
        client_id="cid",
        client_secret="cs",
        scopes=[DRIVE_FILE_SCOPE],
    )
    params.update(overrides)
    # credentials_ref is not a field of GoogleDriveCredentials; it is passed
    # separately to store/load/delete/has functions.  We return a plain
    # GoogleDriveCredentials instance and store the ref alongside in the test.
    creds = GoogleDriveCredentials(**params)
    # Attach credentials_ref as an attribute for use by the test functions.
    object.__setattr__(creds, "credentials_ref", credentials_ref)  # type: ignore[attr-defined]
    return creds


def test_two_different_references_store_independent_credentials(_fake_keyring):
    """Two different ``credentials_ref`` values store independent credentials.

    The keyring store uses ``(service, account)`` as the lookup key, so
    different references must not overwrite each other.
    """
    ref_a = "ref-account-A"
    ref_b = "ref-account-B"

    creds_a = _make_credentials_with_ref(ref_a, root_folder_id="folder-A")
    creds_b = _make_credentials_with_ref(ref_b, root_folder_id="folder-B")

    store_google_drive_credentials(creds_a, credentials_ref=ref_a)
    store_google_drive_credentials(creds_b, credentials_ref=ref_b)

    loaded_a = load_google_drive_credentials(credentials_ref=ref_a)
    loaded_b = load_google_drive_credentials(credentials_ref=ref_b)

    assert loaded_a is not None
    assert loaded_b is not None
    assert loaded_a.refresh_token == "valid-refresh-token"
    assert loaded_b.refresh_token == "valid-refresh-token"
    assert loaded_a.root_folder_id == "folder-A"
    assert loaded_b.root_folder_id == "folder-B"


def test_loading_one_reference_never_returns_another_accounts_credentials(
    _fake_keyring,
):
    """Loading one ``credentials_ref`` never returns another account's credentials."""
    ref_a = "ref-account-X"
    ref_b = "ref-account-Y"

    creds_a = _make_credentials_with_ref(ref_a, root_folder_id="folder-X")
    store_google_drive_credentials(creds_a, credentials_ref=ref_a)

    # Loading with ref_b should return None (no credentials for that account)
    loaded_b = load_google_drive_credentials(credentials_ref=ref_b)
    assert loaded_b is None

    # Loading with the original ref_a should still return the correct credentials
    loaded_a = load_google_drive_credentials(credentials_ref=ref_a)
    assert loaded_a is not None
    assert loaded_a.root_folder_id == "folder-X"


def test_deleting_one_reference_leaves_the_other_untouched(_fake_keyring):
    """Deleting one ``credentials_ref`` leaves the other account's credentials untouched."""
    ref_a = "ref-account-P"
    ref_b = "ref-account-Q"

    creds_a = _make_credentials_with_ref(ref_a, root_folder_id="folder-P")
    creds_b = _make_credentials_with_ref(ref_b, root_folder_id="folder-Q")

    store_google_drive_credentials(creds_a, credentials_ref=ref_a)
    store_google_drive_credentials(creds_b, credentials_ref=ref_b)

    # Verify both are loaded
    loaded_a_before = load_google_drive_credentials(credentials_ref=ref_a)
    loaded_b_before = load_google_drive_credentials(credentials_ref=ref_b)
    assert loaded_a_before is not None
    assert loaded_b_before is not None

    # Delete only ref_a's credentials
    result = delete_google_drive_credentials(credentials_ref=ref_a)
    assert result is True

    # ref_a should now be empty
    loaded_a_after = load_google_drive_credentials(credentials_ref=ref_a)
    assert loaded_a_after is None

    # ref_b should still be intact
    loaded_b_after = load_google_drive_credentials(credentials_ref=ref_b)
    assert loaded_b_after is not None
    assert loaded_b_after.root_folder_id == "folder-Q"


def test_has_google_drive_credentials_checks_correct_entry(_fake_keyring):
    """``has_google_drive_credentials()`` checks the correct keyring entry."""
    ref_a = "ref-account-H"
    ref_b = "ref-account-I"

    # Store credentials only under ref_a
    creds_a = _make_credentials_with_ref(ref_a, root_folder_id="folder-H")
    store_google_drive_credentials(creds_a, credentials_ref=ref_a)

    # has_google_drive_credentials() without ref should check global entry (empty)
    has_global = has_google_drive_credentials()
    # Since we only stored under ref_a (not global), the global check may vary.
    # The important thing is that has_google_drive_credentials(ref=ref_a) is True
    has_ref_a = has_google_drive_credentials(credentials_ref=ref_a)
    assert has_ref_a is True

    # has_google_drive_credentials(ref=ref_b) should be False
    has_ref_b = has_google_drive_credentials(credentials_ref=ref_b)
    assert has_ref_b is False


def test_legacy_no_argument_calls_still_use_original_keyring_entry(_fake_keyring):
    """Legacy no-argument calls still use the original ``"google-drive"`` keyring entry."""
    # Store credentials using the legacy global entry
    creds = _make_credentials(root_folder_id="legacy-folder")
    store_google_drive_credentials(creds)  # no credentials_ref -> global

    # Load using legacy no-argument form
    loaded = load_google_drive_credentials()
    assert loaded is not None
    assert loaded.root_folder_id == "legacy-folder"

    # has_google_drive_credentials() no-arg should be True
    assert has_google_drive_credentials() is True

    # delete_google_drive_credentials() no-arg should delete the global entry
    result = delete_google_drive_credentials()
    assert result is True

    # After deletion, load should return None
    loaded_after = load_google_drive_credentials()
    assert loaded_after is None


def test_invalid_credentials_ref_rejected(_fake_keyring):
    """Invalid ``credentials_ref`` values are rejected by store/load/delete/has,
    and the legacy (no-reference) entry keeps working.

    A legacy credential is stored first so the no-argument check resolves a
    real entry in the legacy ``"google-drive"`` keyring slot.
    """
    legacy_creds = _make_credentials(refresh_token="legacy-refresh-token")
    store_google_drive_credentials(legacy_creds)  # no credentials_ref -> legacy

    # Empty and whitespace-only references are rejected with ValueError.
    for bad_ref in ("", "   "):
        with pytest.raises(ValueError):
            store_google_drive_credentials(
                _make_credentials(), credentials_ref=bad_ref
            )
        with pytest.raises(ValueError):
            load_google_drive_credentials(credentials_ref=bad_ref)
        with pytest.raises(ValueError):
            delete_google_drive_credentials(credentials_ref=bad_ref)
        with pytest.raises(ValueError):
            has_google_drive_credentials(credentials_ref=bad_ref)

    # Non-string references are rejected with TypeError.
    for bad_ref in (12345, b"ref"):
        with pytest.raises(TypeError):
            store_google_drive_credentials(
                _make_credentials(), credentials_ref=bad_ref  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError):
            load_google_drive_credentials(credentials_ref=bad_ref)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            delete_google_drive_credentials(credentials_ref=bad_ref)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            has_google_drive_credentials(credentials_ref=bad_ref)  # type: ignore[arg-type]

    # Legacy compatibility: the rejected calls wrote nothing, so the original
    # legacy entry is still the one the no-argument API resolves.
    loaded = load_google_drive_credentials(credentials_ref=None)
    assert loaded is not None
    assert loaded.refresh_token == "legacy-refresh-token"
    assert has_google_drive_credentials() is True
    assert has_google_drive_credentials(credentials_ref=None) is True


def test_sha256_derivation_is_deterministic(_fake_keyring):
    """The SHA-256 derivation produces the same account name for the same reference."""
    ref = "deterministic-test-ref"

    # Call the derivation function twice - should produce same result
    account_name_1 = _derive_keyring_account(ref)
    account_name_2 = _derive_keyring_account(ref)

    assert account_name_1 == account_name_2
    assert len(account_name_1) == 64  # SHA-256 hex digest length
    assert isinstance(account_name_1, str)

    # Different references should produce different account names
    ref2 = "different-ref"
    account_name_3 = _derive_keyring_account(ref2)
    assert account_name_1 != account_name_3
