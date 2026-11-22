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
from backend.ai.interfaces import ModelManager, ModelRegistry
from backend.ai.storage import ModelStorage
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
    return AiModelServices(storage=storage, registry=registry, manager=manager)


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
