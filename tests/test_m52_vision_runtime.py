# Tests for SiloVault M5.2 Vision Runtime Adapter Foundation.

import pytest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from backend.ai.interfaces import ModelDescriptor
from backend.ai.vision_runtime import (
    VisionRuntime,
    VisionRuntimeManager,
    RuntimeState,
    RuntimeStatus,
    RuntimeCapability,
    RuntimeInitializationError,
    ModelNotInstalledError,
    ModelIncompatibleError,
    RuntimeNotReadyError,
    ExecutionError,
    CancellationError,
    get_vision_execution_service,
)


class MockVisionRuntime:
    """Mock vision runtime implementing VisionRuntime protocol."""

    def __init__(self, should_fail_init: bool = False, should_fail_exec: bool = False):
        self._should_fail_init = should_fail_init
        self._should_fail_exec = should_fail_exec
        self._initialized = False
        self._loaded_model: Optional[ModelDescriptor] = None
        self._state = RuntimeState.SHUTDOWN
        self._cpu_opt = True

    @property
    def name(self) -> str:
        return "mock_florence2"

    @property
    def version(self) -> str:
        return "1.0.0"

    def initialize(self, profile: Optional[str] = None) -> None:
        if self._should_fail_init:
            self._state = RuntimeState.FAILED
            raise RuntimeError("Mock initialization failed")
        self._initialized = True
        self._state = RuntimeState.READY

    def is_initialized(self) -> bool:
        return self._initialized

    def load_model(self, descriptor: ModelDescriptor) -> None:
        if not self._initialized:
            raise RuntimeNotReadyError("Not initialized")
        self._state = RuntimeState.LOADING
        self._loaded_model = descriptor
        self._state = RuntimeState.READY

    def is_model_loaded(self) -> bool:
        return self._loaded_model is not None

    def get_loaded_model(self) -> Optional[ModelDescriptor]:
        return self._loaded_model

    def execute_image_description(
        self, image_data: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        if not self._initialized or self._loaded_model is None:
            raise RuntimeNotReadyError("Runtime not ready or model not loaded")
        if self._should_fail_exec:
            raise ExecutionError("Inference failed")
        self._state = RuntimeState.RUNNING
        result = {
            "description": "A test image showing a cat.",
            "tags": ("cat", "animal"),
            "model": self._loaded_model.identifier,
            "confidence": 0.95,
        }
        self._state = RuntimeState.READY
        return result

    def cancel_execution(self) -> None:
        self._state = RuntimeState.CANCELLED

    def unload_model(self) -> None:
        self._loaded_model = None

    def shutdown(self) -> None:
        self._initialized = False
        self._loaded_model = None
        self._state = RuntimeState.SHUTDOWN

    def get_status(self) -> RuntimeStatus:
        return RuntimeStatus(
            state=self._state,
            model_id=self._loaded_model.identifier if self._loaded_model else None,
            capabilities=(RuntimeCapability.IMAGE_DESCRIPTION,),
        )

    def set_cpu_optimization(self, enabled: bool) -> None:
        self._cpu_opt = enabled


class MockModelManager:
    """Mock model manager for testing VisionRuntimeManager."""

    def __init__(self, models: Optional[list[ModelDescriptor]] = None, installed_map: Optional[dict[str, bool]] = None):
        self._models = models or []
        self._installed_map = installed_map or {}

    def get_model(self, identifier: str) -> Optional[ModelDescriptor]:
        for m in self._models:
            if m.identifier == identifier:
                return m
        return None

    def get_models(self) -> list[ModelDescriptor]:
        return self._models

    def list_models(self) -> list[ModelDescriptor]:
        return self._models

    def get_model_count(self) -> int:
        return len(self._models)

    def is_model_installed(self, descriptor: ModelDescriptor) -> bool:
        return self._installed_map.get(descriptor.identifier, False)


def test_runtime_initialization():
    """Test successful runtime initialization and status reporting."""
    manager = MockModelManager()
    vr_manager = VisionRuntimeManager(manager)

    assert not vr_manager.is_initialized
    runtime = vr_manager.initialize_runtime(MockVisionRuntime)
    assert vr_manager.is_initialized
    assert runtime.name == "mock_florence2"

    status = vr_manager.get_status()
    assert status.state == RuntimeState.READY
    assert RuntimeCapability.IMAGE_DESCRIPTION in status.capabilities

    vr_manager.shutdown()
    assert not vr_manager.is_initialized
    assert vr_manager.get_status().state == RuntimeState.SHUTDOWN


def test_missing_dependency_handling():
    """Test handling when runtime initialization fails (e.g. missing torch/transformers)."""
    manager = MockModelManager()
    vr_manager = VisionRuntimeManager(manager)

    class FailingRuntime(MockVisionRuntime):
        def __init__(self):
            super().__init__(should_fail_init=True)

    with pytest.raises(RuntimeInitializationError):
        vr_manager.initialize_runtime(FailingRuntime)

    assert not vr_manager.is_initialized


def test_model_not_installed():
    """Test loading a model that is not installed raises ModelNotInstalledError."""
    desc = ModelDescriptor(
        name="Florence-2 Base",
        version="1.0",
        modalities=("vision",),
        model_id="microsoft.florence-2-base",
    )
    manager = MockModelManager(models=[desc], installed_map={"microsoft.florence-2-base": False})
    vr_manager = VisionRuntimeManager(manager)
    vr_manager.initialize_runtime(MockVisionRuntime)

    with pytest.raises(ModelNotInstalledError):
        vr_manager.load_model(desc)


def test_model_incompatible_with_runtime():
    """Test loading an incompatible model raises ModelIncompatibleError."""
    vision_desc = ModelDescriptor(
        name="Florence-2 Base",
        version="1.0",
        modalities=("vision",),
        model_id="microsoft.florence-2-base",
    )
    text_desc = ModelDescriptor(
        name="Text Classifier",
        version="1.0",
        modalities=("text",),
        model_id="text.classifier",
    )
    manager = MockModelManager(
        models=[vision_desc, text_desc],
        installed_map={"microsoft.florence-2-base": True, "text.classifier": True}
    )
    vr_manager = VisionRuntimeManager(manager)
    vr_manager.initialize_runtime(MockVisionRuntime)

    with pytest.raises(ModelIncompatibleError):
        vr_manager.load_model(text_desc)


def test_load_and_unload_lifecycle():
    """Test full load, execute, unload lifecycle."""
    desc = ModelDescriptor(
        name="Florence-2 Base",
        version="1.0",
        modalities=("vision",),
        model_id="microsoft.florence-2-base",
    )
    manager = MockModelManager(models=[desc], installed_map={"microsoft.florence-2-base": True})
    vr_manager = VisionRuntimeManager(manager)
    runtime = vr_manager.initialize_runtime(MockVisionRuntime)

    assert not runtime.is_model_loaded()
    vr_manager.load_model(desc)
    assert runtime.is_model_loaded()
    assert runtime.get_loaded_model() == desc

    result = vr_manager.execute_image_description(b"fake-image-bytes", mime_type="image/jpeg")
    assert "description" in result
    assert result["model"] == "microsoft.florence-2-base"

    vr_manager.unload_model()
    assert not runtime.is_model_loaded()


def test_cancellation():
    """Test execution cancellation."""
    desc = ModelDescriptor(
        name="Florence-2 Base",
        version="1.0",
        modalities=("vision",),
        model_id="microsoft.florence-2-base",
    )
    manager = MockModelManager(models=[desc], installed_map={"microsoft.florence-2-base": True})
    vr_manager = VisionRuntimeManager(manager)
    vr_manager.initialize_runtime(MockVisionRuntime)
    vr_manager.load_model(desc)

    vr_manager.cancel()
    status = vr_manager.get_status()
    assert vr_manager.runtime._state == RuntimeState.CANCELLED


def test_inference_error_propagation():
    """Test that runtime execution errors propagate correctly."""
    desc = ModelDescriptor(
        name="Florence-2 Base",
        version="1.0",
        modalities=("vision",),
        model_id="microsoft.florence-2-base",
    )
    manager = MockModelManager(models=[desc], installed_map={"microsoft.florence-2-base": True})
    vr_manager = VisionRuntimeManager(manager)
    vr_manager.initialize_runtime(MockVisionRuntime)
    vr_manager.load_model(desc)

    class FailingExecRuntime(MockVisionRuntime):
        def __init__(self):
            super().__init__(should_fail_exec=True)

    vr_manager._runtime = FailingExecRuntime()
    vr_manager._runtime.initialize()
    vr_manager._runtime.load_model(desc)

    with pytest.raises(ExecutionError):
        vr_manager.execute_image_description(b"bytes")


def test_task_integration_bridge():
    """Test vision execution service bridge integration with AiExecutionService pattern."""
    desc = ModelDescriptor(
        name="Florence-2 Base",
        version="1.0",
        modalities=("vision",),
        model_id="microsoft.florence-2-base",
    )
    manager = MockModelManager(models=[desc], installed_map={"microsoft.florence-2-base": True})
    bridge = get_vision_execution_service(manager)
    
    bridge._vision_manager.initialize_runtime(MockVisionRuntime)
    bridge.load_model(desc)

    result = bridge.describe_image(b"fake-bytes", mime_type="image/png")
    assert result["description"] is not None
    assert result["tags"] == ("cat", "animal")

    status = bridge.get_status()
    assert status.state == RuntimeState.READY

    bridge.unload_model()
    bridge._vision_manager.shutdown()


def test_get_compatible_models_uses_real_model_manager_api():
    """Verify _get_compatible_models iterates via ModelManager.list_models() (not get_models).

    This exercises the real ModelManager public API, ensuring the compatibility scan
    does not depend on any private or aliased methods that may be absent from the
    production implementation.
    """
    from backend.ai.interfaces import ModelManager, ModelRegistry

    # Build a real ModelManager with a small catalogue containing both
    # vision and non-vision descriptors so the modality filter is exercised.
    registry = ModelRegistry()
    vision_desc = ModelDescriptor(
        name="VisionModel", version="1.0", modalities=("vision",),
        model_id="vision/model-a", compatible_runtimes=("mock-runtime",),
    )
    text_desc = ModelDescriptor(
        name="TextModel", version="1.0", modalities=("text",),
        model_id="text/model-b", compatible_runtimes=("mock-runtime",),
    )
    registry.register(vision_desc)
    registry.register(text_desc)
    manager = ModelManager(model_registry=registry)
    vm = VisionRuntimeManager(manager)

    # A minimal runtime object that passes the execute_image_description check.
    class StubRuntime:
        @property
        def name(self):
            return "stub"
        @property
        def version(self):
            return "0.1"
        def initialize(self, profile=None):
            pass
        def is_initialized(self):
            return True
        def load_model(self, descriptor):
            pass
        def is_model_loaded(self):
            return False
        def get_loaded_model(self):
            return None
        def execute_image_description(self, image_data, mime_type=None, **kwargs):
            return {}
        def cancel_execution(self):
            pass
        def unload_model(self):
            pass
        def shutdown(self):
            pass
        def get_status(self):
            from backend.ai.vision_runtime import RuntimeStatus, RuntimeState
            return RuntimeStatus(state=RuntimeState.READY)
        def set_cpu_optimization(self, enabled):
            pass

    stub = StubRuntime()
    compatible = vm._get_compatible_models(stub)

    # Only the vision-modality model should be included.
    assert compatible == {"vision/model-a"}, (
        f"Expected only the vision descriptor; got {compatible!r}"
    )
    # Confirm non-vision models are excluded.
    assert "text/model-b" not in compatible
