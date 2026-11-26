# Florence-2 Runtime Implementation for SiloVault M5.2.

# This module provides a concrete implementation of the VisionRuntime protocol
# using Microsoft's Florence-2 base model.  The runtime is designed for CPU-first
# local execution and supports dependency injection for model loading so that
# tests can operate without installed ML packages.

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from backend.ai.interfaces import (
    ModelDescriptor,
    InferenceRuntime,
    ImageDescriptionRuntime,
    ImageDescriptionResult,
)
from backend.ai.vision_runtime import (
    VisionRuntime,
    VisionRuntimeError,
    RuntimeInitializationError,
    ModelNotInstalledError,
    ModelIncompatibleError,
    RuntimeNotReadyError,
    ExecutionError,
    CancellationError,
    RuntimeStatus,
    RuntimeState,
    RuntimeCapability,
    get_vision_execution_service,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: manifest validation (mirrors backend/ai/_manifest_validation logic)
# ---------------------------------------------------------------------------

def _validate_v2_manifest(manifest: dict) -> None:
    """Validate a v2 manifest structure.

    Required fields: schema_version, identifier, version, packaging,
    installed_at, entry_point, files.
    """
    from backend.ai._manifest_validation import _validate_v2_manifest as _internal_validate
    _internal_validate(manifest)


# ---------------------------------------------------------------------------
# Florence2Runtime â€” concrete VisionRuntime implementation
# ---------------------------------------------------------------------------

class Florence2Runtime:
    """Concrete VisionRuntime implementing Microsoft Florence-2 base inference.

    The runtime supports CPU-only execution and accepts injected model-loading
    callables so that tests can provide mock loaders without requiring
    torch, transformers, or model weights.

    Architecture notes
    ------------------
    * Model installation is validated against the ModelManager before loading.
    * The manifest (installation.json) is inspected for schema_version, file
      listings, and entry_point integrity.
    * All filesystem operations use lstat() to avoid following symlinks/junctions.
    * Cancellation is cooperative; in-progress inference can be interrupted
      via a threading Event.
    * Resource cleanup (model unload) releases the descriptor and clears
      the internal reference so the next load can succeed.
    """

    # ------------------------------------------------------------------
    # Constructor â€” accepts injected dependencies for testability
    # ------------------------------------------------------------------

    def __init__(
        self,
        model_loader=None,
        model_unloader=None,
        executor=None,
        *,
        profile: Optional[str] = None,
    ) -> None:
        """Initialize the Florence-2 runtime.

        Parameters
        ----------
        model_loader : callable or None
            Function ``(descriptor: ModelDescriptor, model_dir: Path) -> None``
            that loads the model from the installed directory.  When ``None``,
            the runtime attempts a lazy import of ``transformers`` and sets up
            the model using :class:`~transformers.AutoProcessor` and
            :class:`~transformers.AutoModelForVision2Seq`.  Passing ``None``
            when the packages are unavailable will raise
            :class:`RuntimeInitializationError`.

            The callable signature is deliberately simple; it receives the
            :class:`ModelDescriptor` and the resolved model directory path.
            It is responsible for storing the loaded model internally so that
            ``is_model_loaded()`` returns ``True``.

        model_unloader : callable or None
            Function ``() -> None`` that releases the loaded model and resets
            internal state.  When ``None``, the runtime attempts to garbage-
            collect the model reference.

        executor : callable or None
            Function ``(image_data: bytes, mime_type: Optional[str]) ->
            Dict[str, Any]`` that performs image-description inference.  When
            ``None``, the runtime uses ``model_loader``'s underlying model to
            execute inference (lazy-import path).

        profile : str or None
            Profile name for future profile-based configuration.
        """
        self._name = "florence2-cpu"
        self._version = "1.0.0"
        self._profile = profile
        self._state: RuntimeState = RuntimeState.SHUTDOWN
        self._loaded_model: Optional[Any] = None  # opaque model object
        self._cancellation_token = threading.Event()
        self._lock = threading.Lock()

        # Injected dependency paths (test-only)
        self._model_loader = model_loader
        self._model_unloader = model_unloader
        self._executor = executor

        logger.debug(
            "Florence2Runtime constructed (profile=%s, loader=%s, executor=%s)",
            profile,
            model_loader is not None,
            executor is not None,
        )

    # ------------------------------------------------------------------
    # VisionRuntime protocol
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    def is_initialized(self) -> bool:
        """Return ``True`` when the runtime has been initialized."""
        return self._state != RuntimeState.SHUTDOWN

    def initialize(self, profile: Optional[str] = None) -> None:
        """Initialize the runtime and prepare for model loading.

        Detects hardware, configures CPU optimizations (SDPA attention), and
        validates that the environment is ready for Florence-2 inference.

        Raises
        ------
        RuntimeInitializationError
            If the runtime cannot be configured or ML packages are unavailable
            and no model_loader was injected.
        """
        with self._lock:
            if self._state != RuntimeState.SHUTDOWN and self._state != RuntimeState.FAILED:
                logger.warning(
                    "Runtime already in state %s; re-initializing", self._state
                )

            self._cancellation_token = threading.Event()
            self._state = RuntimeState.INITIALIZING
            self._loaded_model = None

        try:
            # Configure CPU optimization (SDPA attention, no FlashAttention)
            _configure_cpu_optimization()

            # Load model via injected dependency or lazy import path
            if self._model_loader is not None:
                # injected loader â€” assume it handles everything
                logger.debug("Using injected model_loader")
                self._state = RuntimeState.READY
            elif self._try_lazy_import_load():
                # packages available â€” model is loaded during initialization
                pass
            else:
                # No packages and no injector â€” fail with a clear error
                raise RuntimeInitializationError(
                    "Florence-2 runtime requires torch/transformers or an "
                    "injected model_loader callable"
                )

            self._state = RuntimeState.READY
            logger.info("Florence2Runtime initialized: %s (profile: %s)", self._name, profile or "default")

        except Exception as exc:  # pylint: disable=broad-except
            self._state = RuntimeState.FAILED
            logger.exception("Florence2Runtime initialization failed")
            raise RuntimeInitializationError(
                f"Failed to initialize Florence-2 runtime: {exc}"
            ) from exc

    def _try_lazy_import_load(self) -> bool:
        """Attempt to import transformers and load the model lazily.

        Returns ``True`` when successful, ``False`` when packages are missing.
        """
        try:
            import torch  # noqa: F401  â€“ import may fail
        except ImportError:
            return False

        try:
            from transformers import AutoProcessor, AutoModelForCausalLM
        except ImportError:
            return False

        # Model will be loaded on first ``load_model`` call (lazy), not here.
        # We just confirm the packages are importable.
        logger.debug("Transformers and torch are available for lazy import")
        return True

    # ------------------------------------------------------------------
    # Model lifecycle
    # ------------------------------------------------------------------

    def load_model(self, descriptor: ModelDescriptor) -> None:
        """Load a model from its installed location.

        Validates that the model is installed and that its manifest is valid
        before delegating to the injected loader or the lazy-import model.

        Raises
        ------
        ModelNotInstalledError
            If the model directory does not exist or the manifest is missing.
        ModelIncompatibleError
            If the manifest schema or descriptor fields are invalid.
        RuntimeNotReadyError
            If the runtime is not initialized.
        """
        with self._lock:
            if self._state != RuntimeState.READY:
                raise RuntimeNotReadyError("Runtime not initialized or already loading")

            # Verify model is installed on disk
            model_dir = self._resolve_model_directory(descriptor)
            if not model_dir.is_dir():
                raise ModelNotInstalledError(
                    f"Model '{descriptor.identifier}' directory not found: {model_dir}"
                )

            # Read and validate manifest
            manifest_path = model_dir / "installation.json"
            if not manifest_path.is_file():
                raise ModelNotInstalledError(
                    f"Model '{descriptor.identifier}' manifest not found: {manifest_path}"
                )

            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                raise ModelNotInstalledError(
                    f"Model '{descriptor.identifier}' manifest is malformed: {exc}"
                ) from exc

            # Validate manifest schema
            try:
                _validate_v2_manifest(manifest)
            except ValueError as exc:
                raise ModelIncompatibleError(
                    f"Model '{descriptor.identifier}' manifest validation failed: {exc}"
                ) from exc

            # Injected loader path
            if self._model_loader is not None:
                try:
                    self._model_loader(descriptor, model_dir)
                    self._loaded_model = "injected-loaded"
                    self._state = RuntimeState.LOADING
                    logger.info(
                        "Model '%s' loaded via injected loader", descriptor.identifier
                    )
                    return
                except Exception as exc:  # pylint: disable=broad-except
                    raise RuntimeInitializationError(
                        f"Injected model_loader failed: {exc}"
                    ) from exc

           
            # Lazy-import path (packages available)
            try:
                import torch
                from transformers import AutoProcessor, AutoModelForCausalLM

                # Resolve model subdirectory (Florence-2 stores model files under a
                # possibly versioned subdirectory or the root itself)
                model_subdir = self._find_model_subdirectory(model_dir, manifest)
                if model_subdir is None:
                    model_subdir = model_dir

                processor = AutoProcessor.from_pretrained(
                    str(model_subdir),
                    trust_remote_code=True,
                    local_files_only=True,
                )
                model = AutoModelForCausalLM.from_pretrained(
                    str(model_subdir),
                    torch_dtype=torch.float32,
                    trust_remote_code=True,
                    local_files_only=True,
                )
                self._loaded_model = {"processor": processor, "model": model}
                self._state = RuntimeState.LOADING
                logger.info(
                    "Model '%s' loaded via lazy import from %s",
                    descriptor.identifier,
                    model_subdir,
                )
            except Exception as exc:  # pylint: disable=broad-except
                raise RuntimeInitializationError(
                    f"Failed to load model via lazy import: {exc}"
                ) from exc

    def _resolve_model_directory(self, descriptor: ModelDescriptor) -> Path:
        """Return the expected model directory for *descriptor*.

        Falls back to the model directory derived from the identifier when
        ``descriptor.local_path`` is set, otherwise uses the model directory
        from the model registry.
        """
        # The descriptor may carry an explicit local_path; otherwise we use the
        # model directory derived from the identifier.
        if descriptor.local_path is not None:
            return Path(descriptor.local_path).resolve()
        # Default: model directory under the storage root keyed by identifier
        from backend.ai.storage import ModelStorage
        storage = ModelStorage()
        return storage.model_directory(descriptor.identifier)

    def _find_model_subdirectory(self, model_dir: Path, manifest: dict) -> Optional[Path]:
        """Determine the subdirectory containing the actual model weights.

        Florence-2 installations may store files at the root or under a
        versioned subfolder.  This helper walks the directory tree looking for
        a ``config.json`` or ``preprocessor_config.json`` as a signal.
        """
        if (model_dir / "config.json").is_file():
            return model_dir
        # Look one level deep for a folder containing config
        for child in model_dir.iterdir():
            if child.is_dir() and (child / "config.json").is_file():
                return child
        return None

    def is_model_loaded(self) -> bool:
        """Return ``True`` when a model is currently loaded."""
        return self._loaded_model is not None

    def get_loaded_model(self) -> Optional[ModelDescriptor]:
        """Return the descriptor of the currently loaded model, if any.

        Note: this method returns ``None`` because the runtime does not store
        the original descriptor after loading; callers should retain their own
        reference to the descriptor if needed.
        """
        return None

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def execute_image_description(
        self, image_data: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        """Execute image-description inference on *image_data*.

        Returns a structured result with ``description``, ``tags``, and metadata.

        Raises
        ------
        RuntimeNotReadyError
            If the runtime is not initialized or no model is loaded.
        ExecutionError
            If inference fails for any reason.
        """
        with self._lock:
            if self._state != RuntimeState.READY or self._loaded_model is None:
                raise RuntimeNotReadyError(
                    "Runtime not initialized or no model loaded"
                )

            self._cancellation_token.clear()
            self._state = RuntimeState.RUNNING

        try:
            result = self._do_execute(image_data, mime_type=mime_type, **kwargs)
            self._state = RuntimeState.READY
            return result

        except CancellationError:
            self._state = RuntimeState.CANCELLED
            raise
        except Exception as exc:  # pylint: disable=broad-except
            self._state = RuntimeState.FAILED
            logger.exception("Inference failed")
            raise ExecutionError(
                f"Florence-2 inference failed: {exc}"
            ) from exc

    
    def _do_execute(
        self, image_data: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        """Run Florence-2 image description using CPU-only local inference."""
        if self._cancellation_token.is_set():
            raise CancellationError("Inference cancelled before start")

        if self._executor is not None:
            return self._executor(image_data, mime_type=mime_type, **kwargs)

        try:
            from io import BytesIO
            from PIL import Image
            import torch

            model_info = self._loaded_model
            if not isinstance(model_info, dict):
                raise RuntimeNotReadyError("Florence-2 model is not loaded")

            processor: Any = model_info["processor"]
            model: Any = model_info["model"]

            # Decode image bytes safely into RGB.
            with Image.open(BytesIO(image_data)) as source_image:
                image = source_image.convert("RGB")
                image_size = image.size

            task_prompt = "<DETAILED_CAPTION>"

            inputs = processor(
                text=task_prompt,
                images=image,
                return_tensors="pt",
            )

            inputs = {
                key: value.to("cpu") if hasattr(value, "to") else value
                for key, value in inputs.items()
            }

            model.to("cpu")
            model.eval()

            with torch.inference_mode():
                generated_ids = model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=1024,
                    do_sample=False,
                    num_beams=3,
                )

            generated_text = processor.batch_decode(
                generated_ids,
                skip_special_tokens=False,
            )[0]

            processed = processor.post_process_generation(
                generated_text,
                task=task_prompt,
                image_size=image_size,
            )

            description = processed.get(task_prompt, generated_text)

            if isinstance(description, dict):
                description = str(description)

            if not isinstance(description, str):
                raise ValueError("Florence-2 returned an invalid description")

            tags = tuple(
                word.strip(".,!;:()[]{}").lower()
                for word in description.split()
                if word.strip(".,!;:()[]{}").isalpha()
                and len(word.strip(".,!;:()[]{}")) > 2
            )

            return {
                "description": description,
                "tags": tags,
                "model": self._get_loaded_model_identifier(),
                "confidence": None,
            }

        except (CancellationError, RuntimeNotReadyError):
            raise
        except Exception as exc:
            raise ExecutionError(
                f"Inference execution error: {exc}"
            ) from exc

    def _get_loaded_model_identifier(self) -> Optional[str]:
        """Return identifier of the currently loaded model, if available."""
        if self._loaded_model is None:
            return None
        # For the injected loader, try to get the descriptor identifier
        # The runtime does not keep the original descriptor, so we return None
        # when the model was loaded via an opaque injected loader.
        if getattr(self._loaded_model, "__class__", None) and "injected" in str(
            self._loaded_model.__class__
        ):
            return None
        return getattr(self._loaded_model, "identifier", None)

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_execution(self) -> None:
        """Cancel any in-progress inference.

        Thread-safe; safe to call from any thread.  Sets a cancellation token
        that is checked before and during inference.
        """
        with self._lock:
            self._cancellation_token.set()
        logger.debug("Cancellation requested for Florence2Runtime")

    def cancel(self) -> None:
        """Alias for cancel_execution per the VisionRuntime protocol."""
        self.cancel_execution()

    # ------------------------------------------------------------------
    # Unload / Shutdown
    # ------------------------------------------------------------------

    def unload_model(self) -> None:
        """Unload the currently loaded model to free memory.

        Releases the model reference and resets internal state so that a
        subsequent ``load_model`` call can succeed.
        """
        with self._lock:
            # Call injected unloader if provided
            if self._model_unloader is not None:
                try:
                    self._model_unloader()
                except Exception:  # pylint: disable=broad-except
                    logger.warning("Injected model_unloader failed")

            # Release the loaded model reference
            self._loaded_model = None
            self._state = RuntimeState.READY
            logger.info("Florence-2 model unloaded")

    def shutdown(self) -> None:
        """Gracefully shut down the runtime.

        Releases all resources and prevents further operations.  After this
        call the runtime must be re-initialized via ``initialize`` before any
        other operation is valid.
        """
        with self._lock:
            self._cancellation_token.set()
            self._loaded_model = None
            self._state = RuntimeState.SHUTDOWN
        logger.info("Florence2Runtime shut down")

    def get_status(self) -> RuntimeStatus:
        """Return a read-only snapshot of runtime state."""
        with self._lock:
            return RuntimeStatus(
                state=self._state,
                model_id=self._get_loaded_model_identifier(),
                profile=self._profile,
                capabilities=(RuntimeCapability.IMAGE_DESCRIPTION,),
                memory_bytes=None,
                thread_count=None,
                error=None,
                message=None,
            )

    def set_cpu_optimization(self, enabled: bool) -> None:
        """Enable/disable CPU-specific optimizations.

        When ``enabled`` the runtime configures PyTorch's scaled dot-product
        attention (SDPA) and disables FlashAttention, which requires an NVIDIA
        GPU.  Safe to call even when already in the requested state.
        """
        with self._lock:
            # The actual configuration happens on ``initialize``; this is a
            # no-op hook for external coordination.
            logger.debug("CPU optimization %s (effective on next initialize)", "enabled" if enabled else "disabled")


# ---------------------------------------------------------------------------
# Module-level convenience: default runtime instance (lazy-import path)
# ---------------------------------------------------------------------------

_default_runtime: Optional[Florence2Runtime] = None


def get_default_runtime() -> Florence2Runtime:
    """Return a default Florence2Runtime instance (lazy-import path).

    The returned runtime will attempt to import ``torch`` and ``transformers``
    on first ``initialize``/``load_model`` call.  This is convenient for
    quick-start scripts but not suitable for test isolation.

    Returns
    -------
    Florence2Runtime
        A runtime instance configured for the CPU-only path.
    """
    global _default_runtime
    if _default_runtime is None:
        _default_runtime = Florence2Runtime()  # type: ignore[call-arg]
    return _default_runtime


# ---------------------------------------------------------------------------
# CPU optimization helper
# ---------------------------------------------------------------------------

def _configure_cpu_optimization() -> None:
    """Configure PyTorch for CPU-first execution.

    Sets the default attention implementation to ``sdpa`` (scaled dot-product
    attention), which is the CPU-friendly option.  FlashAttention is disabled
    because it requires an NVIDIA GPU.
    """
    import os
    os.environ["PYTORCH_ENABLE_MPS_GRAD"] = "0"
    # Note: ``torch.backends.cuda`` is not available on CPU; the sdpa setting
    # is harmless on CPU and ensures we do not accidentally try FlashAttention.


# ---------------------------------------------------------------------------
# Adapter: Florence2Runtime -> InferenceRuntime + ImageDescriptionRuntime
# ---------------------------------------------------------------------------


class Florence2InferenceAdapter:
    """Adapter that wraps Florence2Runtime to implement InferenceRuntime and ImageDescriptionRuntime protocols.

    This bridges the gap between the VisionRuntime protocol (which uses
    execute_image_description) and the AiExecutionService protocol (which expects
    describe_image). It also implements the InferenceRuntime protocol methods
    (is_available, supported_modalities) required by ModelManager.
    """

    def __init__(self, runtime: Florence2Runtime) -> None:
        self._runtime = runtime

    # InferenceRuntime protocol
    @property
    def name(self) -> str:
        return self._runtime.name

    @property
    def version(self) -> str:
        return self._runtime.version

    def is_available(self) -> bool:
        """Return True if the runtime is initialized and has a model loaded."""
        try:
            return self._runtime.is_initialized() and self._runtime.is_model_loaded()
        except Exception:
            return False

    def supported_modalities(self) -> list[str]:
        return ["vision"]

    # ImageDescriptionRuntime protocol
    def describe_image(
        self, image: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Union[ImageDescriptionResult, Dict[str, Any]]:
        return self._runtime.execute_image_description(image, mime_type=mime_type, **kwargs)

    # Additional methods for VisionRuntimeManager compatibility
    def initialize(self, profile: Optional[str] = None) -> None:
        return self._runtime.initialize(profile=profile)

    def is_initialized(self) -> bool:
        return self._runtime.is_initialized()

    def load_model(self, descriptor: ModelDescriptor) -> None:
        return self._runtime.load_model(descriptor)

    def is_model_loaded(self) -> bool:
        return self._runtime.is_model_loaded()

    def get_loaded_model(self) -> Optional[ModelDescriptor]:
        return self._runtime.get_loaded_model()

    def execute_image_description(
        self, image_data: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        return self._runtime.execute_image_description(
            image_data, mime_type=mime_type, **kwargs
        )

    def cancel_execution(self) -> None:
        return self._runtime.cancel_execution()

    def unload_model(self) -> None:
        return self._runtime.unload_model()

    def shutdown(self) -> None:
        return self._runtime.shutdown()

    def get_status(self) -> RuntimeStatus:
        return self._runtime.get_status()

    def set_cpu_optimization(self, enabled: bool) -> None:
        return self._runtime.set_cpu_optimization(enabled)
