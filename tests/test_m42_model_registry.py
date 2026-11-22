"""Focused tests for the M4.2 model registry and metadata layer.

All descriptors are synthetic; nothing here touches model files, networks, or
real hardware.
"""

from unittest.mock import MagicMock

import pytest

from backend.ai.interfaces import (
    CapabilityRegistry,
    GpuInfo,
    HardwareInfo,
    ModelDescriptor,
    ModelManager,
    ModelRegistry,
    ModelValidationError,
    Profile,
)

GIB = 1024 ** 3


def _raw(**overrides) -> ModelDescriptor:
    data = {
        "name": "Synthetic Classifier",
        "version": "1.0.0",
        "modalities": ("text",),
        "model_id": "vendor.synthetic.classifier",
        "compatible_runtimes": ("onnx",),
    }
    data.update(overrides)
    return ModelDescriptor(**data)


def _hardware(**overrides) -> HardwareInfo:
    data = {
        "os_name": "Windows",
        "total_ram_bytes": 32 * GIB,
        "logical_cores": 16,
        "gpus": (GpuInfo("Synthetic GPU", "NVIDIA", 8 * GIB),),
    }
    data.update(overrides)
    return HardwareInfo(**data)


# ---------------------------------------------------------------------------
# Registration and retrieval
# ---------------------------------------------------------------------------

def test_register_and_retrieve_model():
    registry = ModelRegistry()
    descriptor = _raw()

    identifier = registry.register(descriptor)

    assert identifier == "vendor.synthetic.classifier"
    assert registry.get(identifier) is descriptor
    assert registry.find(identifier) is descriptor
    assert registry.is_registered(identifier) is True
    assert identifier in registry
    assert len(registry) == 1


def test_identity_is_model_id_not_display_name():
    registry = ModelRegistry()
    descriptor = _raw(name="Readable Display Name")
    registry.register(descriptor)

    # Display names are not identifiers.
    assert registry.find("Readable Display Name") is None
    assert registry.get("vendor.synthetic.classifier") is descriptor
    assert registry.find_by_name("Readable Display Name") is descriptor


def test_duplicate_model_id_is_rejected():
    registry = ModelRegistry()
    registry.register(_raw())

    duplicate = _raw(name="Another Display Name", version="2.0.0")
    with pytest.raises(ModelValidationError) as excinfo:
        registry.register(duplicate)
    assert "already registered" in str(excinfo.value)
    assert len(registry) == 1


def test_same_display_name_with_distinct_ids_is_allowed_but_not_ambiguous():
    registry = ModelRegistry()
    first = _raw(model_id="vendor.a", name="Shared Name")
    second = _raw(model_id="vendor.b", name="Shared Name")
    registry.register(first)
    registry.register(second)

    assert registry.get("vendor.a") is first
    assert registry.get("vendor.b") is second
    # Ambiguous display name never silently resolves to one model.
    assert registry.find_by_name("Shared Name") is None


def test_unknown_model_lookup_is_predictable():
    registry = ModelRegistry()

    with pytest.raises(KeyError):
        registry.get("missing.model")
    assert registry.find("missing.model") is None
    assert registry.is_registered("missing.model") is False


# ---------------------------------------------------------------------------
# Listing and filtering
# ---------------------------------------------------------------------------

def test_listing_and_filtering():
    registry = ModelRegistry()
    text_model = _raw(
        model_id="vendor.text",
        modalities=("text",),
        supported_formats=(".pdf", ".txt"),
        compatible_runtimes=("onnx",),
        supported_platforms=("Windows", "Linux"),
    )
    vision_model = _raw(
        model_id="vendor.vision",
        name="Synthetic Vision",
        modalities=("vision",),
        supported_formats=(".jpg",),
        compatible_runtimes=("torch",),
        supported_platforms=("Windows",),
    )
    agnostic_model = _raw(
        model_id="vendor.agnostic",
        name="Synthetic Agnostic",
        modalities=("text", "vision"),
        supported_formats=None,
        compatible_runtimes=None,
        supported_platforms=None,
    )
    for model in (text_model, vision_model, agnostic_model):
        registry.register(model)

    assert registry.list_ids() == [
        "vendor.text",
        "vendor.vision",
        "vendor.agnostic",
    ]
    assert registry.list_models() == [text_model, vision_model, agnostic_model]
    assert registry.list_by_modality("text") == [text_model, agnostic_model]
    assert registry.list_by_modality("VISION") == [vision_model, agnostic_model]
    assert registry.list_by_format(".PDF") == [text_model]
    assert registry.list_by_runtime("onnx") == [text_model]
    assert registry.list_for_platform("windows") == [
        text_model,
        vision_model,
        agnostic_model,
    ]
    assert registry.list_for_platform("linux") == [text_model, agnostic_model]

    # Returned collections are copies, not internal state.
    listed = registry.list_models()
    listed.clear()
    listed_ids = registry.list_ids()
    listed_ids.clear()
    assert len(registry) == 3


