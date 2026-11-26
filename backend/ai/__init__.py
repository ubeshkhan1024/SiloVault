"""Local AI processing integration.

M4.2 AI foundation modules are now available:

* :mod:`backend.ai.interfaces` – core interfaces and registries
* :mod:`backend.ai.storage` – local model directory resolution
* :mod:`backend.ai.installation` – verified single-file model installation
* :mod:`backend.ai.bootstrap` – shared AI model services composition
* :mod:`backend.ai.catalogue` – trusted, code-controlled model catalogue
* :mod:`backend.ai.execution` – synchronous execution bridge for supported capabilities
* :mod:`backend.ai.text_extraction` – basic text extraction for supported uploads
* :mod:`backend.ai.processors` – AI task processors bridging the worker to AI services
* :mod:`backend.ai.registration` – processor registration with the AI worker
* :mod:`backend.ai.lifecycle` – application lifespan integration for the AI worker

Local AI processing must remain on the primary laptop and must
never be exposed directly to the public internet.
"""

from __future__ import annotations

from backend.ai.interfaces import *  # noqa: F401,F403
from backend.ai.pack_manager import PackManager, PackMetadata, PackCapability, PackManifest, PackApproval
from backend.ai.bootstrap import (
    AiModelServices,
    AiModelServicesUnavailableError,
    build_ai_model_services,
    configure_ai_model_services,
    get_ai_model_services,
)
from backend.ai.catalogue import MODEL_CATALOGUE, register_catalogue
from backend.ai.execution import (
    AiExecutionService,
    ExecutionError,
    ExecutionFailedError,
    ExecutionUnavailableError,
    IMAGE_DESCRIPTION_CAPABILITY,
    InvalidExecutionInputError,
    InvalidExecutionResultError,
    TEXT_CLASSIFICATION_CAPABILITY,
)
from backend.ai.text_extraction import (
    MAX_TEXT_EXTRACTION_BYTES,
    SUPPORTED_EXTENSIONS,
    ExtractedText,
    FileTooLargeError,
    InvalidContentError,
    InvalidEncodingError,
    TextExtractionError,
    TextExtractionService,
    UnsupportedFormatError,
)
from backend.ai.processors import (
    IMAGE_DESCRIPTION_TASK_TYPE,
    TEXT_CLASSIFICATION_TASK_TYPE,
    FileRecordNotFoundError,
    ImageDescriptionProcessor,
    ImageTooLargeError,
    MissingFileReferenceError,
    ProcessorError,
    TaskNotFoundError,
    TextClassificationProcessor,
    UnsupportedImageTypeError,
    UnsupportedTaskTypeError,
)
from backend.ai.registration import (
    TaskProcessor,
    build_ai_worker,
    build_task_processors,
    register_task_processors,
)
from backend.ai.lifecycle import (
    AiWorkerRuntime,
    start_ai_worker,
    stop_ai_worker,
)
from backend.ai.installation import (
    InstallationResult,
    ModelDownloader,
    ModelInstallationService,
)
from backend.ai.storage import ModelStorage

__all__: list[str] = [
    "InferenceRuntime",
    "TextGenerationRuntime",
    "EmbeddingRuntime",
    "ClassificationRuntime",
    "ImageDescriptionResult",
    "ImageDescriptionRuntime",
    "ModelDescriptor",
    "CapabilityRegistry",
    "ModelRegistry",
    "ModelValidationError",
    "Profile",
    "GpuInfo",
    "HardwareInfo",
    "HardwareDetector",
    "ModelManager",
    "PROFILE_CAPABILITIES",
    "BALANCED_MIN_TOTAL_RAM_BYTES",
    "BALANCED_MIN_LOGICAL_CORES",
    "BALANCED_MIN_GPU_MEMORY_BYTES",
    "ModelStorage",
    "ModelDownloader",
    "ModelInstallationService",
    "InstallationResult",
    "AiModelServices",
    "AiModelServicesUnavailableError",
    "build_ai_model_services",
    "configure_ai_model_services",
    "get_ai_model_services",
    "MODEL_CATALOGUE",
    "register_catalogue",
    "AiExecutionService",
    "ExecutionError",
    "ExecutionUnavailableError",
    "InvalidExecutionInputError",
    "ExecutionFailedError",
    "InvalidExecutionResultError",
    "TEXT_CLASSIFICATION_CAPABILITY",
    "IMAGE_DESCRIPTION_CAPABILITY",
    "TextExtractionService",
    "ExtractedText",
    "TextExtractionError",
    "UnsupportedFormatError",
    "FileTooLargeError",
    "InvalidContentError",
    "InvalidEncodingError",
    "MAX_TEXT_EXTRACTION_BYTES",
    "SUPPORTED_EXTENSIONS",
    "TextClassificationProcessor",
    "ImageDescriptionProcessor",
    "ProcessorError",
    "TaskNotFoundError",
    "UnsupportedTaskTypeError",
    "MissingFileReferenceError",
    "FileRecordNotFoundError",
    "ImageTooLargeError",
    "UnsupportedImageTypeError",
    "TEXT_CLASSIFICATION_TASK_TYPE",
    "IMAGE_DESCRIPTION_TASK_TYPE",
    "TaskProcessor",
    "build_task_processors",
    "register_task_processors",
    "build_ai_worker",
    "AiWorkerRuntime",
    "start_ai_worker",
    "stop_ai_worker",
]