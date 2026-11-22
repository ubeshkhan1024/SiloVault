"""Focused tests for the synchronous AI execution bridge."""

import math

import pytest

from backend.ai import (
    AiExecutionService,
    ExecutionFailedError,
    ExecutionUnavailableError,
    IMAGE_DESCRIPTION_CAPABILITY,
    ImageDescriptionResult,
    InvalidExecutionInputError,
    InvalidExecutionResultError,
    ModelDescriptor,
    ModelManager,
    Profile,
)


_DEFAULT_SCORES = object()


class FakeClassificationRuntime:
    name = "fake-classifier"
    version = "1.0"

    def __init__(
        self,
        *,
        name="fake-classifier",
        available=True,
        modalities=("text",),
        scores=_DEFAULT_SCORES,
        inference_error=None,
        availability_error=None,
    ):
        self.name = name
        self.available = available
        self.modalities = modalities
        self.scores = (
            {"positive": 0.9, "negative": 0.1}
            if scores is _DEFAULT_SCORES
            else scores
        )
        self.inference_error = inference_error
        self.availability_error = availability_error
        self.classify_calls = []

    def is_available(self):
        if self.availability_error is not None:
            raise self.availability_error
        return self.available

    def supported_modalities(self):
        if self.modalities is None:
            raise RuntimeError("modality probe failed")
        return list(self.modalities)

    def classify(self, image=None, text=None):
        self.classify_calls.append({"image": image, "text": text})
        if self.inference_error is not None:
            raise self.inference_error
        return self.scores


class NonClassificationRuntime:
    name = "text-only"
    version = "1.0"

    def is_available(self):
        return True

    def supported_modalities(self):
        return ["text"]


def _descriptor(model_id="vendor.classifier", **overrides):
    values = dict(
        name="Fake Classifier",
        version="1.0",
        modalities=("text",),
        model_id=model_id,
    )
    values.update(overrides)
    return ModelDescriptor(**values)


def _manager(runtime=None, descriptor=None, profile=Profile.EFFICIENT):
    manager = ModelManager()
    descriptor = descriptor if descriptor is not None else _descriptor()
    manager.register_capability("text_classification", descriptor, profile)
    if runtime is not None:
        manager.register_runtime(runtime)
        manager.associate_runtime_with_model(
            runtime.name, descriptor.identifier
        )
    return manager


def test_successful_text_classification():
    runtime = FakeClassificationRuntime()
    service = AiExecutionService(_manager(runtime))

    result = service.classify_text("This is great")

    assert result == {"positive": 0.9, "negative": 0.1}
    assert runtime.classify_calls == [{"image": None, "text": "This is great"}]


def test_current_profile_selects_its_own_descriptor_and_runtime():
    efficient = FakeClassificationRuntime(
        name="efficient-runtime", scores={"efficient": 1.0}
    )
    balanced = FakeClassificationRuntime(
        name="balanced-runtime", scores={"balanced": 1.0}
    )
    manager = ModelManager()
    efficient_descriptor = _descriptor(
        "vendor.classifier.efficient", name="Efficient Classifier"
    )
    balanced_descriptor = _descriptor(
        "vendor.classifier.balanced", name="Balanced Classifier"
    )
    manager.register_capability(
        "text_classification", efficient_descriptor, Profile.EFFICIENT
    )
    manager.register_capability(
        "text_classification", balanced_descriptor, Profile.BALANCED
    )
    manager.register_runtime(efficient)
    manager.register_runtime(balanced)
    manager.associate_runtime_with_model(
        efficient.name, efficient_descriptor.identifier
    )
    manager.associate_runtime_with_model(
        balanced.name, balanced_descriptor.identifier
    )
    service = AiExecutionService(manager)

    assert manager.get_profile() is Profile.EFFICIENT
    assert service.classify_text("review") == {"efficient": 1.0}
    assert efficient.classify_calls
    assert balanced.classify_calls == []

    manager.set_profile(Profile.BALANCED)

    assert service.classify_text("review") == {"balanced": 1.0}
    assert len(efficient.classify_calls) == 1
    assert len(balanced.classify_calls) == 1


def test_missing_capability_descriptor_fails_closed():
    service = AiExecutionService(ModelManager())

    with pytest.raises(ExecutionUnavailableError, match="no model descriptor"):
        service.classify_text("text")


def test_missing_compatible_runtime_fails_closed():
    service = AiExecutionService(_manager(runtime=None))

    with pytest.raises(ExecutionUnavailableError, match="no compatible runtime"):
        service.classify_text("text")