# ---------------------------------------------------------------------------
# Metadata validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "descriptor",
    [
        _raw(model_id=""),
        _raw(model_id=None, name=""),
        _raw(model_id="   "),
        _raw(model_id="bad id"),
        _raw(model_id=" padded.id "),
        _raw(model_id="x" * 201),
        _raw(name=""),
        _raw(version=""),
        _raw(modalities=()),
        _raw(modalities=("text", "")),
        _raw(modalities="text"),
        _raw(supported_formats=()),
        _raw(supported_formats=(".pdf", " ")),
        _raw(compatible_runtimes=()),
        _raw(supported_platforms=()),
        _raw(supported_platforms=("Windows", "")),
        _raw(model_size_bytes=0),
        _raw(model_size_bytes=-1),
        _raw(model_size_bytes=True),
        _raw(checksum="abc"),
        _raw(checksum="sha256:zzzzzzzzzzzzzzzz"),
        _raw(licensing="   "),
        _raw(hardware_requirements={"ram_gb": ""}),
        _raw(hardware_requirements={"": "8"}),
        _raw(hardware_requirements=["ram_gb"]),
        _raw(download_url="ftp://models.example.invalid/model.bin"),
        _raw(download_url="not-a-url"),
    ],
)
def test_invalid_metadata_is_rejected(descriptor):
    registry = ModelRegistry()
    with pytest.raises(ModelValidationError):
        registry.register(descriptor)


def test_valid_full_metadata_is_accepted():
    descriptor = _raw(
        supported_formats=(".pdf", ".txt"),
        compatible_runtimes=("onnx", "torch"),
        supported_platforms=("Windows", "Linux"),
        model_size_bytes=123456,
        checksum="sha256:" + "a" * 64,
        licensing="Apache-2.0",
        hardware_requirements={"min_ram_gb": "8", "logical_cores": "4"},
        download_url="https://models.example.invalid/synthetic.onnx",
    )
    registry = ModelRegistry()

    assert registry.register(descriptor) == "vendor.synthetic.classifier"
    assert registry.get("vendor.synthetic.classifier") is descriptor


def test_unknown_optional_metadata_stays_optional():
    descriptor = _raw(
        supported_formats=None,
        compatible_runtimes=None,
        supported_platforms=None,
        model_size_bytes=None,
        checksum=None,
        licensing=None,
        hardware_requirements=None,
        download_url=None,
    )
    registry = ModelRegistry()
    identifier = registry.register(descriptor)

    stored = registry.get(identifier)
    assert stored.checksum is None
    assert stored.licensing is None
    assert stored.model_size_bytes is None
    assert stored.download_url is None
    assert registry.runtime_compatibility(identifier, "onnx") is None
    assert registry.hardware_compatibility(identifier, _hardware()) is None


def test_validation_does_not_mutate_the_descriptor():
    descriptor = _raw(licensing=None)
    registry = ModelRegistry()
    registry.register(descriptor)
    assert descriptor.licensing is None


# ---------------------------------------------------------------------------
# ModelManager integration
# ---------------------------------------------------------------------------

