"""Focused test for M4.2 AI foundation interfaces."""

from pathlib import Path

from backend.ai.interfaces import (
    InferenceRuntime,
    ModelDescriptor,
    CapabilityRegistry,
    Profile,
    HardwareDetector,
    ModelManager,
    PROFILE_CAPABILITIES,
)


def test_model_descriptor_has_metadata():
    """Test that ModelDescriptor includes all required metadata fields."""
    desc = ModelDescriptor(
        name="text_classifier_efficient",
        version="1.0",
        modalities=("text",),
    )
    assert desc.name == "text_classifier_efficient"
    assert desc.version == "1.0"
    assert desc.modalities == ("text",)  # verify modalities field exists
    # Verify optional metadata fields exist with defaults
    assert desc.model_id is None
    assert desc.framework is None
    assert desc.supported_formats is None
    assert desc.model_size_bytes is None
    assert desc.checksum is None
    assert desc.licensing is None
    assert desc.hardware_requirements is None
    assert desc.compatible_runtimes is None
    assert desc.download_url is None
    # Installation state is not part of static model metadata.
    assert not hasattr(desc, "is_installed")


def test_capability_registry():
    """Test CapabilityRegistry basic operations."""
    registry = CapabilityRegistry()
    
    # Test register and get
    desc = ModelDescriptor(name="test", version="1.0", modalities=("text",))
    registry.register("test_capability", desc)
    retrieved = registry.get("test_capability")
    assert retrieved is desc
    
    # Test listing
    caps = registry.list_capabilities()
    assert "test_capability" in caps
    
    # Test missing capability
    try:
        registry.get("nonexistent")
        assert False, "Should have raised KeyError"
    except KeyError:
        pass


def test_profile_enum():
    """Test Profile enum values."""
    assert Profile.EFFICIENT.value == "efficient"
    assert Profile.BALANCED.value == "balanced"


def test_hardware_detector():
    """Test HardwareDetector returns the structured hardware representation."""
    from backend.ai.interfaces import HardwareInfo

    detector = HardwareDetector()
    info = detector.detect()
    assert isinstance(info, HardwareInfo)
    data = info.to_dict()
    for key in ("os", "cpu", "logical_cores", "ram_gb", "gpu", "npu_available"):
        assert key in data
    # NPU state is either unknown or positively detected, never fabricated.
    assert info.npu_available in (None, True)


def test_model_manager_initialization():
    """Test ModelManager initializes with _runtime_model_map."""
    manager = ModelManager()
    assert hasattr(manager, "_runtime_model_map")
    assert manager._runtime_model_map == {}
    assert manager.get_profile() == Profile.EFFICIENT


def test_model_manager_runtime_association():
    """Test ModelManager can associate runtimes with models."""
    from unittest.mock import MagicMock
    
    manager = ModelManager()
    
    # Create a mock runtime
    mock_runtime = MagicMock()
    mock_runtime.name = "test_runtime"
    mock_runtime.version = "1.0"
    mock_runtime.is_available.return_value = True
    
    # Register the runtime
    manager.register_runtime(mock_runtime)
    
    # Create and register a model descriptor
    desc = ModelDescriptor(name="text_classifier_efficient", version="1.0", modalities=("text",))
    manager.register_descriptor(desc)
    
    # Associate runtime with model
    manager.associate_runtime_with_model("test_runtime", "text_classifier_efficient")
    
    # Verify association
    models = manager.get_models_for_runtime("test_runtime")
    assert "text_classifier_efficient" in models
    
    # Verify get_runtime_for_model
    runtime_name = manager.get_runtime_for_model("text_classifier_efficient")
    assert runtime_name == "test_runtime"


def test_model_manager_can_execute_capability():
    """Test ModelManager.can_execute_capability with available runtime."""
    from unittest.mock import MagicMock
    
    manager = ModelManager()
    
    # Create a mock runtime that is available
    mock_runtime = MagicMock()
    mock_runtime.name = "test_runtime"
    mock_runtime.version = "1.0"
    mock_runtime.is_available.return_value = True
    
    # Register the runtime
    manager.register_runtime(mock_runtime)
    
    # Create and register a model descriptor
    desc = ModelDescriptor(name="text_classifier_balanced", version="1.0", modalities=("text",))
    manager.register_descriptor(desc)
    
    # Associate runtime with model
    manager.associate_runtime_with_model("test_runtime", "text_classifier_balanced")
    
    # Set PRO profile (which has text_classification capability)
    manager.set_profile(Profile.BALANCED)
    
    # Check if capability can be executed
    can_execute = manager.can_execute_capability("text_classification")
    assert can_execute is True