def test_unavailable_runtime_fails_closed_without_inference():
    runtime = FakeClassificationRuntime(available=False)
    service = AiExecutionService(_manager(runtime))

    with pytest.raises(ExecutionUnavailableError, match="unavailable"):
        service.classify_text("text")
    assert runtime.classify_calls == []


def test_availability_probe_exception_is_treated_as_unavailable():
    runtime = FakeClassificationRuntime(
        availability_error=RuntimeError("probe exploded")
    )
    service = AiExecutionService(_manager(runtime))

    with pytest.raises(
        ExecutionUnavailableError, match="availability could not be determined"
    ):
        service.classify_text("text")
    assert runtime.classify_calls == []


def test_runtime_without_classification_method_fails_closed():
    runtime = NonClassificationRuntime()
    manager = _manager(runtime=runtime)

    with pytest.raises(ExecutionUnavailableError, match="does not implement"):
        AiExecutionService(manager).classify_text("text")


def test_descriptor_without_text_modality_fails_closed():
    descriptor = _descriptor(modalities=("image",))
    runtime = FakeClassificationRuntime()
    service = AiExecutionService(_manager(runtime, descriptor=descriptor))

    with pytest.raises(
        ExecutionUnavailableError, match="descriptor does not support text"
    ):
        service.classify_text("text")
    assert runtime.classify_calls == []


def test_runtime_without_text_modality_fails_closed():
    runtime = FakeClassificationRuntime(modalities=("image",))
    service = AiExecutionService(_manager(runtime))

    with pytest.raises(
        ExecutionUnavailableError, match="runtime does not support text"
    ):
        service.classify_text("text")
    assert runtime.classify_calls == []


def test_runtime_modality_probe_exception_fails_closed():
    runtime = FakeClassificationRuntime(modalities=None)
    service = AiExecutionService(_manager(runtime))

    with pytest.raises(
        ExecutionUnavailableError, match="modalities could not be determined"
    ):
        service.classify_text("text")
    assert runtime.classify_calls == []


@pytest.mark.parametrize("text", [None, 42, b"bytes", "", "   ", "\n\t "])
def test_invalid_or_empty_text_is_rejected(text):
    runtime = FakeClassificationRuntime()
    service = AiExecutionService(_manager(runtime))

    with pytest.raises(InvalidExecutionInputError):
        service.classify_text(text)
    assert runtime.classify_calls == []


def test_runtime_inference_exception_is_reported_and_not_retried():
    failing = FakeClassificationRuntime(
        inference_error=RuntimeError("inference exploded")
    )
    fallback = FakeClassificationRuntime(name="fallback")
    manager = _manager(runtime=failing)
    manager.register_runtime(fallback)
    manager.associate_runtime_with_model("fallback", "vendor.classifier")
    service = AiExecutionService(manager)

    with pytest.raises(ExecutionFailedError, match="inference exploded"):
        service.classify_text("text")

    assert len(failing.classify_calls) == 1
    assert fallback.classify_calls == []


@pytest.mark.parametrize(
    "scores",
    [
        None,
        ["positive"],
        "positive",
        {"label": "high"},
        {1: 0.5},
        {"label": True},
        {"label": float("nan")},
        {"label": float("inf")},
        {"label": float("-inf")},
    ],
)
def test_malformed_results_are_rejected(scores):
    runtime = FakeClassificationRuntime(scores=scores)
    service = AiExecutionService(_manager(runtime))

    with pytest.raises(InvalidExecutionResultError):
        service.classify_text("text")
    assert len(runtime.classify_calls) == 1


def test_finite_integer_scores_are_normalized_to_floats():
    runtime = FakeClassificationRuntime(scores={"positive": 1, "negative": 0})
    service = AiExecutionService(_manager(runtime))

    result = service.classify_text("text")

    assert result == {"positive": 1.0, "negative": 0.0}
    assert all(isinstance(value, float) for value in result.values())
    assert all(math.isfinite(value) for value in result.values())


# ---------------------------------------------------------------------------
# Image Description / Vision Execution Tests
# ---------------------------------------------------------------------------

TINY_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


_DEFAULT_VISION_RESULT = object()