def test_model_manager_uses_central_registry():
    manager = ModelManager()
    descriptor = _raw(model_id="vendor.eff", name="text_classifier_efficient")

    manager.register_capability(
        "text_classification", descriptor, profile=Profile.EFFICIENT
    )

    assert manager.model_registry.find("vendor.eff") is descriptor
    assert manager.get_model("vendor.eff") is descriptor
    assert manager.list_models() == [descriptor]
    assert manager.list_descriptors() == ["vendor.eff"]
    assert manager.models_for_capability("text_classification") == [descriptor]
    assert manager.find_model("unknown.model") is None


def test_register_capability_rejects_conflicting_metadata():
    manager = ModelManager()
    catalogued = _raw(model_id="vendor.model", name="Catalogued", version="1.0")
    manager.register_descriptor(catalogued)
    conflicting = _raw(model_id="vendor.model", name="Conflicting", version="9.9")

    with pytest.raises(ModelValidationError) as excinfo:
        manager.register_capability(
            "text_classification", conflicting, Profile.BALANCED
        )
    assert "different metadata" in str(excinfo.value)

    # The catalogue entry is unchanged and still canonical.
    stored = manager.get_model("vendor.model")
    assert stored is catalogued
    assert stored.version == "1.0"
    assert stored.name == "Catalogued"

    # The conflicting descriptor never reached the capability registry.
    assert (
        manager.capability_registry.resolve("text_classification", Profile.BALANCED)
        is None
    )
    assert manager.capability_registry.has_capability("text_classification") is False
    assert manager.models_for_capability("text_classification") == []


def test_register_capability_reuses_the_canonical_descriptor():
    manager = ModelManager()
    catalogued = _raw(model_id="vendor.model")
    manager.register_descriptor(catalogued)

    # Equal metadata in a distinct object must map to the catalogue instance.
    equal_copy = _raw(model_id="vendor.model")
    assert equal_copy == catalogued
    assert equal_copy is not catalogued

    manager.register_capability(
        "text_classification", equal_copy, Profile.EFFICIENT
    )

    resolved = manager.get_capability_descriptor("text_classification")
    assert resolved is catalogued
    assert manager.get_model("vendor.model") is catalogued
    assert manager.models_for_capability("text_classification") == [catalogued]


def test_identical_metadata_shares_one_canonical_descriptor_across_profiles():
    manager = ModelManager()
    efficient = _raw(model_id="vendor.model", name="Canonical")
    balanced = _raw(model_id="vendor.model", name="Canonical")
    assert efficient == balanced and efficient is not balanced

    manager.register_capability(
        "text_classification", efficient, Profile.EFFICIENT
    )
    manager.register_capability(
        "text_classification", balanced, Profile.BALANCED
    )

    assert manager.get_model("vendor.model") is efficient
    catalogued = manager.model_registry.list_models()
    assert catalogued == [efficient]

    manager.set_profile(Profile.EFFICIENT)
    assert manager.get_capability_descriptor("text_classification") is efficient
    manager.set_profile(Profile.BALANCED)
    assert manager.get_capability_descriptor("text_classification") is efficient


def test_profile_specific_ids_still_resolve_independently():
    manager = ModelManager()
    efficient = _raw(model_id="vendor.eff", name="Efficient")
    balanced = _raw(model_id="vendor.balanced", name="Balanced")

    manager.register_capability(
        "text_classification", efficient, Profile.EFFICIENT
    )
    manager.register_capability(
        "text_classification", balanced, Profile.BALANCED
    )

    assert manager.model_registry.list_ids() == ["vendor.eff", "vendor.balanced"]
    manager.set_profile(Profile.EFFICIENT)
    assert manager.get_capability_descriptor("text_classification") is efficient
    manager.set_profile(Profile.BALANCED)
    assert manager.get_capability_descriptor("text_classification") is balanced


def test_orphan_capability_mapping_fails_closed():
    manager = ModelManager()
    orphan = _raw(model_id="vendor.orphan", name="Orphan")
    # Bypass ModelManager and write straight into the capability registry.
    manager.capability_registry.register(
        "text_classification", orphan, Profile.EFFICIENT
    )

    assert manager.get_capability_descriptor("text_classification") is None
    assert manager.models_for_capability("text_classification") == []
    assert manager.list_models() == []


