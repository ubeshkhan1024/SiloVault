"""Focused tests for the M4.2 runtime contract and runtime unregistration."""

import pytest

from backend.ai import (
    ClassificationRuntime,
    EmbeddingRuntime,
    InferenceRuntime,
    ModelManager,
    TextGenerationRuntime,
)
from backend.ai.interfaces import ModelDescriptor, Profile


class MinimalRuntime:
    """Implements only the core runtime contract."""

    name = "minimal"
    version = "1.0"

    def is_available(self) -> bool:
        return True

    def supported_modalities(self):
        return ["text"]


class TextRuntime(MinimalRuntime):
    name = "text"

    def generate(self, prompt: str, **kwargs) -> str:
        return prompt


class EmbeddingOnlyRuntime(MinimalRuntime):
    name = "embedding-only"

    def embed(self, text):
        return [0.0]


class ClassificationOnlyRuntime(MinimalRuntime):
    name = "classification-only"

    def classify(self, image=None, text=None):
        return {}


def test_minimal_runtime_satisfies_core_protocol_only():
    runtime = MinimalRuntime()

    assert isinstance(runtime, InferenceRuntime)
    assert not isinstance(runtime, TextGenerationRuntime)
    assert not isinstance(runtime, EmbeddingRuntime)
    assert not isinstance(runtime, ClassificationRuntime)


@pytest.mark.parametrize(
    "runtime, supported_protocol, unsupported_protocols",
    [
        (
            TextRuntime(),
            TextGenerationRuntime,
            (EmbeddingRuntime, ClassificationRuntime),
        ),
        (
            EmbeddingOnlyRuntime(),
            EmbeddingRuntime,
            (TextGenerationRuntime, ClassificationRuntime),
        ),
        (
            ClassificationOnlyRuntime(),
            ClassificationRuntime,
            (TextGenerationRuntime, EmbeddingRuntime),
        ),
    ],
)
def test_capability_specific_protocol_conformance(
    runtime, supported_protocol, unsupported_protocols
):
    assert isinstance(runtime, InferenceRuntime)
    assert isinstance(runtime, supported_protocol)
    for protocol in unsupported_protocols:
        assert not isinstance(runtime, protocol)


def _manager_with_associated_runtime():
    manager = ModelManager()
    descriptor = ModelDescriptor(
        name="Test Model",
        version="1.0",
        modalities=("text",),
        model_id="vendor.model",
    )
    manager.register_descriptor(descriptor)
    manager.register_capability(
        "text_classification", descriptor, Profile.EFFICIENT
    )
    runtime = MinimalRuntime()
    manager.register_runtime(runtime)
    manager.associate_runtime_with_model("minimal", descriptor.identifier)
    return manager, descriptor


def test_unregister_runtime_removes_runtime_and_associations():
    manager, _ = _manager_with_associated_runtime()
    assert manager.get_models_for_runtime("minimal") == ["vendor.model"]
    assert manager.can_execute_capability("text_classification") is True

    assert manager.unregister_runtime("minimal") is True

    assert manager.list_runtimes() == []
    assert manager.get_models_for_runtime("minimal") == []
    assert manager.get_runtime_for_model("vendor.model") is None
    assert manager.can_execute_capability("text_classification") is False
    assert manager.try_execute("text_classification") is None
    with pytest.raises(KeyError):
        manager.get_runtime("minimal")


def test_unregister_unknown_runtime_is_safe_and_isolated():
    manager, _ = _manager_with_associated_runtime()
    other = MinimalRuntime()
    other.name = "other"
    manager.register_runtime(other)
    manager.associate_runtime_with_model("other", "vendor.model")

    assert manager.unregister_runtime("missing") is False

    assert sorted(manager.list_runtimes()) == ["minimal", "other"]
    assert manager.get_models_for_runtime("minimal") == ["vendor.model"]
    assert manager.get_runtime_for_model("vendor.model") is not None

    # Unregistering one runtime does not affect another registered runtime.
    assert manager.unregister_runtime("minimal") is True
    assert manager.list_runtimes() == ["other"]
    assert manager.get_runtime_for_model("vendor.model") == "other"
    assert manager.unregister_runtime("minimal") is False


def test_unregister_runtime_keeps_descriptors_and_capabilities():
    manager, descriptor = _manager_with_associated_runtime()

    assert manager.unregister_runtime("minimal") is True

    assert manager.get_model("vendor.model") is descriptor
    assert manager.model_registry.find("vendor.model") is descriptor
    assert manager.capability_registry.has_capability(
        "text_classification", Profile.EFFICIENT
    )
    assert manager.get_capability_descriptor("text_classification") is descriptor