class FakeImageDescriptionRuntime:
    name = "fake-vision"
    version = "1.0"

    def __init__(
        self,
        *,
        name="fake-vision",
        available=True,
        modalities=("vision",),
        result=_DEFAULT_VISION_RESULT,
        inference_error=None,
        availability_error=None,
    ):
        self.name = name
        self.available = available
        self.modalities = modalities
        self.result = (
            ImageDescriptionResult(
                description="A photo of a ginger cat sitting in a sunny window.",
                tags=("cat", "window", "pet", "sunlight"),
                model=name,
                confidence=0.95,
            )
            if result is _DEFAULT_VISION_RESULT
            else result
        )
        self.inference_error = inference_error
        self.availability_error = availability_error
        self.describe_calls = []

    def is_available(self):
        if self.availability_error is not None:
            raise self.availability_error
        return self.available

    def supported_modalities(self):
        if self.modalities is None:
            raise RuntimeError("modality probe failed")
        return list(self.modalities)

    def describe_image(self, image, mime_type=None, **kwargs):
        self.describe_calls.append({"image_len": len(image), "mime_type": mime_type})
        if self.inference_error is not None:
            raise self.inference_error
        return self.result


class NonVisionRuntime:
    name = "vision-modality-only"
    version = "1.0"

    def is_available(self):
        return True

    def supported_modalities(self):
        return ["vision"]


def _vision_descriptor(model_id="vendor.vision.balanced", **overrides):
    values = dict(
        name="Balanced Vision Model",
        version="1.0",
        modalities=("vision",),
        model_id=model_id,
    )
    values.update(overrides)
    return ModelDescriptor(**values)


def _vision_manager(runtime=None, descriptor=None, profile=Profile.BALANCED):
    manager = ModelManager()
    manager.set_profile(profile)
    descriptor = descriptor if descriptor is not None else _vision_descriptor()
    manager.register_capability(IMAGE_DESCRIPTION_CAPABILITY, descriptor, profile)
    if runtime is not None:
        manager.register_runtime(runtime)
        manager.associate_runtime_with_model(runtime.name, descriptor.identifier)
    return manager


def test_successful_image_description_with_result_object():
    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))

    result = service.describe_image(TINY_PNG_BYTES, mime_type="image/png")

    assert isinstance(result, ImageDescriptionResult)
    assert result.description == "A photo of a ginger cat sitting in a sunny window."
    assert result.tags == ("cat", "window", "pet", "sunlight")
    assert result.model == "fake-vision"
    assert result.confidence == 0.95
    assert runtime.describe_calls == [{"image_len": len(TINY_PNG_BYTES), "mime_type": "image/png"}]


def test_successful_image_description_with_dict():
    dict_result = {
        "description": "A green mountain under blue sky",
        "tags": ["mountain", "sky", "nature"],
        "confidence": 0.88,
    }
    runtime = FakeImageDescriptionRuntime(result=dict_result)
    service = AiExecutionService(_vision_manager(runtime))

    result = service.describe_image(TINY_PNG_BYTES)

    assert isinstance(result, ImageDescriptionResult)
    assert result.description == "A green mountain under blue sky"
    assert result.tags == ("mountain", "sky", "nature")
    assert result.model == "vendor.vision.balanced"
    assert result.confidence == 0.88


def test_confidence_is_not_invented_when_omitted_by_runtime():
    dict_without_confidence = {
        "description": "A scenic view of the ocean.",
        "tags": ["ocean", "water"],
    }
    runtime = FakeImageDescriptionRuntime(result=dict_without_confidence)
    service = AiExecutionService(_vision_manager(runtime))

    result = service.describe_image(TINY_PNG_BYTES)

    assert result.confidence is None


def test_vision_profile_selection():
    balanced_runtime = FakeImageDescriptionRuntime(
        name="balanced-vision",
        result={"description": "Balanced description", "tags": ["balanced"]},
    )
    manager = ModelManager()
    balanced_desc = _vision_descriptor("vendor.vision.balanced", name="Balanced Vision")
    manager.register_capability(IMAGE_DESCRIPTION_CAPABILITY, balanced_desc, Profile.BALANCED)
    manager.register_runtime(balanced_runtime)
    manager.associate_runtime_with_model(balanced_runtime.name, balanced_desc.identifier)

    service = AiExecutionService(manager)

    # In EFFICIENT profile, image_description is not permitted by PROFILE_CAPABILITIES -> fails closed
    manager.set_profile(Profile.EFFICIENT)
    with pytest.raises(ExecutionUnavailableError, match="no model descriptor for the current profile"):
        service.describe_image(TINY_PNG_BYTES)

    # In BALANCED profile, image_description is permitted -> executes successfully
    manager.set_profile(Profile.BALANCED)
    res_bal = service.describe_image(TINY_PNG_BYTES)
    assert res_bal.description == "Balanced description"
    assert len(balanced_runtime.describe_calls) == 1