def test_catalogued_descriptor_resolves_through_direct_mapping():
    manager = ModelManager()
    descriptor = _raw(model_id="vendor.canonical")
    manager.register_descriptor(descriptor)
    # Profile-agnostic mapping that references the catalogue instance.
    manager.capability_registry.register("text_classification", descriptor)

    manager.set_profile(Profile.EFFICIENT)
    assert manager.get_capability_descriptor("text_classification") is descriptor
    manager.set_profile(Profile.BALANCED)
    assert manager.get_capability_descriptor("text_classification") is descriptor


def test_equal_copy_direct_mapping_resolves_to_canonical():
    manager = ModelManager()
    canonical = _raw(model_id="vendor.canonical")
    manager.register_descriptor(canonical)
    copy = _raw(model_id="vendor.canonical")
    assert copy == canonical and copy is not canonical
    manager.capability_registry.register("text_classification", copy)

    assert manager.get_capability_descriptor("text_classification") is canonical


def test_conflicting_direct_mapping_fails_closed():
    manager = ModelManager()
    canonical = _raw(model_id="vendor.canonical", version="1.0")
    manager.register_descriptor(canonical)
    conflicting = _raw(model_id="vendor.canonical", version="9.9")
    manager.capability_registry.register("text_classification", conflicting)

    assert manager.get_capability_descriptor("text_classification") is None
    assert manager.get_model("vendor.canonical") is canonical
    assert manager.get_model("vendor.canonical").version == "1.0"


def test_orphan_mapping_does_not_affect_other_profile():
    manager = ModelManager()
    canonical = _raw(model_id="vendor.canonical")
    orphan = _raw(model_id="vendor.orphan")
    manager.register_descriptor(canonical)
    manager.register_capability(
        "text_classification", canonical, Profile.EFFICIENT
    )
    manager.capability_registry.register(
        "text_classification", orphan, Profile.BALANCED
    )

    manager.set_profile(Profile.EFFICIENT)
    assert manager.get_capability_descriptor("text_classification") is canonical
    manager.set_profile(Profile.BALANCED)
    assert manager.get_capability_descriptor("text_classification") is None


def test_model_registry_can_share_an_existing_capability_registry():
    capabilities = CapabilityRegistry()
    registry = ModelRegistry(capability_registry=capabilities)
    descriptor = _raw(model_id="vendor.shared")
    registry.register(descriptor)
    capabilities.register("text_classification", descriptor, profile=Profile.EFFICIENT)

    assert registry.list_by_capability(
        "text_classification", Profile.EFFICIENT
    ) == [descriptor]
    assert registry.list_by_capability("text_classification") == []


def test_profile_specific_model_resolution_still_works():
    manager = ModelManager()
    efficient = _raw(model_id="vendor.eff", name="Efficient Text")
    balanced = _raw(model_id="vendor.balanced", name="Balanced Text")
    manager.register_capability(
        "text_classification", efficient, profile=Profile.EFFICIENT
    )
    manager.register_capability(
        "text_classification", balanced, profile=Profile.BALANCED
    )

    manager.set_profile(Profile.EFFICIENT)
    assert manager.get_capability_descriptor("text_classification") is efficient
    assert manager.models_for_capability("text_classification") == [efficient]

    manager.set_profile(Profile.BALANCED)
    assert manager.get_capability_descriptor("text_classification") is balanced
    assert manager.models_for_capability("text_classification") == [balanced]

    manager.set_profile(Profile.EFFICIENT)
    assert manager.get_capability_descriptor("text_classification") is efficient


def test_legacy_profile_map_falls_back_by_unique_name():
    manager = ModelManager()
    # Registered under a model_id, but named like a PROFILE_CAPABILITIES entry.
    descriptor = _raw(
        model_id="vendor.efficient.text",
        name="text_classifier_efficient",
        compatible_runtimes=None,
    )
    manager.register_descriptor(descriptor)
    manager.set_profile(Profile.EFFICIENT)

    # No capability registration exists, so resolution falls back to the
    # descriptor named by PROFILE_CAPABILITIES.
    assert manager.get_capability_descriptor("text_classification") is descriptor


