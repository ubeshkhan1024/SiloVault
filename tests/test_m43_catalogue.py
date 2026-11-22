"""Focused tests for the trusted, code-controlled model catalogue."""

import hashlib

import pytest

from backend.ai import catalogue
from backend.ai.bootstrap import build_ai_model_services
from backend.ai.catalogue import MODEL_CATALOGUE, register_catalogue
from backend.ai.interfaces import ModelDescriptor, ModelRegistry, ModelValidationError
from backend.config import Settings

SYNTHETIC_PAYLOAD = b"synthetic catalogue payload"
SYNTHETIC_SHA256 = hashlib.sha256(SYNTHETIC_PAYLOAD).hexdigest()
SYNTHETIC_SHA512 = hashlib.sha512(SYNTHETIC_PAYLOAD).hexdigest()


def _descriptor(**overrides):
    """Clearly synthetic metadata; not a real model or real URL."""
    values = dict(
        name="Synthetic Catalogue Model",
        version="1.0.0",
        modalities=("text",),
        model_id="vendor.synthetic.model",
        download_url="https://models.example.test/synthetic/model.bin",
        checksum=SYNTHETIC_SHA256,
        model_size_bytes=len(SYNTHETIC_PAYLOAD),
    )
    values.update(overrides)
    return ModelDescriptor(**values)


def test_default_catalogue_is_empty():
    assert MODEL_CATALOGUE == ()
    registry = ModelRegistry()
    assert register_catalogue(registry) == []
    assert registry.list_ids() == []


def test_valid_descriptor_is_registered():
    descriptor = _descriptor()
    registry = ModelRegistry()

    identifiers = register_catalogue(registry, (descriptor,))

    assert identifiers == ["vendor.synthetic.model"]
    assert registry.get("vendor.synthetic.model") is descriptor


def test_sha512_installable_descriptor_is_accepted():
    descriptor = _descriptor(checksum=f"sha512:{SYNTHETIC_SHA512}")
    registry = ModelRegistry()

    assert register_catalogue(registry, (descriptor,)) == [descriptor.identifier]


def test_bootstrap_registers_catalogue_in_the_shared_registry(
    tmp_path, monkeypatch
):
    descriptor = _descriptor()
    monkeypatch.setattr(catalogue, "MODEL_CATALOGUE", (descriptor,))

    services = build_ai_model_services(
        Settings(_env_file=None, models_dir=str(tmp_path / "models"))
    )

    assert services.registry.get(descriptor.identifier) is descriptor
    assert services.manager.model_registry is services.registry
    assert services.manager.get_model(descriptor.identifier) is descriptor
    assert not (tmp_path / "models").exists()


def test_duplicate_registration_is_idempotent():
    descriptor = _descriptor()
    registry = ModelRegistry()

    first = register_catalogue(registry, (descriptor,))
    second = register_catalogue(registry, (descriptor,))

    assert first == second == [descriptor.identifier]
    assert registry.list_models() == [descriptor]


def test_conflicting_registration_is_rejected():
    original = _descriptor()
    registry = ModelRegistry()
    registry.register(original)

    conflicting = _descriptor(version="2.0.0")
    with pytest.raises(ModelValidationError, match="different metadata"):
        register_catalogue(registry, (conflicting,))

    assert registry.get(original.identifier) is original
    assert registry.get(original.identifier).version == "1.0.0"


@pytest.mark.parametrize(
    "descriptor",
    [
        # Registry-level metadata validation.
        _descriptor(modalities=()),
        _descriptor(checksum="sha256:abcd"),
        # Catalogue policy: installable entries must use HTTPS.
        _descriptor(download_url="http://models.example.test/model.bin"),
        # Catalogue policy: installable entries require a checksum.
        _descriptor(checksum=None),
        # Catalogue policy: only algorithms the installer supports.
        _descriptor(checksum="md5:" + "0" * 32),
        _descriptor(checksum="sha512:" + "0" * 127),
    ],
)
def test_invalid_catalogue_metadata_is_rejected(descriptor):
    registry = ModelRegistry()

    with pytest.raises(ModelValidationError):
        register_catalogue(registry, (descriptor,))

    assert registry.list_ids() == []


def test_non_descriptor_entry_is_rejected():
    registry = ModelRegistry()

    with pytest.raises(ModelValidationError, match="ModelDescriptor"):
        register_catalogue(registry, ({"name": "not a descriptor"},))
