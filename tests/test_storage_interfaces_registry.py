"""Tests for storage interfaces and the provider registry (Task 4 of M2).

Verifies:
- StorageOperations is an ABC with exactly seven mandatory abstract methods:
  upload, download, rename, move, quarantine, restore, cleanup.
- A concrete subclass implementing all seven methods can be instantiated.
- Missing any of the seven abstract methods prevents instantiation.
- StorageLifecycle is a separate, optional ABC (LocalAdapter need not implement it).
- Registry.register rejects duplicates and accepts well-typed inputs.
- Registry.replace overwrites an existing registration.
- Registry.lookup, get_capabilities, and remove behave correctly.
- Unknown provider lookups raise StorageUnavailableError.
- Provider ID validation rejects non-strings, empty/whitespace IDs, leading or
  trailing whitespace, and non-canonical (lowercase / invalid) formats, while
  accepting any well-formed uppercase ID (not just the six known providers).
- The storage interface and registry modules do not import FastAPI or
  SQLAlchemy, verified via an isolated subprocess import.
"""

import subprocess
import sys

import pytest

from backend.storage.capabilities import CAPABILITY_MAP, ProviderCapabilities
from backend.storage.errors import StorageUnavailableError
from backend.storage.interfaces import StorageLifecycle, StorageOperations
from backend.storage.registry import (
    get_capabilities,
    lookup,
    register,
    remove,
    replace,
)
from backend.storage.types import ProviderFileRecord


# ---------------------------------------------------------------------------
# Stub adapter helpers
# ---------------------------------------------------------------------------


class StubOperationsOnly(StorageOperations):
    """Minimal StorageOperations implementation with no Lifecycle mixin."""

    def upload(self, file_data, name, logical_path):
        return ProviderFileRecord(
            provider_file_id="stub-op",
            name=name,
            logical_path=logical_path,
            mime_type="application/octet-stream",
            size_bytes=len(file_data),
        )

    def download(self, provider_file_id):
        return b"stub-data"

    def rename(self, provider_file_id, new_name):
        return ProviderFileRecord(
            provider_file_id=provider_file_id,
            name=new_name,
            logical_path="/stub",
            mime_type="text/plain",
            size_bytes=9,
        )

    def move(self, source_provider_file_id, target_logical_path):
        return ProviderFileRecord(
            provider_file_id=source_provider_file_id,
            name="moved",
            logical_path=target_logical_path,
            mime_type="text/plain",
            size_bytes=5,
        )

    def quarantine(self, provider_file_id):
        return f"quarantine-of-{provider_file_id}"

    def restore(self, quarantine_id):
        return ProviderFileRecord(
            provider_file_id=quarantine_id,
            name="restored",
            logical_path="/stub",
            mime_type="text/plain",
            size_bytes=8,
        )

    def cleanup(self, quarantine_id):
        pass


class StubFullLifecycle(StubOperationsOnly, StorageLifecycle):
    """Adds the three Lifecycle methods to the stub."""

    def authenticate(self, config):
        pass

    def refresh(self):
        pass

    def disconnect(self):
        pass