@pytest.mark.parametrize(
    "profile, capability, map_name",
    [
        (Profile.EFFICIENT, "text_classification", "text_classifier_efficient"),
        (Profile.BALANCED, "image_description", "image_description_balanced"),
    ],
)
def test_static_fallback_does_not_shadow_by_identifier(
    profile, capability, map_name
):
    manager = ModelManager()
    # A model whose identifier equals the static map value ...
    by_id = _raw(model_id=map_name, name="Unrelated Model")
    # ... must not shadow the model that actually has that display name.
    by_name = _raw(model_id=f"vendor.{profile.value}", name=map_name)
    manager.register_descriptor(by_id)
    manager.register_descriptor(by_name)
    manager.set_profile(profile)

    resolved = manager.get_capability_descriptor(capability)
    assert resolved is by_name
    assert resolved is not by_id


def test_static_fallback_returns_none_for_ambiguous_display_name():
    manager = ModelManager()
    manager.register_descriptor(
        _raw(model_id="vendor.a", name="text_classifier_efficient")
    )
    manager.register_descriptor(
        _raw(model_id="vendor.b", name="text_classifier_efficient")
    )
    manager.set_profile(Profile.EFFICIENT)

    assert manager.get_capability_descriptor("text_classification") is None


def test_static_fallback_ignores_identifier_only_match():
    manager = ModelManager()
    manager.register_descriptor(
        _raw(model_id="text_classifier_efficient", name="Different Model")
    )
    manager.set_profile(Profile.EFFICIENT)

    assert manager.get_capability_descriptor("text_classification") is None


def test_existing_public_api_remains_functional():
    manager = ModelManager()
    descriptor = _raw(
        model_id="vendor.balanced.text",
        name="text_classifier_balanced",
        compatible_runtimes=None,
    )
    manager.register_descriptor(descriptor)

    runtime = MagicMock()
    runtime.name = "synthetic_runtime"
    runtime.version = "1.0"
    runtime.is_available.return_value = True
    manager.register_runtime(runtime)
    manager.associate_runtime_with_model("synthetic_runtime", "vendor.balanced.text")

    manager.set_profile(Profile.BALANCED)
    assert manager.get_descriptor("vendor.balanced.text") is descriptor
    assert "vendor.balanced.text" in manager.list_descriptors()
    assert manager.get_models_for_runtime("synthetic_runtime") == [
        "vendor.balanced.text"
    ]
    assert manager.get_runtime_for_model("vendor.balanced.text") == "synthetic_runtime"
    assert (
        manager.get_compatible_runtime_for_capability("text_classification")
        == "synthetic_runtime"
    )
    assert manager.can_execute_capability("text_classification") is True
    assert manager.try_execute("text_classification") is descriptor


# ---------------------------------------------------------------------------
# Compatibility queries
# ---------------------------------------------------------------------------

def test_runtime_compatibility_filtering():
    registry = ModelRegistry()
    onnx_model = _raw(model_id="vendor.onnx", compatible_runtimes=("onnx",))
    torch_model = _raw(model_id="vendor.torch", compatible_runtimes=("torch",))
    unknown_model = _raw(model_id="vendor.unknown", compatible_runtimes=None)
    for model in (onnx_model, torch_model, unknown_model):
        registry.register(model)

    assert registry.runtime_compatibility("vendor.onnx", "onnx") is True
    assert registry.runtime_compatibility("vendor.onnx", "torch") is False
    assert registry.runtime_compatibility("vendor.unknown", "onnx") is None
    assert registry.list_by_runtime("onnx") == [onnx_model]
    assert registry.list_by_runtime("missing") == []

    manager = ModelManager()
    manager.register_descriptor(onnx_model)
    runtime = MagicMock()
    runtime.name = "onnx"
    runtime.version = "1.0"
    runtime.is_available.return_value = True
    manager.register_runtime(runtime)

    assert manager.is_model_compatible_with_runtime("vendor.onnx", "onnx") is True
    # Unregistered runtime is unknown, not a hard incompatibility.
    assert (
        manager.is_model_compatible_with_runtime("vendor.onnx", "not_registered")
        is None
    )