def test_missing_image_description_descriptor_fails_closed():
    service = AiExecutionService(ModelManager())

    with pytest.raises(ExecutionUnavailableError, match="no model descriptor"):
        service.describe_image(TINY_PNG_BYTES)


def test_missing_compatible_vision_runtime_fails_closed():
    service = AiExecutionService(_vision_manager(runtime=None))

    with pytest.raises(ExecutionUnavailableError, match="no compatible runtime"):
        service.describe_image(TINY_PNG_BYTES)


def test_unavailable_vision_runtime_fails_closed():
    runtime = FakeImageDescriptionRuntime(available=False)
    service = AiExecutionService(_vision_manager(runtime))

    with pytest.raises(ExecutionUnavailableError, match="unavailable"):
        service.describe_image(TINY_PNG_BYTES)
    assert runtime.describe_calls == []


def test_vision_availability_probe_exception_fails_closed():
    runtime = FakeImageDescriptionRuntime(
        availability_error=RuntimeError("GPU probe failed")
    )
    service = AiExecutionService(_vision_manager(runtime))

    with pytest.raises(ExecutionUnavailableError, match="availability could not be determined"):
        service.describe_image(TINY_PNG_BYTES)
    assert runtime.describe_calls == []


def test_runtime_without_describe_image_method_fails_closed():
    runtime = NonVisionRuntime()  # supports vision modality but has no describe_image()
    manager = _vision_manager(runtime=runtime)

    with pytest.raises(ExecutionUnavailableError, match="does not implement"):
        AiExecutionService(manager).describe_image(TINY_PNG_BYTES)


def test_descriptor_without_vision_modality_fails_closed():
    descriptor = _vision_descriptor(modalities=("text",))
    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime, descriptor=descriptor))

    with pytest.raises(ExecutionUnavailableError, match="descriptor does not support vision"):
        service.describe_image(TINY_PNG_BYTES)
    assert runtime.describe_calls == []


def test_runtime_without_vision_modality_fails_closed():
    runtime = FakeImageDescriptionRuntime(modalities=("text",))
    service = AiExecutionService(_vision_manager(runtime))

    with pytest.raises(ExecutionUnavailableError, match="runtime does not support vision"):
        service.describe_image(TINY_PNG_BYTES)
    assert runtime.describe_calls == []


def test_runtime_vision_modality_probe_exception_fails_closed():
    runtime = FakeImageDescriptionRuntime(modalities=None)
    service = AiExecutionService(_vision_manager(runtime))

    with pytest.raises(ExecutionUnavailableError, match="modalities could not be determined"):
        service.describe_image(TINY_PNG_BYTES)
    assert runtime.describe_calls == []


@pytest.mark.parametrize(
    "invalid_input",
    [None, "not_bytes", 12345, [], {}, b"", b"corrupt not an image header at all"],
)
def test_invalid_image_inputs_are_rejected(invalid_input):
    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))

    with pytest.raises(InvalidExecutionInputError):
        service.describe_image(invalid_input)
    assert runtime.describe_calls == []


def test_oversized_image_bytes_are_rejected():
    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))
    huge_image = b"\x00" * (25 * 1024 * 1024)  # 25 MB

    with pytest.raises(InvalidExecutionInputError, match="exceeds maximum limit"):
        service.describe_image(huge_image)
    assert runtime.describe_calls == []


def test_vision_runtime_inference_exception_is_reported_and_not_retried():
    failing = FakeImageDescriptionRuntime(
        inference_error=RuntimeError("CUDA out of memory")
    )
    fallback = FakeImageDescriptionRuntime(name="fallback-vision")
    manager = _vision_manager(runtime=failing)
    manager.register_runtime(fallback)
    manager.associate_runtime_with_model("fallback-vision", "vendor.vision.balanced")
    service = AiExecutionService(manager)

    with pytest.raises(ExecutionFailedError, match="CUDA out of memory"):
        service.describe_image(TINY_PNG_BYTES)

    assert len(failing.describe_calls) == 1
    assert fallback.describe_calls == []


