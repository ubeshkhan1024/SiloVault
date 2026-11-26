"""Composition root for SiloVault's local AI model services.

This module builds the small set of long-lived AI components the application
shares: model storage locations, the model metadata catalogue, and the
:class:`~backend.ai.interfaces.ModelManager` that coordinates profiles,
runtimes, and capabilities.

Model installation is deliberately **not** constructed here yet: there is no
production downloader, so no installation or network capability is exposed.

Construction performs no filesystem or network work beyond resolving paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from backend.ai.catalogue import register_catalogue
from backend.ai.florence2_runtime import Florence2InferenceAdapter, Florence2Runtime
from backend.ai.interfaces import ModelManager, ModelRegistry, Profile
from backend.ai.storage import ModelStorage
from backend.ai.vision_runtime import VisionRuntimeManager
from backend.config import Settings, settings as default_settings


class AiModelServicesUnavailableError(RuntimeError):
    """Raised when the shared AI model services cannot be constructed."""


@dataclass(frozen=True)
class AiModelServices:
    """Shared, long-lived AI model components.

    ``registry`` is the same catalogue instance used by ``manager``; the
    capability registry is owned and bound by the manager.
    """

    storage: ModelStorage
    registry: ModelRegistry
    manager: ModelManager
    vision_runtime_manager: Optional[VisionRuntimeManager] = None

    def shutdown(self) -> None:
        """Shut down the vision runtime if initialized.

        Idempotent: safe to call multiple times.
        """
        if self.vision_runtime_manager is not None:
            try:
                self.vision_runtime_manager.shutdown()
            except Exception:
                pass  # Best effort; log if needed


def _initialize_vision_runtime(manager: ModelManager) -> tuple[Optional[Florence2InferenceAdapter], Optional[VisionRuntimeManager]]:
    """Initialize and register the Florence-2 vision runtime.

    Returns a tuple of (adapter, vision_runtime_manager) if initialization succeeds,
    (None, None) if the runtime cannot be initialized (e.g., missing ML dependencies).
    This allows the application to start without vision capabilities while preserving
    text-processing functionality.

    The VisionRuntimeManager reuses the runtime registered in ModelManager to ensure
    a single authoritative runtime instance.
    """
    # Create VisionRuntimeManager first so it can reuse any pre-registered runtime
    vision_manager = VisionRuntimeManager(manager)

    try:
        runtime = Florence2Runtime()
        runtime.initialize()
    except Exception as exc:
        # Missing torch/transformers or other initialization failure.
        # Log and return None so the application can continue without vision.
        import logging
        logging.getLogger(__name__).warning(
            "Florence-2 runtime unavailable (text processing still works): %s", exc
        )
        return None, None

    # Wrap with adapter to implement InferenceRuntime + ImageDescriptionRuntime
    adapter = Florence2InferenceAdapter(runtime)

    # Register the runtime with the ModelManager
    try:
        manager.register_runtime(adapter)
    except ValueError:
        # Already registered (idempotent)
        pass

    # Associate the runtime with the Florence-2 model descriptor
    model_id = "microsoft.florence-2-base"
    if manager.model_registry.is_registered(model_id):
        try:
            manager.associate_runtime_with_model(adapter.name, model_id)
        except KeyError:
            # Runtime or model not registered - this is fine, capability will be unavailable
            pass

    # Initialize the VisionRuntimeManager with the already-registered runtime.
    # Since the adapter implements VisionRuntime protocol, we can use it directly.
    # The VisionRuntimeManager will detect it's already initialized and reuse it.
    try:
        # Use a dummy runtime class that returns the already-initialized adapter
        # This avoids creating a second Florence2Runtime instance
        class _PreInitializedRuntime:
            def __init__(self, adapter: Florence2InferenceAdapter):
                self._adapter = adapter

            def __call__(self):
                return self._adapter

        vision_manager.initialize_runtime(_PreInitializedRuntime(adapter))
    except Exception as exc:
        # If VisionRuntimeManager initialization fails, clean up the runtime
        # and its registration to avoid leaving an unmanaged initialized runtime.
        import logging
        logging.getLogger(__name__).warning(
            "VisionRuntimeManager initialization failed, cleaning up: %s", exc
        )
        try:
            runtime.shutdown()
        except Exception:
            pass
        try:
            manager.unregister_runtime(adapter.name)
        except Exception:
            pass
        return None, None

    return adapter, vision_manager


def build_ai_model_services(
    settings: Optional[Settings] = None,
) -> AiModelServices:
    """Build the shared AI model services for the given settings.

    ``settings.models_dir`` is used as the storage root when configured; when
    unset, :class:`ModelStorage` applies its platform default.  Unavailable or
    invalid storage is reported as :class:`AiModelServicesUnavailableError`
    rather than falling back to a guessed location.
    """
    config = default_settings if settings is None else settings
    models_dir = getattr(config, "models_dir", None)
    try:
        storage = ModelStorage(root=models_dir) if models_dir else ModelStorage()
    except (ValueError, OSError) as exc:
        raise AiModelServicesUnavailableError(
            f"Model storage is unavailable: {exc}"
        ) from exc

    registry = ModelRegistry()
    manager = ModelManager(model_registry=registry)
    # Trusted, code-controlled entries only; the catalogue is currently empty.
    register_catalogue(registry)

    # Register Florence-2 image_description capability for BALANCED profile.
    # This makes the model discoverable via ModelManager.get_capability_descriptor()
    # and enables can_execute_capability() to find the runtime.
    florence2_descriptor = registry.find("microsoft.florence-2-base")
    if florence2_descriptor is not None:
        manager.register_capability("image_description", florence2_descriptor, profile=Profile.BALANCED)

    # Initialize and register the vision runtime (non-blocking, optional)
    adapter, vision_manager = _initialize_vision_runtime(manager)

    return AiModelServices(storage=storage, registry=registry, manager=manager, vision_runtime_manager=vision_manager)


def configure_ai_model_services(
    app, settings: Optional[Settings] = None
) -> Optional[AiModelServices]:
    """Construct the services once and attach them to ``app.state``.

    Returns the container, or ``None`` when storage is unavailable.  Failure
    is recorded on ``app.state.ai_models_error`` so application startup is not
    blocked by an optional model subsystem.
    """
    try:
        services = build_ai_model_services(settings)
    except AiModelServicesUnavailableError as exc:
        app.state.ai_models = None
        app.state.ai_models_error = str(exc)
        return None
    app.state.ai_models = services
    app.state.ai_models_error = None
    return services


def get_ai_model_services(request: Request) -> AiModelServices:
    """FastAPI dependency returning the shared AI model services.

    Raises a 503 with the recorded reason when the services could not be
    constructed (for example, missing Android storage configuration).
    """
    services = getattr(request.app.state, "ai_models", None)
    if services is None:
        error = getattr(request.app.state, "ai_models_error", None)
        detail = "AI model services are unavailable"
        if error:
            detail = f"{detail}: {error}"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
        )
    return services