def _manager_with_runtime(
    descriptor, runtime_name="synthetic_runtime", available=True
):
    manager = ModelManager()
    manager.register_descriptor(descriptor)
    manager.register_capability(
        "text_classification", descriptor, Profile.EFFICIENT
    )
    runtime = MagicMock()
    runtime.name = runtime_name
    runtime.version = "1.0"
    runtime.is_available.return_value = available
    manager.register_runtime(runtime)
    return manager, runtime


def test_incompatible_metadata_blocks_associated_runtime():
    descriptor = _raw(model_id="vendor.model", compatible_runtimes=("other_runtime",))
    manager, runtime = _manager_with_runtime(descriptor)
    manager.associate_runtime_with_model("synthetic_runtime", descriptor.identifier)

    assert (
        manager.is_model_compatible_with_runtime("vendor.model", "synthetic_runtime")
        is False
    )
    assert manager.get_compatible_runtime_for_capability("text_classification") is None
    assert manager.can_execute_capability("text_classification") is False
    assert manager.try_execute("text_classification") is None
    # Blocked before the availability probe is consulted.
    runtime.is_available.assert_not_called()


def test_compatibility_true_with_unavailable_runtime_still_cannot_execute():
    descriptor = _raw(
        model_id="vendor.model", compatible_runtimes=("synthetic_runtime",)
    )
    manager, _ = _manager_with_runtime(descriptor, available=False)

    assert (
        manager.is_model_compatible_with_runtime("vendor.model", "synthetic_runtime")
        is True
    )
    assert (
        manager.get_compatible_runtime_for_capability("text_classification")
        == "synthetic_runtime"
    )
    assert manager.can_execute_capability("text_classification") is False
    assert manager.try_execute("text_classification") is None


def test_unspecified_metadata_allows_associated_runtime():
    descriptor = _raw(model_id="vendor.model", compatible_runtimes=None)
    manager, _ = _manager_with_runtime(descriptor)
    manager.associate_runtime_with_model("synthetic_runtime", descriptor.identifier)

    assert (
        manager.is_model_compatible_with_runtime("vendor.model", "synthetic_runtime")
        is None
    )
    assert (
        manager.get_compatible_runtime_for_capability("text_classification")
        == "synthetic_runtime"
    )
    assert manager.can_execute_capability("text_classification") is True
    assert manager.try_execute("text_classification") is descriptor


def test_try_execute_does_not_perform_inference():
    descriptor = _raw(model_id="vendor.model", compatible_runtimes=None)
    manager, runtime = _manager_with_runtime(descriptor)
    manager.associate_runtime_with_model("synthetic_runtime", descriptor.identifier)

    assert manager.try_execute("text_classification") is descriptor
    runtime.is_available.assert_called()
    runtime.generate.assert_not_called()
    runtime.embed.assert_not_called()
    runtime.classify.assert_not_called()


def test_availability_probe_failure_fails_closed():
    descriptor = _raw(model_id="vendor.model", compatible_runtimes=None)
    manager, runtime = _manager_with_runtime(descriptor)
    manager.associate_runtime_with_model("synthetic_runtime", descriptor.identifier)
    runtime.is_available.side_effect = RuntimeError("probe failed")

    assert manager.can_execute_capability("text_classification") is False
    assert manager.try_execute("text_classification") is None


def test_is_model_compatible_with_runtime_unknown_model_raises():
    manager, _ = _manager_with_runtime(_raw(model_id="vendor.model"))

    with pytest.raises(KeyError):
        manager.is_model_compatible_with_runtime("missing.model", "synthetic_runtime")


def test_hardware_compatibility_with_sufficient_resources():
    registry = ModelRegistry()
    descriptor = _raw(
        hardware_requirements={
            "min_ram_gb": "16",
            "logical_cores": "8",
            "gpu_memory_gb": "4",
        }
    )
    registry.register(descriptor)

    hardware = _hardware()
    assert registry.hardware_compatibility(descriptor.identifier, hardware) is True
    assert registry.list_compatible_with_hardware(hardware) == [descriptor]