@pytest.mark.parametrize(
    "malformed_result",
    [
        None,
        42,
        "not a dict or result",
        [],
        {},  # missing description
        {"description": ""},  # empty description
        {"description": "   "},  # whitespace only
        {"description": 123},  # non-string description
        {"description": "valid", "tags": "not a list or tuple"},
        {"description": "valid", "tags": [123]},  # non-string tag
        {"description": "valid", "tags": [""]},  # empty tag
        {"description": "valid", "tags": ["   "]},  # blank tag
        {"description": "valid", "tags": [], "confidence": "high"},  # non-numeric confidence
        {"description": "valid", "tags": [], "confidence": 1.5},  # > 1.0
        {"description": "valid", "tags": [], "confidence": -0.1},  # < 0.0
        {"description": "valid", "tags": [], "confidence": float("nan")},
        {"description": "valid", "tags": [], "confidence": float("inf")},
    ],
)
def test_malformed_image_description_results_are_rejected(malformed_result):
    runtime = FakeImageDescriptionRuntime(result=malformed_result)
    service = AiExecutionService(_vision_manager(runtime))

    with pytest.raises(InvalidExecutionResultError):
        service.describe_image(TINY_PNG_BYTES)
    assert len(runtime.describe_calls) == 1


def test_image_bytes_are_never_leaked_in_exception_messages():
    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))
    secret_bytes = b"SECRET_USER_IMAGE_BYTES_XYZ123456789"

    with pytest.raises(InvalidExecutionInputError) as exc_info:
        service.describe_image(secret_bytes)

    assert "SECRET_USER_IMAGE_BYTES_XYZ123456789" not in str(exc_info.value)


def test_truncated_image_is_rejected():
    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))
    truncated = TINY_PNG_BYTES[: len(TINY_PNG_BYTES) // 2]

    with pytest.raises(InvalidExecutionInputError, match="corrupt or an unrecognized format"):
        service.describe_image(truncated)
    assert runtime.describe_calls == []


def test_mime_type_mismatch_is_rejected():
    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))

    # Declared as JPEG, but content is actually PNG
    with pytest.raises(InvalidExecutionInputError, match="does not match declared mime_type"):
        service.describe_image(TINY_PNG_BYTES, mime_type="image/jpeg")
    assert runtime.describe_calls == []


def test_non_image_mime_type_is_rejected():
    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))

    with pytest.raises(InvalidExecutionInputError, match="is not an image format"):
        service.describe_image(TINY_PNG_BYTES, mime_type="application/pdf")
    assert runtime.describe_calls == []


def test_matching_mime_type_is_accepted():
    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))

    result = service.describe_image(TINY_PNG_BYTES, mime_type="image/png")
    assert result.description == "A photo of a ginger cat sitting in a sunny window."


def test_decompression_bomb_is_rejected(monkeypatch):
    from PIL import Image

    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))

    class MockBombImage:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def verify(self):
            raise Image.DecompressionBombError("Too large")

    monkeypatch.setattr(Image, "open", lambda *args, **kwargs: MockBombImage())

    with pytest.raises(InvalidExecutionInputError, match="decompression limits"):
        service.describe_image(TINY_PNG_BYTES)


def test_missing_pillow_fails_closed_without_weakening_validation(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))

    with pytest.raises(ExecutionUnavailableError, match="Pillow is required"):
        service.describe_image(TINY_PNG_BYTES)


def test_image_passing_verify_but_failing_load_is_rejected():
    import io
    import struct
    import zlib
    from PIL import Image

    # Construct a PNG with valid structure and CRC, but corrupt zlib stream in IDAT
    header = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 10, 10, 8, 2, 0, 0, 0)
    ihdr_crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data))
    ihdr_chunk = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + ihdr_crc

    bad_idat_data = b"corrupted_zlib_stream_that_fails_decompression"
    bad_idat_crc = struct.pack(">I", zlib.crc32(b"IDAT" + bad_idat_data))
    idat_chunk = struct.pack(">I", len(bad_idat_data)) + b"IDAT" + bad_idat_data + bad_idat_crc

    iend_crc = struct.pack(">I", zlib.crc32(b"IEND"))
    iend_chunk = struct.pack(">I", 0) + b"IEND" + iend_crc

    image_bytes = header + ihdr_chunk + idat_chunk + iend_chunk

    # Confirm that verify() passes on this fixture
    with Image.open(io.BytesIO(image_bytes)) as img:
        img.verify()

    runtime = FakeImageDescriptionRuntime()
    service = AiExecutionService(_vision_manager(runtime))

    with pytest.raises(InvalidExecutionInputError, match="pixel data is corrupt or cannot be decoded"):
        service.describe_image(image_bytes)
    assert runtime.describe_calls == []