def test_model_manager_cannot_execute_missing_runtime():
    """Test ModelManager.can_execute_capability returns False when runtime unavailable."""
    from unittest.mock import MagicMock
    
    manager = ModelManager()
    
    # Create a mock runtime that is NOT available
    mock_runtime = MagicMock()
    mock_runtime.name = "test_runtime"
    mock_runtime.version = "1.0"
    mock_runtime.is_available.return_value = False
    
    # Register the runtime
    manager.register_runtime(mock_runtime)
    
    # Create and register a model descriptor
    desc = ModelDescriptor(name="text_classifier_balanced", version="1.0", modalities=("text",))
    manager.register_descriptor(desc)
    
    # Associate runtime with model
    manager.associate_runtime_with_model("test_runtime", "text_classifier_balanced")
    
    # Set PRO profile
    manager.set_profile(Profile.BALANCED)
    
    # Check if capability can be executed (should be False because runtime not available)
    can_execute = manager.can_execute_capability("text_classification")
    assert can_execute is False


def test_model_manager_try_execute():
    """Test ModelManager.try_execute returns descriptor when possible."""
    from unittest.mock import MagicMock
    
    manager = ModelManager()
    
    # Create a mock runtime that is available
    mock_runtime = MagicMock()
    mock_runtime.name = "test_runtime"
    mock_runtime.version = "1.0"
    mock_runtime.is_available.return_value = True
    
    # Register the runtime
    manager.register_runtime(mock_runtime)
    
    # Create and register a model descriptor
    desc = ModelDescriptor(name="text_classifier_efficient", version="1.0", modalities=("text",))
    manager.register_descriptor(desc)
    
    # Associate runtime with model
    manager.associate_runtime_with_model("test_runtime", "text_classifier_efficient")
    
    # Set profile
    manager.set_profile(Profile.EFFICIENT)
    
    # Try to execute
    result = manager.try_execute("text_classification")
    # Should return the descriptor (or None if capability not in profile map)
    assert result is not None


def test_model_manager_try_execute_no_runtime():
    """Test ModelManager.try_execute returns None when no runtime associated."""
    from unittest.mock import MagicMock
    
    manager = ModelManager()
    
    # Create a model descriptor WITHOUT associating a runtime
    desc = ModelDescriptor(name="orphan_model", version="1.0", modalities=("text",))
    manager.register_descriptor(desc)
    
    # Set profile
    manager.set_profile(Profile.EFFICIENT)
    
    # Try to execute - should return None because no runtime associated
    result = manager.try_execute("text_classification")
    assert result is None


def test_model_manager_graceful_fallback():
    """Test ModelManager handles missing runtime gracefully."""
    from unittest.mock import MagicMock
    
    manager = ModelManager()
    
    # Register a runtime but don't associate it with any model
    mock_runtime = MagicMock()
    mock_runtime.name = "unassociated_runtime"
    mock_runtime.version = "1.0"
    mock_runtime.is_available.return_value = True
    
    manager.register_runtime(mock_runtime)
    
    # Create and register a model descriptor
    desc = ModelDescriptor(name="some_model", version="1.0", modalities=("text",))
    manager.register_descriptor(desc)
    
    # Set profile
    manager.set_profile(Profile.EFFICIENT)
    
    # Try to execute - should return None because no association
    result = manager.try_execute("text_classification")
    assert result is None




def test_model_descriptor_new_metadata():
    """Test ModelDescriptor with runtime compatibility and download URL."""
    desc = ModelDescriptor(
        name="custom_model",
        version="2.0",
        modalities=("text", "vision"),
        local_path=Path("/path/to/model"),
        compatible_runtimes=("test_runtime", "onnx"),
        download_url="https://models.silovault.local/custom_model.tar.gz",
    )
    assert desc.compatible_runtimes == ("test_runtime", "onnx")
    assert desc.download_url == "https://models.silovault.local/custom_model.tar.gz"
    assert not hasattr(desc, "is_installed")


