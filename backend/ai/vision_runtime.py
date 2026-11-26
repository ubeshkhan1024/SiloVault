# Vision runtime interface for local Florence-2 integration.
#
# This module defines the runtime protocol that will be implemented by the actual
# Florence-2 runtime.  It separates model installation status from runtime
# availability and provides the clean interface required by the AI task
# execution system.

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union, runtime_checkable

from backend.ai.interfaces import (
    ModelDescriptor,
    ModelValidationError,
    Profile,
)

logger = logging.getLogger(__name__)


class RuntimeState(Enum):
    """Lifecycle states for a vision runtime instance."""
    INITIALIZING = "initializing"
    READY = "ready"
    LOADING = "loading"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SHUTTING_DOWN = "shutting_down"
    SHUTDOWN = "shutdown"


class RuntimeCapability(Enum):
    """Capabilities a vision runtime may expose."""
    IMAGE_DESCRIPTION = "image_description"
    TEXT_GENERATION = "text_generation"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class RuntimeStatus:
    """Read-only view of runtime state for external consumers."""
    state: RuntimeState
    model_id: Optional[str] = None
    profile: Optional[str] = None
    capabilities: Tuple[RuntimeCapability, ...] = ()
    memory_bytes: Optional[int] = None
    thread_count: Optional[int] = None
    error: Optional[str] = None
    message: Optional[str] = None


class VisionRuntimeError(RuntimeError):
    """Base exception for vision runtime failures."""
    pass


class RuntimeInitializationError(VisionRuntimeError):
    """Raised when runtime fails to initialize or detect hardware."""
    pass


class ModelNotInstalledError(VisionRuntimeError):
    """Raised when attempting to load a model that is not installed."""
    pass


class ModelIncompatibleError(VisionRuntimeError):
    """Raised when a model is installed but incompatible with this runtime."""
    pass


class RuntimeNotReadyError(VisionRuntimeError):
    """Raised when operation is requested but runtime is not in READY state."""
    pass


class ExecutionError(VisionRuntimeError):
    """Raised when inference execution fails."""
    pass


class CancellationError(VisionRuntimeError):
    """Raised when cancellation cannot be performed."""
    pass