def _make_stub_missing(missing_method: str):
    """Build a StorageOperations subclass that omits one abstract method.

    The full stub implements all seven operations; we strip the requested one
    so the ABC machinery must reject instantiation.
    """
    attrs = {
        "upload": StubOperationsOnly.upload,
        "download": StubOperationsOnly.download,
        "rename": StubOperationsOnly.rename,
        "move": StubOperationsOnly.move,
        "quarantine": StubOperationsOnly.quarantine,
        "restore": StubOperationsOnly.restore,
        "cleanup": StubOperationsOnly.cleanup,
    }
    attrs.pop(missing_method)
    return type(f"StubMissing_{missing_method}", (StorageOperations,), attrs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Give every test fresh registry dictionaries.

    Instead of draining the module-level dicts (which would destroy any
    registrations that existed before the test), we monkeypatch both
    ``_adapter_map`` and ``_capability_map`` with brand-new empty dicts.
    The registry functions resolve these names from the module's globals at
    call time, so patching the module attributes is sufficient.  monkeypatch
    restores the original dicts automatically after the test.
    """
    import backend.storage.registry as reg  # noqa: PLC0415

    monkeypatch.setattr(reg, "_adapter_map", {})
    monkeypatch.setattr(reg, "_capability_map", {})
    yield


# ---------------------------------------------------------------------------
# StorageOperations ABC: exactly seven mandatory abstract methods
# ---------------------------------------------------------------------------


def test_storage_operations_is_abc():
    """StorageOperations must be an abstract base class."""
    assert hasattr(StorageOperations, "__abstractmethods__")


def test_storage_operations_has_exactly_seven_abstract_methods():
    """The interface must declare exactly the seven approved operations."""
    expected = {
        "upload",
        "download",
        "rename",
        "move",
        "quarantine",
        "restore",
        "cleanup",
    }
    assert StorageOperations.__abstractmethods__ == frozenset(expected)


def test_storage_operations_has_no_delete_method():
    """The approved workflow uses quarantine/restore/cleanup, not delete()."""
    assert not hasattr(StorageOperations, "delete")


def test_storage_operations_cannot_be_instantiated_directly():
    """Instantiating the bare ABC must raise TypeError."""
    with pytest.raises(TypeError):
        StorageOperations()


def test_stub_operations_only_can_be_instantiated():
    """A class implementing all seven mandatory methods must be instantiable."""
    obj = StubOperationsOnly()
    assert isinstance(obj, StorageOperations)
    assert not isinstance(obj, StorageLifecycle)


@pytest.mark.parametrize(
    "missing_method",
    ["upload", "download", "rename", "move", "quarantine", "restore", "cleanup"],
)
def test_stub_missing_any_method_cannot_be_instantiated(missing_method):
    """Omitting any one of the seven abstract methods must block instantiation."""
    StubMissing = _make_stub_missing(missing_method)
    with pytest.raises(TypeError):
        StubMissing()


# ---------------------------------------------------------------------------
# StorageLifecycle ABC: separate and optional
# ---------------------------------------------------------------------------


def test_storage_lifecycle_is_abc():
    """StorageLifecycle must be an abstract base class."""
    assert hasattr(StorageLifecycle, "__abstractmethods__")


def test_storage_lifecycle_abstract_methods():
    expected = {"authenticate", "refresh", "disconnect"}
    assert StorageLifecycle.__abstractmethods__ == frozenset(expected)


def test_storage_lifecycle_not_required_for_local():
    """LocalAdapter-equivalent classes need not implement Lifecycle."""
    obj = StubOperationsOnly()
    assert isinstance(obj, StorageOperations)
    assert not isinstance(obj, StorageLifecycle)


def test_stub_full_lifecycle_implements_both():
    """A class mixing both ABCs satisfies both isinstance checks."""
    obj = StubFullLifecycle()
    assert isinstance(obj, StorageOperations)
    assert isinstance(obj, StorageLifecycle)


# ---------------------------------------------------------------------------
# Registry: register / lookup
# ---------------------------------------------------------------------------


def test_register_and_lookup_returns_adapter_and_capabilities():
    """Basic happy path: register then lookup returns the stored pair."""
    adapter = StubOperationsOnly()
    register("TEST_PROVIDER", adapter, CAPABILITY_MAP["LOCAL"])
    fetched_adapter, caps = lookup("TEST_PROVIDER")
    assert fetched_adapter is adapter
    assert caps is CAPABILITY_MAP["LOCAL"]


def test_get_capabilities_returns_only_profile():
    """Convenience accessor delegates to lookup internally."""
    adapter = StubOperationsOnly()
    register("TEST_CAPS", adapter, CAPABILITY_MAP["LOCAL"])
    caps = get_capabilities("TEST_CAPS")
    assert caps is CAPABILITY_MAP["LOCAL"]
    fetched_adapter, _ = lookup("TEST_CAPS")
    assert fetched_adapter is adapter


def test_register_validates_adapter_type():
    """Passing a non-StorageOperations object must raise TypeError."""
    with pytest.raises(TypeError, match="StorageOperations"):
        register("BAD_ADAPTER", "not-an-adapter", CAPABILITY_MAP["LOCAL"])


def test_register_validates_capabilities_type():
    """Passing a non-ProviderCapabilities object must raise TypeError."""
    with pytest.raises(TypeError, match="ProviderCapabilities"):
        register("BAD_CAPS", StubOperationsOnly(), "not-a-capability")


# ---------------------------------------------------------------------------
# Registry: provider ID validation
# ---------------------------------------------------------------------------


def test_register_accepts_valid_custom_provider_id():
    """Any well-formed uppercase ID is accepted — not just the six known ones."""
    adapter = StubOperationsOnly()
    register("MY_CUSTOM_CLOUD_2", adapter, CAPABILITY_MAP["LOCAL"])
    fetched, _ = lookup("MY_CUSTOM_CLOUD_2")
    assert fetched is adapter


def test_register_accepts_all_six_known_provider_ids():
    """The canonical provider IDs from the approved architecture all validate."""
    for pid in ("LOCAL", "GOOGLE_DRIVE", "ONEDRIVE", "DROPBOX", "BOX", "MEGA"):
        register(pid, StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])
    for pid in ("LOCAL", "GOOGLE_DRIVE", "ONEDRIVE", "DROPBOX", "BOX", "MEGA"):
        lookup(pid)  # must not raise


def test_register_rejects_non_string_provider_id():
    with pytest.raises(TypeError, match="provider_id must be a string"):
        register(12345, StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_register_rejects_empty_provider_id():
    with pytest.raises(StorageUnavailableError, match="non-empty"):
        register("", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_register_rejects_whitespace_only_provider_id():
    with pytest.raises(StorageUnavailableError, match="whitespace"):
        register("   ", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_register_rejects_leading_whitespace():
    with pytest.raises(StorageUnavailableError, match="whitespace"):
        register(" LOCAL", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_register_rejects_trailing_whitespace():
    with pytest.raises(StorageUnavailableError, match="whitespace"):
        register("LOCAL ", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_register_rejects_lowercase_provider_id():
    """Provider IDs are case-sensitive; lowercase must be rejected."""
    with pytest.raises(StorageUnavailableError, match="uppercase"):
        register("local", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_register_rejects_mixed_case_provider_id():
    with pytest.raises(StorageUnavailableError, match="uppercase"):
        register("Google_Drive", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_register_rejects_id_starting_with_digit():
    with pytest.raises(StorageUnavailableError, match="uppercase"):
        register("2FA_CLOUD", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_register_rejects_id_with_hyphen():
    with pytest.raises(StorageUnavailableError, match="uppercase"):
        register("GOOGLE-DRIVE", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_register_rejects_id_with_space_inside():
    with pytest.raises(StorageUnavailableError, match="uppercase"):
        register("GOOGLE DRIVE", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_provider_ids_are_case_sensitive():
    """'LOCAL' and 'Local' are distinct; only the canonical form registers."""
    adapter = StubOperationsOnly()
    register("LOCAL", adapter, CAPABILITY_MAP["LOCAL"])
    with pytest.raises(StorageUnavailableError, match="uppercase"):
        lookup("Local")
    # The valid registration is untouched.
    fetched, _ = lookup("LOCAL")
    assert fetched is adapter


# ---------------------------------------------------------------------------
# Registry: duplicate prevention
# ---------------------------------------------------------------------------


def test_duplicate_register_raises_storage_unavailable():
    """Registering the same provider ID twice must raise StorageUnavailableError."""
    adapter_a = StubOperationsOnly()
    adapter_b = StubOperationsOnly()
    register("DUP_TEST", adapter_a, CAPABILITY_MAP["LOCAL"])
    with pytest.raises(StorageUnavailableError, match="already registered"):
        register("DUP_TEST", adapter_b, CAPABILITY_MAP["LOCAL"])


def test_duplicate_register_message_mentions_replace():
    """The duplicate error must guide the caller toward replace()."""
    adapter_a = StubOperationsOnly()
    adapter_b = StubOperationsOnly()
    register("REPLACE_HINT", adapter_a, CAPABILITY_MAP["LOCAL"])
    exc = pytest.raises(
        StorageUnavailableError,
        lambda: register("REPLACE_HINT", adapter_b, CAPABILITY_MAP["LOCAL"]),
    )
    assert "replace()" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Registry: replace
# ---------------------------------------------------------------------------


def test_replace_overwrites_existing_registration():
    """replace() swaps the adapter and capabilities without raising."""
    adapter_a = StubOperationsOnly()
    adapter_b = StubOperationsOnly()
    register("REP_TEST", adapter_a, CAPABILITY_MAP["LOCAL"])
    replace("REP_TEST", adapter_b, CAPABILITY_MAP["GOOGLE_DRIVE"])
    fetched_adapter, caps = lookup("REP_TEST")
    assert fetched_adapter is adapter_b
    assert caps is CAPABILITY_MAP["GOOGLE_DRIVE"]


def test_replace_fails_when_provider_not_registered():
    """replace() on an unknown provider must raise StorageUnavailableError."""
    with pytest.raises(StorageUnavailableError, match="not registered"):
        replace("GHOST", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


def test_replace_validates_types():
    """replace() enforces type constraints like register()."""
    register("REP_TYPE", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])
    with pytest.raises(TypeError, match="StorageOperations"):
        replace("REP_TYPE", "bad", CAPABILITY_MAP["LOCAL"])
    with pytest.raises(TypeError, match="ProviderCapabilities"):
        replace("REP_TYPE", StubOperationsOnly(), "bad")


def test_replace_rejects_invalid_provider_id():
    """replace() runs the same ID validation as register()."""
    with pytest.raises(StorageUnavailableError, match="uppercase"):
        replace("lowercase", StubOperationsOnly(), CAPABILITY_MAP["LOCAL"])


# ---------------------------------------------------------------------------
# Registry: remove
# ---------------------------------------------------------------------------


def test_remove_clears_registration():
    """After remove(), lookup must raise StorageUnavailableError."""
    adapter = StubOperationsOnly()
    register("RM_TEST", adapter, CAPABILITY_MAP["LOCAL"])
    remove("RM_TEST")
    with pytest.raises(StorageUnavailableError):
        lookup("RM_TEST")
    with pytest.raises(StorageUnavailableError):
        get_capabilities("RM_TEST")


def test_remove_unknown_provider_raises():
    """Removing an unregistered provider must raise StorageUnavailableError."""
    with pytest.raises(StorageUnavailableError, match="not registered"):
        remove("ALSO_GHOST")


def test_remove_rejects_invalid_provider_id():
    with pytest.raises(StorageUnavailableError, match="non-empty"):
        remove("")


# ---------------------------------------------------------------------------
# Registry: unknown provider behavior
# ---------------------------------------------------------------------------


def test_lookup_unknown_provider_raises():
    """Looking up a provider that was never registered must raise."""
    with pytest.raises(StorageUnavailableError, match="Unregistered"):
        lookup("NEVER_REGISTERED")


def test_get_capabilities_unknown_provider_raises():
    """get_capabilities on an unknown provider must delegate to lookup's error."""
    with pytest.raises(StorageUnavailableError):
        get_capabilities("NEVER_REGISTERED_EITHER")


def test_unknown_provider_error_includes_provider_name():
    """The error message should identify the missing provider."""
    exc = pytest.raises(StorageUnavailableError, lambda: lookup("MISSING_XYZ"))
    assert "MISSING_XYZ" in str(exc.value)


# ---------------------------------------------------------------------------
# Cross-provider capability preservation
# ---------------------------------------------------------------------------


def test_different_providers_can_be_registered_independently():
    """Two distinct provider IDs coexist without interfering."""
    local_adapter = StubOperationsOnly()
    cloud_adapter = StubOperationsOnly()
    register("LOCAL", local_adapter, CAPABILITY_MAP["LOCAL"])
    register("GOOGLE_DRIVE", cloud_adapter, CAPABILITY_MAP["GOOGLE_DRIVE"])
    _, local_caps = lookup("LOCAL")
    _, cloud_caps = lookup("GOOGLE_DRIVE")
    assert local_caps is CAPABILITY_MAP["LOCAL"]
    assert cloud_caps is CAPABILITY_MAP["GOOGLE_DRIVE"]
    # Swapping one must not affect the other.
    new_local = StubOperationsOnly()
    replace("LOCAL", new_local, CAPABILITY_MAP["LOCAL"])
    _, recheck_cloud = lookup("GOOGLE_DRIVE")
    assert recheck_cloud is CAPABILITY_MAP["GOOGLE_DRIVE"]


# ---------------------------------------------------------------------------
# Framework independence: FastAPI / SQLAlchemy must not be pulled in
# ---------------------------------------------------------------------------
#
# Approach: run a fresh Python subprocess that imports ONLY the two storage
# modules and then reports whether "fastapi" or "sqlalchemy" appear in
# sys.modules.  A subprocess is used because the main test process already
# loads FastAPI and SQLAlchemy (via the app fixtures in other test files), so
# checking sys.modules in-process would be a false negative.  The subprocess
# starts clean, so any presence of those packages proves the storage modules
# imported them transitively.


def _check_no_framework_imports() -> str:
    """Import the storage modules in a clean interpreter; report offenders."""
    code = (
        "import sys\n"
        "import backend.storage.interfaces  # noqa: F401\n"
        "import backend.storage.registry  # noqa: F401\n"
        "offenders = sorted(m for m in sys.modules if m.split('.')[0] in ('fastapi', 'sqlalchemy'))\n"
        "print('OFFENDERS:' + ','.join(offenders))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=".",
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    for line in result.stdout.splitlines():
        if line.startswith("OFFENDERS:"):
            return line[len("OFFENDERS:"):]
    raise AssertionError("OFFENDERS marker missing from subprocess output")


def test_storage_modules_do_not_import_fastapi():
    """Importing interfaces + registry in a clean interpreter must not load FastAPI."""
    offenders = _check_no_framework_imports()
    assert "fastapi" not in offenders, f"fastapi was imported: {offenders}"


def test_storage_modules_do_not_import_sqlalchemy():
    """Importing interfaces + registry in a clean interpreter must not load SQLAlchemy."""
    offenders = _check_no_framework_imports()
    assert "sqlalchemy" not in offenders, f"sqlalchemy was imported: {offenders}"
