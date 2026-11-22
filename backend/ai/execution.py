"""Synchronous execution bridge for locally supported AI capabilities.

The bridge exposes ``text_classification`` and ``image_description``. It resolves
the descriptor and runtime through :class:`~backend.ai.interfaces.ModelManager`, so
profile selection and runtime compatibility rules stay owned by the manager.

No model loading, download, background worker, or network access happens here:
the selected runtime must already be registered and available. Failures are
reported as explicit execution exceptions; a failing runtime is never silently
replaced by another one.
"""

from __future__ import annotations

import io
import math
from typing import Any, Dict, List, Optional, Tuple, Union

from backend.ai.interfaces import (
    ClassificationRuntime,
    ImageDescriptionResult,
    ImageDescriptionRuntime,
    ModelDescriptor,
    ModelManager,
    ModelValidationError,
)

TEXT_CLASSIFICATION_CAPABILITY = "text_classification"
IMAGE_DESCRIPTION_CAPABILITY = "image_description"
_TEXT_MODALITY = "text"
_VISION_MODALITY = "vision"

#: Safety limits for input images to prevent memory exhaustion and decompression bombs.
MAX_IMAGE_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB safety limit
MAX_IMAGE_DIMENSION = 16384  # Max width/height
MAX_IMAGE_TOTAL_PIXELS = 67_108_864  # Max total pixels (e.g. 8192 x 8192)


class ExecutionError(RuntimeError):
    """Base class for all AI execution failures."""


class ExecutionUnavailableError(ExecutionError):
    """The capability cannot be executed safely with the current configuration.

    Covers a missing descriptor or compatible runtime, an unavailable runtime,
    a failing availability probe, incompatible modalities, and a runtime that
    does not structurally implement the capability protocol.
    """


class InvalidExecutionInputError(ExecutionError):
    """The supplied execution input is missing or invalid."""


class ExecutionFailedError(ExecutionError):
    """The selected runtime raised while performing inference."""


class InvalidExecutionResultError(ExecutionError):
    """The runtime returned a malformed classification result."""


class AiExecutionService:
    """Executes supported capabilities against the manager's selected runtime.

    The service is stateless apart from the manager reference.  A new runtime
    is never selected after resolution, including when inference fails.
    """

    def __init__(self, manager: ModelManager) -> None:
        if not isinstance(manager, ModelManager):
            raise TypeError("manager must be a ModelManager instance")
        self._manager = manager

    @property
    def manager(self) -> ModelManager:
        """Return the manager this service resolves capabilities through."""
        return self._manager

    def classify_text(self, text: str) -> Dict[str, float]:
        """Classify *text* and return a score mapping.

        Raises :class:`InvalidExecutionInputError` for empty or non-text input,
        :class:`ExecutionUnavailableError` when resolution or availability
        fails, :class:`ExecutionFailedError` when the runtime raises, and
        :class:`InvalidExecutionResultError` for malformed scores.
        """
        _validate_text_input(text)

        descriptor = self._resolve_descriptor()
        _require_text_modality(descriptor)
        runtime = self._resolve_runtime()
        _require_runtime_text_modality(runtime)
        _require_available(runtime)
        if not isinstance(runtime, ClassificationRuntime) or not callable(
            getattr(runtime, "classify", None)
        ):
            raise ExecutionUnavailableError(
                f"Selected runtime does not implement {TEXT_CLASSIFICATION_CAPABILITY}"
            )

        try:
            result = runtime.classify(text=text)
        except Exception as exc:
            raise ExecutionFailedError(
                f"Runtime failed during {TEXT_CLASSIFICATION_CAPABILITY}: {exc}"
            ) from exc

        return _validate_scores(result)

    def describe_image(
        self, image: bytes, mime_type: Optional[str] = None
    ) -> ImageDescriptionResult:
        """Analyze *image* and return a structured description and tags.

        Raises :class:`InvalidExecutionInputError` for empty, non-bytes, corrupt,
        or oversized image input, :class:`ExecutionUnavailableError` when
        resolution, modality check, or availability fails,
        :class:`ExecutionFailedError` when the runtime raises, and
        :class:`InvalidExecutionResultError` for malformed results.
        """
        _validate_image_input(image, mime_type=mime_type)

        descriptor = self._resolve_descriptor(IMAGE_DESCRIPTION_CAPABILITY)
        _require_vision_modality(descriptor)
        runtime = self._resolve_runtime(IMAGE_DESCRIPTION_CAPABILITY)
        _require_runtime_vision_modality(runtime)
        _require_available(runtime)
        if not isinstance(runtime, ImageDescriptionRuntime) or not callable(
            getattr(runtime, "describe_image", None)
        ):
            raise ExecutionUnavailableError(
                f"Selected runtime does not implement {IMAGE_DESCRIPTION_CAPABILITY}"
            )

        try:
            result = runtime.describe_image(image=image, mime_type=mime_type)
        except Exception as exc:
            raise ExecutionFailedError(
                f"Runtime failed during {IMAGE_DESCRIPTION_CAPABILITY}: {exc}"
            ) from exc

        return _validate_image_description_result(
            result, model_id=descriptor.identifier
        )

    # -- resolution --------------------------------------------------------

    def _resolve_descriptor(
        self, capability: str = TEXT_CLASSIFICATION_CAPABILITY
    ) -> ModelDescriptor:
        descriptor = self._manager.get_capability_descriptor(capability)
        if descriptor is None:
            raise ExecutionUnavailableError(
                f"Capability '{capability}' has no model "
                "descriptor for the current profile"
            )
        return descriptor

    def _resolve_runtime(
        self, capability: str = TEXT_CLASSIFICATION_CAPABILITY
    ):
        runtime_name = self._manager.get_compatible_runtime_for_capability(
            capability
        )
        if runtime_name is None:
            raise ExecutionUnavailableError(
                f"Capability '{capability}' has no compatible runtime"
            )
        try:
            return self._manager.get_runtime(runtime_name)
        except KeyError as exc:
            raise ExecutionUnavailableError(
                f"Compatible runtime '{runtime_name}' is not registered"
            ) from exc