@runtime_checkable
class VisionRuntime(Protocol):
    """Core protocol for local vision runtime implementations."""

    @property
    def name(self) -> str:
        ...

    @property
    def version(self) -> str:
        ...

    def initialize(self, profile: Optional[str] = None) -> None:
        ...

    def is_initialized(self) -> bool:
        ...

    def load_model(self, descriptor: ModelDescriptor) -> None:
        ...

    def is_model_loaded(self) -> bool:
        ...

    def get_loaded_model(self) -> Optional[ModelDescriptor]:
        ...

    def execute_image_description(
        self, image_data: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        ...

    def cancel_execution(self) -> None:
        ...

    def unload_model(self) -> None:
        ...

    def shutdown(self) -> None:
        ...

    def get_status(self) -> RuntimeStatus:
        ...

    def set_cpu_optimization(self, enabled: bool) -> None:
        ...


class VisionRuntimeManager:
    """Manages vision runtime instances and their relationship to models."""

    def __init__(self, model_manager: Any):
        if not hasattr(model_manager, "get_model"):
            raise TypeError("model_manager must provide get_model access")
        self._model_manager = model_manager
        self._runtime: Optional[VisionRuntime] = None
        self._runtime_lock = threading.Lock()
        self._initialization_lock = threading.Lock()
        self._initialized = False
        self._profile: Optional[str] = None

    @property
    def runtime(self) -> Optional[VisionRuntime]:
        return self._runtime

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def initialize_runtime(
        self, runtime_cls: type, profile: Optional[str] = None
    ) -> VisionRuntime:
        with self._initialization_lock:
            if self._initialized and self._runtime is not None:
                if profile is not None and self._profile != profile:
                    logger.warning(
                        "Runtime profile changed after initialization; "
                        "restart required for profile switch"
                    )
                return self._runtime

            # Check if a compatible runtime is already registered in ModelManager
            # This avoids creating duplicate Florence2Runtime instances.
            # The expected runtime name for Florence-2 is "florence2-cpu".
            expected_runtime_name = "florence2-cpu"
            list_runtimes = getattr(self._model_manager, "list_runtimes", None)
            if callable(list_runtimes):
                try:
                    if expected_runtime_name in list_runtimes():
                        get_runtime = getattr(self._model_manager, "get_runtime", None)
                        if callable(get_runtime):
                            existing_runtime = get_runtime(expected_runtime_name)
                            # Verify it implements VisionRuntime protocol (the adapter does)
                            if hasattr(existing_runtime, "execute_image_description"):
                                self._runtime = existing_runtime
                                self._initialized = True
                                self._profile = profile
                                logger.info(
                                    "Vision runtime reused from ModelManager: %s (profile: %s)",
                                    existing_runtime.name,
                                    profile or "default",
                                )
                                return self._runtime
                except KeyError:
                    pass  # Fall through to create new instance

            try:
                runtime = runtime_cls()
            except Exception as exc:
                raise RuntimeInitializationError(
                    f"Failed to instantiate runtime {runtime_cls.__name__}: {exc}"
                ) from exc

            try:
                runtime.initialize(profile=profile)
            except Exception as exc:
                runtime.shutdown()
                raise RuntimeInitializationError(
                    f"Runtime initialization failed: {exc}"
                ) from exc

            if len(self._model_manager.list_models()) > 0:
                compatible_models = self._get_compatible_models(runtime)
                if not compatible_models:
                    runtime.shutdown()
                    raise RuntimeInitializationError(
                        "No compatible models found for this runtime"
                    )

            self._runtime = runtime
            self._initialized = True
            self._profile = profile
            logger.info(
                "Vision runtime initialized: %s (profile: %s)",
                runtime.name,
                profile or "default",
            )
            return runtime

    def get_status(self) -> RuntimeStatus:
        with self._runtime_lock:
            if self._runtime is None:
                return RuntimeStatus(state=RuntimeState.SHUTDOWN)
            return self._runtime.get_status()

    def load_model(self, descriptor: ModelDescriptor) -> None:
        with self._runtime_lock:
            if self._runtime is None:
                raise RuntimeNotReadyError("Runtime not initialized")
            if self._runtime.is_model_loaded():
                raise RuntimeError("A model is already loaded")

            is_model_installed = getattr(self._model_manager, "is_model_installed", None)
            if callable(is_model_installed) and not is_model_installed(descriptor):
                raise ModelNotInstalledError(
                    f"Model '{descriptor.identifier}' is not installed"
                )

            if descriptor.identifier not in self._get_compatible_models(self._runtime):
                raise ModelIncompatibleError(
                    f"Model '{descriptor.identifier}' is not compatible with runtime"
                )

            self._runtime.load_model(descriptor)

    def execute_image_description(
        self, image_data: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        with self._runtime_lock:
            if self._runtime is None:
                raise RuntimeNotReadyError("Runtime not initialized")
            return self._runtime.execute_image_description(
                image_data, mime_type=mime_type, **kwargs
            )

    def cancel(self) -> None:
        with self._runtime_lock:
            if self._runtime is not None:
                self._runtime.cancel_execution()

    def unload_model(self) -> None:
        with self._runtime_lock:
            if self._runtime is not None:
                self._runtime.unload_model()

    def shutdown(self) -> None:
        with self._initialization_lock:
            if self._runtime is not None:
                try:
                    self._runtime.shutdown()
                except Exception:
                    pass
                self._runtime = None
                self._initialized = False
                self._profile = None

    def _get_compatible_models(self, runtime: VisionRuntime) -> set[str]:
        compatible = set()
        for model in self._model_manager.list_models():
            if "vision" not in model.modalities:
                continue
            if not hasattr(runtime, "execute_image_description"):
                continue
            compatible.add(model.identifier)
        return compatible


def get_vision_execution_service(manager: Any, vision_manager: Optional[VisionRuntimeManager] = None) -> Any:
    """Return an execution service bridge for vision tasks.

    If *vision_manager* is provided, it is used directly (production path).
    Otherwise, a new VisionRuntimeManager is created (test/backward-compat path).
    """
    if vision_manager is None:
        vision_manager = VisionRuntimeManager(manager)

    class _VisionExecutionBridge:
        def __init__(self, vision_manager: VisionRuntimeManager):
            self._vision_manager = vision_manager

        def describe_image(
            self, image: bytes, mime_type: Optional[str] = None, **kwargs
        ) -> Dict[str, Any]:
            return self._vision_manager.execute_image_description(
                image, mime_type=mime_type, **kwargs
            )

        def get_status(self) -> RuntimeStatus:
            return self._vision_manager.get_status()

        def cancel(self) -> None:
            self._vision_manager.cancel()

        def load_model(self, descriptor: ModelDescriptor) -> None:
            self._vision_manager.load_model(descriptor)

        def unload_model(self) -> None:
            self._vision_manager.unload_model()

    return _VisionExecutionBridge(vision_manager)