def test_hardware_incompatibility_with_insufficient_resources():
    registry = ModelRegistry()
    descriptor = _raw(hardware_requirements={"min_ram_gb": "16"})
    registry.register(descriptor)

    low_ram = _hardware(total_ram_bytes=8 * GIB)
    assert registry.hardware_compatibility(descriptor.identifier, low_ram) is False
    assert registry.list_compatible_with_hardware(low_ram) == []


@pytest.mark.parametrize(
    "requirements, hardware_overrides, expected",
    [
        ({"min_ram_gb": "16"}, {"total_ram_bytes": 16 * GIB}, True),
        ({"min_ram_gb": "16"}, {"total_ram_bytes": 16 * GIB - 1}, False),
        ({"available_ram_gb": "16"}, {"available_ram_bytes": 16 * GIB}, True),
        ({"available_ram_gb": "16"}, {"available_ram_bytes": 16 * GIB - 1}, False),
        ({"gpu_memory_gb": "4"}, {"gpus": (GpuInfo(memory_bytes=4 * GIB),)}, True),
        ({"gpu_memory_gb": "4"}, {"gpus": (GpuInfo(memory_bytes=4 * GIB - 1),)}, False),
        (
            {"gpu_memory_gb": "512 MB"},
            {"gpus": (GpuInfo(memory_bytes=512 * 1024 ** 2),)},
            True,
        ),
        (
            {"gpu_memory_gb": "512 MB"},
            {"gpus": (GpuInfo(memory_bytes=512 * 1024 ** 2 - 1),)},
            False,
        ),
    ],
)
def test_hardware_compatibility_uses_exact_bytes(
    requirements, hardware_overrides, expected
):
    descriptor = _raw(hardware_requirements=requirements)
    registry = ModelRegistry()
    registry.register(descriptor)

    hardware = _hardware(**hardware_overrides)
    assert registry.hardware_compatibility(descriptor.identifier, hardware) is expected


def test_list_compatible_with_hardware_excludes_just_below_machine():
    descriptor = _raw(hardware_requirements={"min_ram_gb": "16"})
    registry = ModelRegistry()
    registry.register(descriptor)

    just_below = _hardware(total_ram_bytes=16 * GIB - 1)
    assert registry.list_compatible_with_hardware(just_below) == []
    assert registry.list_compatible_with_hardware(
        _hardware(total_ram_bytes=16 * GIB)
    ) == [descriptor]


def test_missing_gpu_remains_unknown_for_vram_requirement():
    descriptor = _raw(hardware_requirements={"gpu_memory_gb": "4"})
    registry = ModelRegistry()
    registry.register(descriptor)

    no_gpu = _hardware(gpus=())
    assert registry.hardware_compatibility(descriptor.identifier, no_gpu) is None
    assert registry.list_compatible_with_hardware(no_gpu) == []


def test_unknown_hardware_never_becomes_compatible():
    registry = ModelRegistry()
    descriptor = _raw(hardware_requirements={"min_ram_gb": "16"})
    registry.register(descriptor)

    unknown_ram = _hardware(total_ram_bytes=None)
    assert registry.hardware_compatibility(descriptor.identifier, unknown_ram) is None
    assert registry.list_compatible_with_hardware(unknown_ram) == []

    # An unknown requirement key is not ignored, and never proves compatibility.
    unsupported_key = _raw(
        model_id="vendor.unknown-requirement",
        hardware_requirements={"npu": "required"},
    )
    registry.register(unsupported_key)
    assert registry.hardware_compatibility(unsupported_key.identifier, _hardware()) is None

    # Available RAM is a separate value and may be unknown while total is known.
    available = _raw(
        model_id="vendor.available-ram",
        hardware_requirements={"available_ram_gb": "4"},
    )
    registry.register(available)
    assert registry.hardware_compatibility(
        available.identifier, _hardware(available_ram_bytes=None)
    ) is None

    manager = ModelManager()
    manager.register_descriptor(descriptor)
    assert manager.is_model_compatible_with_hardware(
        descriptor.identifier, unknown_ram
    ) is None

def test_model_manager_preserves_injected_empty_model_registry():
    model_registry = ModelRegistry()
    manager = ModelManager(model_registry=model_registry)

    assert manager.model_registry is model_registry