def test_capability_registry_duplicate_protection():
    """Test that CapabilityRegistry prevents duplicate registrations."""
    registry = CapabilityRegistry()
    desc = ModelDescriptor(name="model_a", version="1.0", modalities=("text",))
    registry.register("text_classification", desc)
    try:
        registry.register("text_classification", desc)
        assert False, "Should have raised ValueError on duplicate capability"
    except ValueError as exc:
        assert "already registered" in str(exc)


def test_model_manager_capability_registry_integration():
    """Test ModelManager integrates CapabilityRegistry cleanly."""
    manager = ModelManager()
    assert isinstance(manager.capability_registry, CapabilityRegistry)

    desc = ModelDescriptor(
        name="text_classifier_balanced",
        version="1.0",
        modalities=("text",),
        compatible_runtimes=("test_runtime",),
    )
    manager.register_capability("text_classification", desc)
    assert manager.capability_registry.has_capability("text_classification")

    # In EFFICIENT profile, text_classification is allowed
    manager.set_profile(Profile.EFFICIENT)
    resolved = manager.get_capability_descriptor("text_classification")
    assert resolved is desc

    # Register an image capability not permitted in EFFICIENT profile
    img_desc = ModelDescriptor(name="img_desc", version="1.0", modalities=("vision",))
    manager.register_capability("image_description", img_desc)
    assert manager.get_capability_descriptor("image_description") is None

    # Switch to BALANCED profile where image_description is allowed
    manager.set_profile(Profile.BALANCED)
    assert manager.get_capability_descriptor("image_description") is img_desc


def test_model_manager_compatible_runtimes_fallback():
    """Test ModelManager uses compatible_runtimes metadata when explicit mapping absent."""
    from unittest.mock import MagicMock

    manager = ModelManager()
    mock_runtime = MagicMock()
    mock_runtime.name = "declared_runtime"
    mock_runtime.version = "1.0"
    mock_runtime.is_available.return_value = True
    manager.register_runtime(mock_runtime)

    desc = ModelDescriptor(
        name="text_classifier_efficient",
        version="1.0",
        modalities=("text",),
        compatible_runtimes=("declared_runtime",),
    )
    manager.register_descriptor(desc)
    manager.set_profile(Profile.EFFICIENT)

    assert manager.can_execute_capability("text_classification") is True


def test_model_descriptor_has_no_installation_state():
    """Installation state must stay separate from static model metadata."""
    untouched = ModelDescriptor(name="m", version="1.0", modalities=("text",))
    assert not hasattr(untouched, "is_installed")

    with_path = ModelDescriptor(
        name="m2",
        version="1.0",
        modalities=("text",),
        local_path=Path("/path/to/model"),
    )
    assert with_path.local_path == Path("/path/to/model")
    assert not hasattr(with_path, "is_installed")


def test_capability_registry_profile_scoped_resolution():
    """Test that one capability resolves to different descriptors per profile."""
    registry = CapabilityRegistry()
    efficient = ModelDescriptor(
        name="text_classifier_efficient", version="1.0", modalities=("text",)
    )
    balanced = ModelDescriptor(
        name="text_classifier_balanced", version="1.0", modalities=("text",)
    )

    registry.register("text_classification", efficient, profile=Profile.EFFICIENT)
    registry.register("text_classification", balanced, profile=Profile.BALANCED)

    assert registry.get("text_classification", profile=Profile.EFFICIENT) is efficient
    assert registry.get("text_classification", profile=Profile.BALANCED) is balanced
    assert registry.list_capabilities(profile=Profile.EFFICIENT) == ["text_classification"]

    # The same capability in another profile is not a duplicate, but
    # re-registering it in the same profile is still rejected.
    try:
        registry.register("text_classification", balanced, profile=Profile.EFFICIENT)
        assert False, "Should have raised ValueError on duplicate profile registration"
    except ValueError as exc:
        assert "already registered" in str(exc)


