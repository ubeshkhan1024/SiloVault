"""Focused tests for M4.2 model storage (Step 5)."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from backend.ai import ModelStorage
from backend.ai.interfaces import ModelDescriptor, ModelRegistry
from backend.config import Settings


def _descriptor(model_id="vendor.model", local_path=None):
    return ModelDescriptor(
        name="Test Model",
        version="1.0",
        modalities=("text",),
        model_id=model_id,
        local_path=local_path,
    )


def test_windows_default_root_uses_local_appdata(tmp_path):
    storage = ModelStorage(
        system="Windows",
        environ={"LOCALAPPDATA": str(tmp_path / "LocalAppData")},
        home=tmp_path / "home",
    )
    assert storage.root == (tmp_path / "LocalAppData" / "SiloVault" / "models").resolve()


def test_windows_default_root_falls_back_to_home(tmp_path):
    storage = ModelStorage(
        system="Windows", environ={}, home=tmp_path / "home"
    )
    assert storage.root == (
        tmp_path / "home" / "AppData" / "Local" / "SiloVault" / "models"
    ).resolve()


def test_linux_default_root_uses_xdg_data_home(tmp_path):
    storage = ModelStorage(
        system="Linux",
        environ={"XDG_DATA_HOME": str(tmp_path / "data")},
        home=tmp_path / "home",
    )
    assert storage.root == (tmp_path / "data" / "silovault" / "models").resolve()


def test_linux_default_root_falls_back_to_local_share(tmp_path):
    storage = ModelStorage(system="Linux", environ={}, home=tmp_path / "home")
    assert storage.root == (
        tmp_path / "home" / ".local" / "share" / "silovault" / "models"
    ).resolve()


def test_android_requires_explicit_private_root():
    with pytest.raises(ValueError, match="Android"):
        ModelStorage(system="Android", environ={}, home=Path("/home/tester"))


def test_explicit_root_override_works_on_android(tmp_path):
    storage = ModelStorage(
        root=tmp_path / "app-private" / "models", system="Android"
    )
    assert storage.root == (tmp_path / "app-private" / "models").resolve()


@pytest.mark.parametrize(
    "identifier",
    ["vendor.model", "text_classifier_efficient", "model-1", "model_v2"],
)
def test_valid_identifiers_resolve_inside_root(tmp_path, identifier):
    storage = ModelStorage(root=tmp_path / "models")
    directory = storage.model_directory(identifier)

    assert directory == (tmp_path / "models" / identifier).resolve()
    assert directory.parent == storage.root


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "   ",
        " model",
        "model ",
        ".",
        "..",
        "../escape",
        "..\\escape",
        "a/b",
        "a\\b",
        "nul\x00byte",
        "C:model",
        "model.",
    ],
)
def test_invalid_identifiers_are_rejected(tmp_path, identifier):
    storage = ModelStorage(root=tmp_path / "models")
    with pytest.raises(ValueError):
        storage.model_directory(identifier)


def test_path_traversal_cannot_escape_root(tmp_path):
    storage = ModelStorage(root=tmp_path / "models")

    with pytest.raises(ValueError):
        storage.model_directory("../outside")
    with pytest.raises(ValueError):
        storage.model_directory("..")

    valid = storage.model_directory("vendor.model")
    assert valid.is_relative_to(storage.root)
    assert valid.parent == storage.root


def test_initialization_does_not_touch_the_filesystem(tmp_path):
    root = tmp_path / "missing" / "models"
    storage = ModelStorage(root=root)

    assert storage.root == root.resolve()
    assert not root.exists()
    assert list(tmp_path.iterdir()) == []


def test_prepare_model_directory_creates_only_on_request(tmp_path):
    storage = ModelStorage(root=tmp_path / "models")
    expected = storage.model_directory("vendor.model")
    assert not expected.exists()

    created = storage.prepare_model_directory("vendor.model")
    assert created == expected
    assert created.is_dir()

    # Idempotent, and still contained in the root.
    assert storage.prepare_model_directory("vendor.model") == expected
    assert created.is_relative_to(storage.root)


def test_missing_and_installed_detection(tmp_path):
    storage = ModelStorage(root=tmp_path / "models")
    descriptor = _descriptor()

    assert storage.is_installed(descriptor) is False
    storage.prepare_model_directory(descriptor.identifier)
    assert storage.is_installed(descriptor) is True


def test_descriptor_local_path_takes_precedence(tmp_path):
    storage = ModelStorage(root=tmp_path / "models")
    explicit = tmp_path / "explicit" / "model"
    descriptor = _descriptor(local_path=explicit)

    assert storage.is_installed(descriptor) is False
    explicit.mkdir(parents=True)
    assert storage.is_installed(descriptor) is True

    # The descriptor is never mutated and its explicit path is preserved.
    assert descriptor.local_path == explicit
    assert descriptor.identifier == "vendor.model"


def test_storage_does_not_change_descriptor_or_registry_behaviour(tmp_path):
    storage = ModelStorage(root=tmp_path / "models")
    descriptor = _descriptor()
    registry = ModelRegistry()
    registry.register(descriptor)

    storage.is_installed(descriptor)
    storage.model_directory(descriptor.identifier)

    assert registry.get("vendor.model") is descriptor
    assert registry.find("vendor.model") is descriptor
    assert descriptor.local_path is None
    with pytest.raises(FrozenInstanceError):
        descriptor.local_path = tmp_path  # type: ignore[misc]


def test_models_dir_setting_is_optional_and_env_configurable(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MODELS_DIR", raising=False)
    assert Settings(_env_file=None).models_dir is None

    monkeypatch.setenv("MODELS_DIR", str(tmp_path / "models"))
    assert Settings(_env_file=None).models_dir == str(tmp_path / "models")
