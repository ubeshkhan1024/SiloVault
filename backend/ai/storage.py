"""Local model storage locations for the M4.2 AI foundation.

:class:`ModelStorage` owns the filesystem side of model management: where a
model's files live and whether the expected files are present.  Model metadata
stays in :class:`backend.ai.interfaces.ModelDescriptor` and the catalogue in
:class:`backend.ai.interfaces.ModelRegistry`; this module never mutates
descriptors and never tracks installation state separately.  Installation is
derived from the filesystem on demand.

Nothing is created during import or object construction.  Directories are only
created by an explicit :meth:`ModelStorage.prepare_model_directory` call.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Optional, Union

from backend.ai.interfaces import ModelDescriptor

PathLike = Union[str, os.PathLike]

_WINDOWS_LOCAL_APP_DATA = ("SiloVault", "models")
_LINUX_SHARE_PARTS = ("silovault", "models")


def _detect_system(system: Optional[str], environ: Mapping[str, str]) -> str:
    """Return the platform name used for default path resolution."""
    if system:
        return system
    if sys.platform == "android" or "ANDROID_ROOT" in environ or "ANDROID_DATA" in environ:
        return "Android"
    try:
        name = platform.system()
    except Exception:  # pragma: no cover - platform module is reliable
        name = ""
    return name or sys.platform


def _default_root(system: str, environ: Mapping[str, str], home: Path) -> Path:
    """Return the platform default model storage root.

    Android has no portable shared location, so callers must supply an
    app-private root explicitly instead of this function guessing one.
    """
    if system == "Android":
        raise ValueError(
            "Android requires an explicit app-private model storage root"
        )
    if system == "Windows":
        local_app_data = environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        return base.joinpath(*_WINDOWS_LOCAL_APP_DATA)
    if system == "Linux":
        xdg_data_home = environ.get("XDG_DATA_HOME")
        base = Path(xdg_data_home) if xdg_data_home else home / ".local" / "share"
        return base.joinpath(*_LINUX_SHARE_PARTS)
    raise ValueError(
        f"No default model storage location for platform {system!r}; "
        "pass an explicit root"
    )


class ModelStorage:
    """Resolve and prepare per-model directories inside a storage root.

    Parameters
    ----------
    root:
        Explicit storage root.  When omitted, a platform default is derived
        from ``system``/``environ``/``home``.  The root is resolved but not
        created.
    system:
        Platform name override used for tests (``"Windows"``, ``"Linux"``,
        ``"Android"``).
    environ:
        Environment mapping used for default resolution (defaults to
        ``os.environ``).
    home:
        Home directory used for Linux/Windows fallbacks (defaults to
        ``Path.home()``).
    """

    def __init__(
        self,
        root: Optional[PathLike] = None,
        *,
        system: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
        home: Optional[Path] = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        home_path = Path.home() if home is None else Path(home)
        if root is not None:
            if not str(root).strip():
                raise ValueError("Model storage root must not be empty")
            self._root = Path(root).expanduser().resolve()
        else:
            self._root = _default_root(
                _detect_system(system, environment), environment, home_path
            ).expanduser().resolve()

    @property
    def root(self) -> Path:
        """Return the resolved storage root (never created by this class)."""
        return self._root

    def model_directory(self, identifier: str) -> Path:
        """Return the directory reserved for *identifier*.

        The identifier must be a single safe path component.  The returned path
        is verified to stay inside :attr:`root`.  Nothing is created.
        """
        safe = self._validate_identifier(identifier)
        candidate = (self._root / safe).resolve()
        if candidate.parent != self._root or not candidate.is_relative_to(self._root):
            raise ValueError(f"Model identifier {identifier!r} escapes the storage root")
        return candidate

    def prepare_model_directory(self, identifier: str) -> Path:
        """Create the model directory if needed and return it.

        Directory creation only happens when this method is called explicitly.
        """
        directory = self.model_directory(identifier)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def is_installed(self, descriptor: ModelDescriptor) -> bool:
        """Return whether the model's expected local path exists.

        A descriptor with an explicit ``local_path`` keeps that location.  For
        other descriptors the expected path is the model directory derived from
        the validated identifier.  No filesystem changes are made.
        """
        if descriptor.local_path is not None:
            return Path(descriptor.local_path).exists()
        return self.model_directory(descriptor.identifier).exists()

    @staticmethod
    def _validate_identifier(identifier: str) -> str:
        if not isinstance(identifier, str):
            raise ValueError("Model identifier must be a string")
        if not identifier or identifier != identifier.strip():
            raise ValueError("Model identifier must not be empty or padded")
        if identifier in (".", ".."):
            raise ValueError(f"Unsafe model identifier: {identifier!r}")
        if "/" in identifier or "\\" in identifier:
            raise ValueError(f"Model identifier contains a path separator: {identifier!r}")
        if any(ord(character) < 32 for character in identifier):
            raise ValueError("Model identifier contains control characters")
        # Windows treats ':' and trailing dots specially; neither is a valid
        # model identifier component we want to map onto the filesystem.
        if ":" in identifier or identifier.endswith("."):
            raise ValueError(f"Unsafe model identifier: {identifier!r}")
        return identifier