def _validate_text_input(text: str) -> None:
    if not isinstance(text, str):
        raise InvalidExecutionInputError("text input must be a string")
    if not text.strip():
        raise InvalidExecutionInputError("text input must not be empty")


def _require_text_modality(descriptor: ModelDescriptor) -> None:
    modalities = {modality.casefold() for modality in descriptor.modalities}
    if _TEXT_MODALITY not in modalities:
        raise ExecutionUnavailableError(
            "Selected model descriptor does not support text modality"
        )


def _require_runtime_text_modality(runtime) -> None:
    try:
        supported = runtime.supported_modalities()
        modalities = {str(modality).casefold() for modality in supported}
    except Exception as exc:
        # A failing modality probe is not evidence of support.
        raise ExecutionUnavailableError(
            "Runtime modalities could not be determined"
        ) from exc
    if _TEXT_MODALITY not in modalities:
        raise ExecutionUnavailableError("Selected runtime does not support text modality")


def _has_valid_image_signature(image: bytes) -> bool:
    """Check known image header magic bytes."""
    if len(image) < 8:
        return False
    # PNG: \x89PNG\r\n\x1a\n
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    # JPEG: \xff\xd8\xff
    if image.startswith(b"\xff\xd8\xff"):
        return True
    # GIF: GIF87a / GIF89a
    if image.startswith(b"GIF87a") or image.startswith(b"GIF89a"):
        return True
    # WEBP: RIFF....WEBP
    if image.startswith(b"RIFF") and len(image) >= 12 and image[8:12] == b"WEBP":
        return True
    # BMP: BM
    if image.startswith(b"BM"):
        return True
    # TIFF: II*\x00 or MM\x00*
    if image.startswith(b"II*\x00") or image.startswith(b"MM\x00*"):
        return True
    return False


def _validate_image_input(
    image: Any,
    mime_type: Optional[str] = None,
    max_bytes: int = MAX_IMAGE_DOWNLOAD_BYTES,
    max_dimension: int = MAX_IMAGE_DIMENSION,
    max_pixels: int = MAX_IMAGE_TOTAL_PIXELS,
) -> None:
    """Validate image bytes, content signatures, and safety limits.

    Never logs or includes image bytes or sensitive content in exceptions.
    Requires Pillow for decoding, structural integrity, and dimension verification;
    fails closed with ExecutionUnavailableError if Pillow is missing so validation
    is never silently weakened.
    """
    if not isinstance(image, bytes):
        raise InvalidExecutionInputError("image input must be bytes")
    if len(image) == 0:
        raise InvalidExecutionInputError("image input must not be empty")
    if len(image) > max_bytes:
        raise InvalidExecutionInputError(
            f"image byte size ({len(image)}) exceeds maximum limit of {max_bytes} bytes"
        )

    # Actual image decoding/verification and dimension checks via Pillow
    try:
        from PIL import Image
    except ImportError as exc:
        raise ExecutionUnavailableError(
            "Pillow is required for image validation and decoding; install Pillow to enable image understanding"
        ) from exc

    try:
        with Image.open(io.BytesIO(image)) as img:
            img.verify()
            width, height = img.size
            detected_mimetype = getattr(img, "get_format_mimetype", lambda: None)()
    except Image.DecompressionBombError as exc:
        raise InvalidExecutionInputError(
            "image exceeds decompression limits (possible decompression bomb)"
        ) from exc
    except Exception as exc:
        raise InvalidExecutionInputError(
            "image content is corrupt or an unrecognized format"
        ) from exc

    if width <= 0 or height <= 0:
        raise InvalidExecutionInputError("image dimensions must be positive")
    if width > max_dimension or height > max_dimension:
        raise InvalidExecutionInputError(
            f"image dimension ({width}x{height}) exceeds maximum limit of {max_dimension}"
        )
    if width * height > max_pixels:
        raise InvalidExecutionInputError(
            f"image total pixel count ({width * height}) exceeds safety limit of {max_pixels}"
        )

    # Validate declared MIME type when provided
    if mime_type is not None:
        if not isinstance(mime_type, str) or not mime_type.strip():
            raise InvalidExecutionInputError("mime_type must be a non-empty string when provided")
        normalised_mime = mime_type.split(";", 1)[0].strip().casefold()
        if not normalised_mime.startswith("image/"):
            raise InvalidExecutionInputError(
                f"declared mime_type '{mime_type}' is not an image format"
            )
        if detected_mimetype and normalised_mime != detected_mimetype.casefold():
            raise InvalidExecutionInputError(
                f"image content format '{detected_mimetype}' does not match declared mime_type '{mime_type}'"
            )

    # Reopen to verify actual pixel raster decoding. Image.verify() checks
    # structural integrity and markers/chunks, but does not decompress pixel
    # streams. Calling load() ensures the pixel payload is not truncated or
    # malformed. Releasing the image context immediately discards decoded data.
    try:
        with Image.open(io.BytesIO(image)) as img:
            img.load()
    except Image.DecompressionBombError as exc:
        raise InvalidExecutionInputError(
            "image exceeds decompression limits (possible decompression bomb)"
        ) from exc
    except Exception as exc:
        raise InvalidExecutionInputError(
            "image pixel data is corrupt or cannot be decoded"
        ) from exc