def test_model_manager_selects_model_per_profile():
    """Test both profiles select the correct model for the same capability."""
    from unittest.mock import MagicMock

    manager = ModelManager()
    efficient = ModelDescriptor(
        name="text_classifier_efficient", version="1.0", modalities=("text",)
    )
    balanced = ModelDescriptor(
        name="text_classifier_balanced", version="1.0", modalities=("text",)
    )
    manager.register_capability("text_classification", efficient, profile=Profile.EFFICIENT)
    manager.register_capability("text_classification", balanced, profile=Profile.BALANCED)

    efficient_runtime = MagicMock()
    efficient_runtime.name = "efficient_runtime"
    efficient_runtime.version = "1.0"
    efficient_runtime.is_available.return_value = True
    balanced_runtime = MagicMock()
    balanced_runtime.name = "balanced_runtime"
    balanced_runtime.version = "1.0"
    balanced_runtime.is_available.return_value = True
    manager.register_runtime(efficient_runtime)
    manager.register_runtime(balanced_runtime)
    manager.associate_runtime_with_model("efficient_runtime", "text_classifier_efficient")
    manager.associate_runtime_with_model("balanced_runtime", "text_classifier_balanced")

    manager.set_profile(Profile.EFFICIENT)
    assert manager.get_capability_descriptor("text_classification") is efficient
    assert manager.get_compatible_runtime_for_capability("text_classification") == "efficient_runtime"
    assert manager.try_execute("text_classification") is efficient

    manager.set_profile(Profile.BALANCED)
    assert manager.get_capability_descriptor("text_classification") is balanced
    assert manager.get_compatible_runtime_for_capability("text_classification") == "balanced_runtime"
    assert manager.try_execute("text_classification") is balanced

    # Switching back must resolve the efficient model again.
    manager.set_profile(Profile.EFFICIENT)
    assert manager.get_capability_descriptor("text_classification") is efficient


def test_model_manager_profile_restriction_and_fallback():
    """Test profile restrictions and graceful fallback are preserved."""
    manager = ModelManager()
    efficient = ModelDescriptor(
        name="text_classifier_efficient", version="1.0", modalities=("text",)
    )
    balanced_image = ModelDescriptor(
        name="image_description_balanced", version="1.0", modalities=("vision",)
    )
    manager.register_capability("text_classification", efficient, profile=Profile.EFFICIENT)
    manager.register_capability("image_description", balanced_image, profile=Profile.BALANCED)

    manager.set_profile(Profile.EFFICIENT)
    # Allowed only in BALANCED, so it is not exposed in EFFICIENT.
    assert manager.get_capability_descriptor("image_description") is None
    # No descriptor available -> graceful None rather than an exception.
    assert manager.get_capability_descriptor("audio_classification") is None
    assert manager.try_execute("audio_classification") is None

    manager.set_profile(Profile.BALANCED)
    assert manager.get_capability_descriptor("image_description") is balanced_image
    # No BALANCED text classifier is registered, so it falls back to None.
    assert manager.get_capability_descriptor("text_classification") is None


if __name__ == "__main__":
    import sys
    
    tests = [
        test_model_descriptor_has_metadata,
        test_capability_registry,
        test_profile_enum,
        test_hardware_detector,
        test_model_manager_initialization,
        test_model_manager_runtime_association,
        test_model_manager_can_execute_capability,
        test_model_manager_cannot_execute_missing_runtime,
        test_model_manager_try_execute,
        test_model_manager_try_execute_no_runtime,
        test_model_manager_graceful_fallback,
        test_model_descriptor_new_metadata,
        test_capability_registry_duplicate_protection,
        test_model_manager_capability_registry_integration,
        test_model_manager_compatible_runtimes_fallback,
        test_model_descriptor_has_no_installation_state,
        test_capability_registry_profile_scoped_resolution,
        test_model_manager_selects_model_per_profile,
        test_model_manager_profile_restriction_and_fallback,
    ]
    
    passed = 0
    failed = 0
    skipped = 0
    
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL: {test.__name__}: {e}")
            failed += 1
    
    print(f"\nResults: {passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(0 if failed == 0 else 1)