def _require_vision_modality(descriptor: ModelDescriptor) -> None:
    modalities = {modality.casefold() for modality in descriptor.modalities}
    if _VISION_MODALITY not in modalities:
        raise ExecutionUnavailableError(
            "Selected model descriptor does not support vision modality"
        )


def _require_runtime_vision_modality(runtime) -> None:
    try:
        supported = runtime.supported_modalities()
        modalities = {str(modality).casefold() for modality in supported}
    except Exception as exc:
        raise ExecutionUnavailableError(
            "Runtime modalities could not be determined"
        ) from exc
    if _VISION_MODALITY not in modalities:
        raise ExecutionUnavailableError("Selected runtime does not support vision modality")


def _require_available(runtime) -> None:
    try:
        available = runtime.is_available()
    except Exception as exc:
        raise ExecutionUnavailableError(
            "Runtime availability could not be determined"
        ) from exc
    if not available:
        raise ExecutionUnavailableError("Selected runtime is unavailable")


def _validate_scores(result) -> Dict[str, float]:
    if not isinstance(result, dict):
        raise InvalidExecutionResultError("classification result must be a dictionary")

    scores: Dict[str, float] = {}
    for label, value in result.items():
        if not isinstance(label, str):
            raise InvalidExecutionResultError(
                "classification result keys must be strings"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidExecutionResultError(
                f"classification score for '{label}' must be numeric"
            )
        try:
            number = float(value)
        except (OverflowError, ValueError) as exc:
            raise InvalidExecutionResultError(
                f"classification score for '{label}' is not a finite number"
            ) from exc
        if not math.isfinite(number):
            raise InvalidExecutionResultError(
                f"classification score for '{label}' must be finite"
            )
        scores[label] = number
    return scores


def _validate_image_description_result(
    result: Any, model_id: Optional[str] = None
) -> ImageDescriptionResult:
    """Validate and normalize image description result from runtime.

    Confidence is never fabricated: only preserved when genuinely supplied
    as a valid finite numeric value between 0.0 and 1.0.
    """
    if isinstance(result, ImageDescriptionResult):
        description = result.description
        tags_raw = result.tags
        model = result.model or model_id
        confidence = result.confidence
    elif isinstance(result, dict):
        if "description" not in result:
            raise InvalidExecutionResultError(
                "image description result missing 'description'"
            )
        description = result["description"]
        tags_raw = result.get("tags", ())
        model = result.get("model", model_id)
        confidence = result.get("confidence")
    else:
        raise InvalidExecutionResultError(
            "image description result must be an ImageDescriptionResult or dict"
        )

    if not isinstance(description, str) or not description.strip():
        raise InvalidExecutionResultError(
            "image description must be a non-empty string"
        )
    cleaned_description = description.strip()

    if not isinstance(tags_raw, (list, tuple, set)):
        raise InvalidExecutionResultError(
            "image tags must be a list or tuple of strings"
        )
    cleaned_tags: List[str] = []
    for tag in tags_raw:
        if not isinstance(tag, str) or not tag.strip():
            raise InvalidExecutionResultError(
                f"image tag {tag!r} must be a non-empty string"
            )
        cleaned_tags.append(tag.strip())

    cleaned_confidence: Optional[float] = None
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise InvalidExecutionResultError(
                "image confidence score must be numeric"
            )
        try:
            val = float(confidence)
        except (ValueError, OverflowError) as exc:
            raise InvalidExecutionResultError(
                "image confidence score is not a valid float"
            ) from exc
        if not math.isfinite(val) or val < 0.0 or val > 1.0:
            raise InvalidExecutionResultError(
                "image confidence score must be a finite number between 0.0 and 1.0"
            )
        cleaned_confidence = val

    return ImageDescriptionResult(
        description=cleaned_description,
        tags=tuple(cleaned_tags),
        model=model,
        confidence=cleaned_confidence,
    )
