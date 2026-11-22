"""M4.2 AI foundation core interfaces and registries.

This module implements the minimal foundation required for M4.2:

* :class:`InferenceRuntime` – core runtime protocol; optional text generation,
  embedding, and classification protocols describe capability-specific support.
* :class:`ModelDescriptor` – lightweight descriptor for a model.
* :class:`CapabilityRegistry` – per-profile registry of capability names to model
  descriptors.
* :class:`ModelRegistry` – catalogue of model metadata keyed by stable model
  identifier.
* :class:`Profile` – Efficient and Balanced profile definitions.
* :class:`HardwareInfo` – immutable description of detected hardware.
* :class:`HardwareDetector` – standard-library hardware detection and profile
  recommendation.
* :class:`ModelManager` – central registry for runtimes, models, and profile handling.

Only the skeleton is implemented – no actual model loading or runtime
implementation.  This keeps the code safe for the audit and preserves
existing functionality.
"""

from __future__ import annotations

import ctypes
import math
import os
import platform
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple, Union, runtime_checkable
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Runtime protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class InferenceRuntime(Protocol):
    """Core protocol every inference runtime must satisfy.

    This covers registration and availability only.  Optional capabilities are
    expressed by the separate structural protocols below, so a runtime only
    implements the operations it actually supports.
    """

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def is_available(self) -> bool: ...

    def supported_modalities(self) -> List[str]: ...


@runtime_checkable
class TextGenerationRuntime(Protocol):
    """Structural protocol for runtimes that can generate text."""

    def generate(self, prompt: str, **kwargs) -> str: ...


@runtime_checkable
class EmbeddingRuntime(Protocol):
    """Structural protocol for runtimes that can produce embeddings."""

    def embed(self, text: str | List[str]) -> List[float]: ...


@runtime_checkable
class ClassificationRuntime(Protocol):
    """Structural protocol for runtimes that can classify content."""

    def classify(
        self, image: Optional[bytes] = None, text: Optional[str] = None
    ) -> Dict[str, float]: ...


@dataclass(frozen=True)
class ImageDescriptionResult:
    """Structured result of an image description/understanding capability.

    Attributes:
        description: Textual description of the image content.
        tags: Clean string tags describing prominent objects, scenes, or attributes.
        model: Identifier or display name of the model that performed inference.
        confidence: Optional confidence score in the range [0.0, 1.0]. Only present
            if the runtime genuinely supplies confidence; never fabricated.
    """

    description: str
    tags: Tuple[str, ...] = ()
    model: Optional[str] = None
    confidence: Optional[float] = None


@runtime_checkable
class ImageDescriptionRuntime(Protocol):
    """Structural protocol for runtimes that can describe images and generate tags."""

    def describe_image(
        self, image: bytes, mime_type: Optional[str] = None, **kwargs
    ) -> Union[ImageDescriptionResult, Dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Model descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelDescriptor:
    """Lightweight description of a model.

    Attributes are intentionally minimal – only what is needed to identify and
    load a model.  Additional attributes (e.g., download URL, checksum) can be
    added later without breaking the public API.
    """

    name: str
    version: str
    modalities: Tuple[str, ...]  # e.g., ('text', 'vision')
    # Path to the model local directory – may be None until the model is
    # downloaded/installed.
    local_path: Optional[Path] = None
    # Optional metadata for model management and identification.
    model_id: Optional[str] = None  # Unique model identifier
    framework: Optional[str] = None  # e.g. "pytorch", "tensorflow", "onnx"
    supported_formats: Optional[Tuple[str, ...]] = None  # e.g. (".pdf", ".jpg")
    model_size_bytes: Optional[int] = None  # Approximate model size
    checksum: Optional[str] = None  # Hash for verification (e.g. SHA-256)
    licensing: Optional[str] = None  # License identifier or SPDX tag
    hardware_requirements: Optional[Dict[str, str]] = None  # CPU/GPU/memory needs
    # Runtime compatibility metadata
    compatible_runtimes: Optional[Tuple[str, ...]] = None  # Runtime names this model works with
    # Optional download source URL
    download_url: Optional[str] = None  # URL for model download
    # Optional platforms the model supports, e.g. ("Windows", "Linux").
    # ``None`` means no platform restriction is declared.
    supported_platforms: Optional[Tuple[str, ...]] = None

    @property
    def identifier(self) -> str:
        """Stable catalogue identifier for the model.

        ``model_id`` is the canonical identifier.  ``name`` is a fallback for
        descriptors created before model IDs existed, so existing metadata
        keeps working; ``name`` stays the display name and is never used for
        ambiguous lookups.
        """
        return self.model_id or self.name

    # NOTE: installation state is intentionally *not* part of the descriptor.
    # Whether a model is present on disk is separate, mutable state that will
    # be tracked by a future installation tracker; the descriptor only carries
    # static metadata about the model.


# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------

class Profile(Enum):
    EFFICIENT = "efficient"
    BALANCED = "balanced"


# Capability-to-model mapping per profile.
# The keys are capability names; the values are ModelDescriptor names.
PROFILE_CAPABILITIES: Dict[Profile, Dict[str, str]] = {
    Profile.EFFICIENT: {
        "text_classification": "text_classifier_efficient",
    },
    Profile.BALANCED: {
        "text_classification": "text_classifier_balanced",
        "image_description": "image_description_balanced",
        "audio_classification": "audio_classification_balanced",
    },
}


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------

class CapabilityRegistry:
    """Registry mapping capability names to model descriptors.

    Registrations are either profile-agnostic (``profile=None``) or scoped to a
    specific :class:`Profile`.  A profile-scoped registration takes precedence
    over a profile-agnostic one, which is what allows the same capability to
    resolve to a different model in ``EFFICIENT`` than in ``BALANCED``.

    The registry is intentionally lightweight – it is a pure Python data
    structure that can be extended at runtime.  It does **not** perform any
    network I/O or disk access, and it tracks no installation state.
    """

    def __init__(self) -> None:
        # Keyed by profile (None = profile-agnostic) then by capability name.
        self._registry: Dict[Optional[Profile], Dict[str, ModelDescriptor]] = {}

    def register(
        self,
        capability: str,
        descriptor: ModelDescriptor,
        profile: Optional[Profile] = None,
    ) -> None:
        """Register *descriptor* for *capability*, optionally scoped to *profile*."""
        if profile is not None and not isinstance(profile, Profile):
            raise ValueError(f"Invalid profile: {profile!r}")
        bucket = self._registry.setdefault(profile, {})
        if capability in bucket:
            scope = f" for profile '{profile.value}'" if profile is not None else ""
            raise ValueError(f"Capability '{capability}' already registered{scope}")
        bucket[capability] = descriptor

    def resolve(
        self, capability: str, profile: Optional[Profile] = None
    ) -> Optional[ModelDescriptor]:
        """Return the descriptor for *capability*, or ``None`` if absent.

        A registration scoped to *profile* wins; otherwise a profile-agnostic
        registration is used.  Registrations scoped to another profile are
        never returned.
        """
        if profile is not None:
            descriptor = self._registry.get(profile, {}).get(capability)
            if descriptor is not None:
                return descriptor
        return self._registry.get(None, {}).get(capability)

    def get(
        self, capability: str, profile: Optional[Profile] = None
    ) -> ModelDescriptor:
        descriptor = self.resolve(capability, profile)
        if descriptor is None:
            raise KeyError(f"Capability '{capability}' not registered")
        return descriptor

    def list_capabilities(self, profile: Optional[Profile] = None) -> List[str]:
        """List capability names, for *profile* only when one is given."""
        if profile is not None:
            return list(self._registry.get(profile, {}).keys())
        names: List[str] = []
        for bucket in self._registry.values():
            for capability in bucket:
                if capability not in names:
                    names.append(capability)
        return names

    def has_capability(self, capability: str, profile: Optional[Profile] = None) -> bool:
        return self.resolve(capability, profile) is not None

    def __contains__(self, capability: str) -> bool:
        return self.has_capability(capability)


# ---------------------------------------------------------------------------
# Hardware information
# ---------------------------------------------------------------------------

_BYTES_PER_GIB = 1024 ** 3

# Minimum hardware assumed by the BALANCED profile.  These thresholds are
# intentionally conservative; they are *not* benchmark claims and are only used
# to decide whether to suggest a profile.
BALANCED_MIN_TOTAL_RAM_BYTES = 16 * _BYTES_PER_GIB
BALANCED_MIN_LOGICAL_CORES = 8
BALANCED_MIN_GPU_MEMORY_BYTES = 4 * _BYTES_PER_GIB

_WINDOWS_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_WINDOWS_DISPLAY_CLASS_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
)
# Dedicated VRAM is exposed under either name depending on the driver.
_WINDOWS_GPU_MEMORY_VALUES = (
    "HardwareInformation.qwMemorySize",
    "HardwareInformation.MemorySize",
)

_GPU_VENDOR_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("nvidia", "NVIDIA"),
    ("geforce", "NVIDIA"),
    ("quadro", "NVIDIA"),
    ("radeon", "AMD"),
    ("amd", "AMD"),
    ("intel", "Intel"),
    ("arc", "Intel"),
    ("qualcomm", "Qualcomm"),
    ("adreno", "Qualcomm"),
    ("apple", "Apple"),
    ("mali", "ARM"),
    ("arm", "ARM"),
)

# Linux DRM vendor identifiers (see /sys/class/drm/card*/device/vendor).
_LINUX_GPU_VENDORS = {
    "0x10de": "NVIDIA",
    "0x1002": "AMD",
    "0x8086": "Intel",
}

_LINUX_CPUINFO_KEYS = ("model name", "Model", "Hardware", "Processor", "cpu model")

# Linux kernel drivers that expose a neural processing unit through the
# accelerator subsystem.  The accelerator subsystem also hosts non-NPU devices,
# so a device node alone is not evidence of an NPU.
_LINUX_NPU_DRIVERS = frozenset({"amdxdna", "ivpu", "intel_vpu"})


def _bytes_to_gib(value: Optional[int]) -> Optional[float]:
    """Convert bytes to GiB, rounded for display; ``None`` stays ``None``."""
    if value is None:
        return None
    return round(value / _BYTES_PER_GIB, 1)


def _classify_gpu_vendor(name: Optional[str]) -> Optional[str]:
    """Return a vendor name derived from an adapter description, if known."""
    if not name:
        return None
    lowered = name.lower()
    for marker, vendor in _GPU_VENDOR_PATTERNS:
        if marker in lowered:
            return vendor
    return None


def _read_text(path: Path) -> Optional[str]:
    """Read a small text file, returning ``None`` when it is unavailable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _close_key(winreg_module, key) -> None:
    close = getattr(winreg_module, "CloseKey", None)
    if close is None:
        return
    try:
        close(key)
    except OSError:
        pass


def _query_registry_value(winreg_module, key, names: Iterable[str]) -> Optional[int]:
    """Return the first integer-like registry value among *names*."""
    for name in names:
        try:
            value, _ = winreg_module.QueryValueEx(key, name)
        except OSError:
            continue
        if isinstance(value, bytes):
            value = int.from_bytes(value, "little", signed=False)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _read_windows_cpu_name(winreg_module) -> Optional[str]:
    """Read the CPU model string from the Windows registry."""
    try:
        key = winreg_module.OpenKey(winreg_module.HKEY_LOCAL_MACHINE, _WINDOWS_CPU_KEY)
    except OSError:
        return None
    try:
        try:
            value, _ = winreg_module.QueryValueEx(key, "ProcessorNameString")
        except OSError:
            return None
        name = str(value).strip()
        return name or None
    finally:
        _close_key(winreg_module, key)


def _read_windows_gpus(winreg_module) -> Tuple["GpuInfo", ...]:
    """Read display adapters from the Windows display class registry key."""
    try:
        root = winreg_module.OpenKey(
            winreg_module.HKEY_LOCAL_MACHINE, _WINDOWS_DISPLAY_CLASS_KEY
        )
    except OSError:
        return ()

    gpus: List[GpuInfo] = []
    seen: List[str] = []
    try:
        index = 0
        while True:
            try:
                subkey_name = winreg_module.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                subkey = winreg_module.OpenKey(root, subkey_name)
            except OSError:
                continue
            try:
                try:
                    name, _ = winreg_module.QueryValueEx(subkey, "DriverDesc")
                except OSError:
                    continue
                adapter = str(name).strip()
                if not adapter or adapter in seen:
                    continue
                seen.append(adapter)
                gpus.append(
                    GpuInfo(
                        name=adapter,
                        vendor=_classify_gpu_vendor(adapter),
                        memory_bytes=_query_registry_value(
                            winreg_module, subkey, _WINDOWS_GPU_MEMORY_VALUES
                        ),
                    )
                )
            finally:
                _close_key(winreg_module, subkey)
    finally:
        _close_key(winreg_module, root)

    return tuple(gpus)


def _parse_linux_cpu_name(text: Optional[str]) -> Optional[str]:
    """Parse the CPU model from ``/proc/cpuinfo`` content."""
    if not text:
        return None
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        if key.strip() in _LINUX_CPUINFO_KEYS:
            name = value.strip()
            if name:
                return name
    return None


def _parse_linux_memory(text: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """Parse ``(total, available)`` RAM in bytes from ``/proc/meminfo`` content."""
    if not text:
        return None, None
    values: Dict[str, int] = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        if not separator:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            amount = int(parts[0])
        except ValueError:
            continue
        unit = parts[1].lower() if len(parts) > 1 else "kb"
        if unit == "kb":
            amount *= 1024
        elif unit == "mb":
            amount *= 1024 ** 2
        values[key.strip()] = amount
    return values.get("MemTotal"), values.get("MemAvailable")


def _linux_accel_driver_name(device: Path) -> Optional[str]:
    """Return the kernel driver bound to an accelerator device, if readable."""
    text = _read_text(device / "uevent")
    if text:
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DRIVER":
                name = value.strip()
                if name:
                    return name

    driver_link = device / "driver"
    if driver_link.exists():
        try:
            return driver_link.resolve().name or None
        except OSError:
            return None
    return None


def _parse_linux_npus(accel_root: Path) -> Optional[bool]:
    """Return ``True`` only for an accelerator identified as an NPU.

    A device is identified by the kernel driver bound to it; generic
    accelerator devices with an unknown driver leave the result unknown
    (``None``).
    """
    try:
        entries = sorted(accel_root.glob("accel*"))
    except OSError:
        return None

    for entry in entries:
        driver = _linux_accel_driver_name(entry / "device")
        if driver is not None and driver.lower() in _LINUX_NPU_DRIVERS:
            return True
    return None


def _parse_linux_gpus(drm_root: Path) -> Tuple["GpuInfo", ...]:
    """Parse GPU adapters from ``/sys/class/drm`` (connector nodes are skipped)."""
    try:
        entries = sorted(drm_root.glob("card*"))
    except OSError:
        return ()

    gpus: List[GpuInfo] = []
    for entry in entries:
        # Only adapter nodes (card0, card1, ...), not connectors (card0-DP-1).
        if not entry.name[4:].isdigit():
            continue
        device = entry / "device"
        if not device.is_dir():
            continue
        vendor_id = (_read_text(device / "vendor") or "").strip().lower()
        memory_text = (_read_text(device / "mem_info_vram_total") or "").strip()
        memory: Optional[int] = None
        if memory_text.isdigit():
            memory = int(memory_text)
        gpus.append(
            GpuInfo(
                name=None,  # The DRM subsystem exposes no reliable model string.
                vendor=_LINUX_GPU_VENDORS.get(vendor_id),
                memory_bytes=memory,
            )
        )
    return tuple(gpus)


@dataclass(frozen=True)
class GpuInfo:
    """A single detected GPU adapter.

    Only reliably obtainable values are populated.  ``name`` is typically
    unavailable on Linux, and ``memory_bytes`` describes dedicated VRAM and is
    ``None`` when the driver does not expose it (e.g. integrated GPUs).
    """

    name: Optional[str] = None
    vendor: Optional[str] = None
    memory_bytes: Optional[int] = None


@dataclass(frozen=True)
class HardwareInfo:
    """Immutable snapshot of detected hardware.

    Unknown values remain ``None`` (and ``gpus`` stays empty) rather than being
    guessed.  The snapshot is not persisted anywhere; an installation tracker
    or compatibility checker can consume it later.
    """

    os_name: Optional[str] = None
    os_version: Optional[str] = None
    architecture: Optional[str] = None
    cpu_name: Optional[str] = None
    logical_cores: Optional[int] = None
    total_ram_bytes: Optional[int] = None
    available_ram_bytes: Optional[int] = None
    gpus: Tuple[GpuInfo, ...] = ()
    # ``None`` means "not determined", never "absent".
    npu_available: Optional[bool] = None

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpus)

    @property
    def primary_gpu(self) -> Optional[GpuInfo]:
        """Prefer the adapter with the most known dedicated memory."""
        if not self.gpus:
            return None
        with_memory = [gpu for gpu in self.gpus if gpu.memory_bytes is not None]
        if with_memory:
            return max(with_memory, key=lambda gpu: gpu.memory_bytes or 0)
        return self.gpus[0]

    @property
    def gpu_name(self) -> Optional[str]:
        gpu = self.primary_gpu
        return gpu.name if gpu is not None else None

    @property
    def gpu_vendor(self) -> Optional[str]:
        gpu = self.primary_gpu
        return gpu.vendor if gpu is not None else None

    @property
    def gpu_memory_bytes(self) -> Optional[int]:
        known = [gpu.memory_bytes for gpu in self.gpus if gpu.memory_bytes is not None]
        return max(known) if known else None

    @property
    def total_ram_gb(self) -> Optional[float]:
        return _bytes_to_gib(self.total_ram_bytes)

    @property
    def available_ram_gb(self) -> Optional[float]:
        return _bytes_to_gib(self.available_ram_bytes)

    @property
    def gpu_memory_gb(self) -> Optional[float]:
        return _bytes_to_gib(self.gpu_memory_bytes)

    def to_dict(self) -> Dict[str, object]:
        """Return a flat, JSON-safe view of the detected hardware."""
        return {
            "os": self.os_name,
            "os_version": self.os_version,
            "architecture": self.architecture,
            "cpu": self.cpu_name,
            "logical_cores": self.logical_cores,
            "ram_gb": self.total_ram_gb,
            "available_ram_gb": self.available_ram_gb,
            "gpu": self.gpu_name,
            "gpu_vendor": self.gpu_vendor,
            "gpu_memory_gb": self.gpu_memory_gb,
            "npu_available": self.npu_available,
        }


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------

class HardwareDetector:
    """Detect basic hardware without administrator rights or extra dependencies.

    Detection relies on the Python standard library only:

    * **Windows** – ``winreg`` for the CPU name and display adapters, and
      ``GlobalMemoryStatusEx`` through ``ctypes`` for physical memory.
    * **Linux** – ``/proc/cpuinfo``, ``/proc/meminfo`` and ``/sys/class/drm``.
    * **Android** – recognised, but desktop probing is not attempted because a
      phone is not the device that runs the local AI node.

    Every probe fails closed: a value that cannot be determined reliably stays
    ``None``.  Probes are cheap reads only; no enumeration of large devices or
    external processes is performed.

    The constructor overrides exist so tests can describe a platform without
    depending on the machine actually running them.
    """

    def __init__(
        self,
        *,
        system: Optional[str] = None,
        machine: Optional[str] = None,
        os_version: Optional[str] = None,
    ) -> None:
        self._system_override = system
        self._machine_override = machine
        self._os_version_override = os_version

    # -- platform identity -------------------------------------------------

    def _system(self) -> str:
        if self._system_override is not None:
            return self._system_override
        try:
            return platform.system() or ""
        except Exception:  # pragma: no cover - platform module is reliable
            return ""

    def _machine(self) -> Optional[str]:
        if self._machine_override is not None:
            return self._machine_override
        try:
            return platform.machine() or None
        except Exception:  # pragma: no cover - platform module is reliable
            return None

    def _os_version(self) -> Optional[str]:
        if self._os_version_override is not None:
            return self._os_version_override
        try:
            return platform.release() or None
        except Exception:  # pragma: no cover - platform module is reliable
            return None

    def _logical_cores(self) -> Optional[int]:
        try:
            count = os.cpu_count()
        except Exception:  # pragma: no cover - os.cpu_count is reliable
            return None
        if not count or count < 1:
            return None
        return int(count)

    @staticmethod
    def _is_android(system: str) -> bool:
        if system == "Android" or sys.platform == "android":
            return True
        # Android Python builds and embedded interpreters normally set these.
        return "ANDROID_ROOT" in os.environ or "ANDROID_DATA" in os.environ

    # -- Windows probes ----------------------------------------------------

    def _detect_windows_cpu(self) -> Optional[str]:
        try:
            import winreg
        except ImportError:
            return None
        try:
            return _read_windows_cpu_name(winreg)
        except (OSError, ValueError):
            return None

    def _detect_windows_memory(self) -> Tuple[Optional[int], Optional[int]]:
        """Return ``(total, available)`` physical RAM in bytes."""
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        except AttributeError:
            # Not running on Windows (ctypes.windll only exists there).
            return None, None

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        try:
            succeeded = kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except (AttributeError, OSError):
            return None, None
        if not succeeded:
            return None, None
        total = int(status.ullTotalPhys) or None
        available = int(status.ullAvailPhys) or None
        return total, available

    def _detect_windows_gpus(self) -> Tuple[GpuInfo, ...]:
        try:
            import winreg
        except ImportError:
            return ()
        try:
            return _read_windows_gpus(winreg)
        except (OSError, ValueError):
            return ()

    # -- Linux probes ------------------------------------------------------

    def _detect_linux_cpu(
        self, cpuinfo_path: Path = Path("/proc/cpuinfo")
    ) -> Optional[str]:
        return _parse_linux_cpu_name(_read_text(cpuinfo_path))

    def _detect_linux_memory(
        self, meminfo_path: Path = Path("/proc/meminfo")
    ) -> Tuple[Optional[int], Optional[int]]:
        return _parse_linux_memory(_read_text(meminfo_path))

    def _detect_linux_gpus(
        self, drm_root: Path = Path("/sys/class/drm")
    ) -> Tuple[GpuInfo, ...]:
        return _parse_linux_gpus(drm_root)

    # -- accelerators ------------------------------------------------------

    def _detect_npu(self, system: str, accel_root: Path = Path("/dev/accel")) -> Optional[bool]:
        """Report an NPU only from a trustworthy platform signal.

        On Linux, an accelerator node under ``/dev/accel`` is only reported as
        an NPU when its kernel driver is a known NPU driver.  Generic
        accelerator devices, missing driver information, an absent node (the
        driver may simply not be loaded) and every non-Linux platform all stay
        unknown (``None``) instead of being guessed.
        """
        if system != "Linux":
            return None
        try:
            return _parse_linux_npus(accel_root)
        except OSError:
            return None

    # -- public API --------------------------------------------------------

    def detect(self) -> HardwareInfo:
        """Detect hardware and return a :class:`HardwareInfo` snapshot."""
        system = self._system()
        machine = self._machine()
        os_version = self._os_version()

        if self._is_android(system):
            return HardwareInfo(
                os_name="Android", os_version=os_version, architecture=machine
            )

        if system == "Windows":
            cpu_name = self._detect_windows_cpu()
            total_ram, available_ram = self._detect_windows_memory()
            gpus = self._detect_windows_gpus()
        elif system == "Linux":
            cpu_name = self._detect_linux_cpu()
            total_ram, available_ram = self._detect_linux_memory()
            gpus = self._detect_linux_gpus()
        else:
            # Unsupported desktop platform: report only what is safe to know.
            return HardwareInfo(
                os_name=system or None,
                os_version=os_version,
                architecture=machine,
                logical_cores=self._logical_cores(),
            )

        return HardwareInfo(
            os_name=system,
            os_version=os_version,
            architecture=machine,
            cpu_name=cpu_name,
            logical_cores=self._logical_cores(),
            total_ram_bytes=total_ram,
            available_ram_bytes=available_ram,
            gpus=gpus,
            npu_available=self._detect_npu(system),
        )

    def recommend_profile(self, info: Optional[HardwareInfo] = None) -> Profile:
        """Suggest a :class:`Profile` for *info* (detected when omitted).

        BALANCED is suggested only when total RAM, logical core count and known
        dedicated GPU memory all meet the documented minimums.  Any missing
        value falls back to the conservative EFFICIENT profile.  The
        recommendation is advisory: it never changes any selected profile.
        """
        if info is None:
            info = self.detect()

        total_ram = info.total_ram_bytes
        cores = info.logical_cores
        gpu_memory = info.gpu_memory_bytes
        if total_ram is None or cores is None or gpu_memory is None:
            return Profile.EFFICIENT
        if (
            total_ram >= BALANCED_MIN_TOTAL_RAM_BYTES
            and cores >= BALANCED_MIN_LOGICAL_CORES
            and gpu_memory >= BALANCED_MIN_GPU_MEMORY_BYTES
        ):
            return Profile.BALANCED
        return Profile.EFFICIENT


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

class ModelValidationError(ValueError):
    """Raised when model metadata is invalid or cannot be identified."""


def _validate_model_identifier(identifier: Optional[str]) -> None:
    if not isinstance(identifier, str):
        raise ModelValidationError("model identifier must be a string")
    if not identifier.strip():
        raise ModelValidationError("model identifier must not be empty")
    if identifier != identifier.strip():
        raise ModelValidationError(
            "model identifier must not have leading or trailing whitespace"
        )
    if len(identifier) > 200:
        raise ModelValidationError("model identifier must be at most 200 characters")
    if any(character.isspace() for character in identifier):
        raise ModelValidationError("model identifier must not contain whitespace")
    if any(ord(character) < 32 for character in identifier):
        raise ModelValidationError(
            "model identifier must not contain control characters"
        )


def _validate_required_text(value, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError(f"{field} must be a non-empty string")


def _validate_optional_text(value, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError(f"{field} must be a non-empty string when provided")


def _validate_text_tuple(value, field: str, required: bool = False) -> None:
    if value is None:
        if required:
            raise ModelValidationError(f"{field} must contain at least one value")
        return
    if isinstance(value, str) or not isinstance(value, tuple):
        raise ModelValidationError(f"{field} must be a tuple of strings")
    if not value:
        # An empty declaration is meaningless; use None for "not provided".
        raise ModelValidationError(
            f"{field} must contain at least one value when provided"
        )
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ModelValidationError(f"{field} entries must be non-empty strings")


def _validate_optional_positive_int(value, field: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelValidationError(f"{field} must be a positive integer")


def _validate_checksum(value) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError("checksum must be a non-empty string when provided")
    body = value.strip()
    if ":" in body:
        algorithm, _, body = body.partition(":")
        if not algorithm.strip():
            raise ModelValidationError("checksum algorithm must not be empty")
    if len(body) < 16 or any(
        character not in "0123456789abcdefABCDEF" for character in body
    ):
        raise ModelValidationError(
            "checksum must be a hexadecimal digest, optionally prefixed by an algorithm"
        )


def _validate_hardware_requirements(value) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ModelValidationError(
            "hardware_requirements must be a mapping of strings to strings"
        )
    for key, requirement in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ModelValidationError(
                "hardware_requirements keys must be non-empty strings"
            )
        if not isinstance(requirement, str) or not requirement.strip():
            raise ModelValidationError(
                "hardware_requirements values must be non-empty strings"
            )


def _validate_download_url(value) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError("download_url must be a non-empty string")
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ModelValidationError("download_url must be an absolute http(s) URL")


class ModelRegistry:
    """Catalogue of model metadata keyed by a stable model identifier.

    Responsibilities are deliberately narrow: this registry stores and
    validates static metadata.  It does not load models, track installation
    state, perform inference, or decide which capability maps to which model –
    that mapping stays in :class:`CapabilityRegistry`, wired in by
    :class:`ModelManager`.

    Identity comes from :attr:`ModelDescriptor.identifier` (``model_id``,
    falling back to ``name`` for older descriptors).  Display names are never
    used for ambiguous lookup.
    """

    def __init__(
        self, capability_registry: Optional[CapabilityRegistry] = None
    ) -> None:
        self._models: Dict[str, ModelDescriptor] = {}
        self._capability_registry = capability_registry

    def bind_capability_registry(
        self, capability_registry: CapabilityRegistry
    ) -> None:
        """Attach the capability mapping used by :meth:`list_by_capability`."""
        self._capability_registry = capability_registry

    # -- identity and validation ------------------------------------------

    @staticmethod
    def identifier_of(descriptor: ModelDescriptor) -> str:
        """Return the stable identifier for *descriptor*."""
        return descriptor.identifier

    @staticmethod
    def validate_descriptor(descriptor: ModelDescriptor) -> None:
        """Validate static model metadata.

        Raises :class:`ModelValidationError` for invalid values.  Optional
        metadata may stay ``None``; nothing is inferred or filled in.
        """
        if not isinstance(descriptor, ModelDescriptor):
            raise ModelValidationError(
                "descriptor must be a ModelDescriptor instance"
            )
        _validate_model_identifier(descriptor.identifier)
        _validate_required_text(descriptor.name, "name")
        _validate_required_text(descriptor.version, "version")
        _validate_text_tuple(descriptor.modalities, "modalities", required=True)
        _validate_text_tuple(descriptor.supported_formats, "supported_formats")
        _validate_text_tuple(descriptor.compatible_runtimes, "compatible_runtimes")
        _validate_text_tuple(descriptor.supported_platforms, "supported_platforms")
        _validate_optional_positive_int(
            descriptor.model_size_bytes, "model_size_bytes"
        )
        _validate_checksum(descriptor.checksum)
        _validate_optional_text(descriptor.licensing, "licensing")
        _validate_hardware_requirements(descriptor.hardware_requirements)
        _validate_download_url(descriptor.download_url)

    # -- registration ------------------------------------------------------

    def register(self, descriptor: ModelDescriptor) -> str:
        """Validate and catalogue *descriptor*, returning its identifier."""
        self.validate_descriptor(descriptor)
        identifier = descriptor.identifier
        if identifier in self._models:
            raise ModelValidationError(
                f"Model '{identifier}' is already registered"
            )
        self._models[identifier] = descriptor
        return identifier

    # -- retrieval ---------------------------------------------------------

    def get(self, model_id: str) -> ModelDescriptor:
        """Return the model registered as *model_id*; raises ``KeyError``."""
        try:
            return self._models[model_id]
        except KeyError:
            raise KeyError(f"Model '{model_id}' is not registered")

    def find(self, model_id: str) -> Optional[ModelDescriptor]:
        """Return the model registered as *model_id*, or ``None``."""
        return self._models.get(model_id)

    def find_by_name(self, name: str) -> Optional[ModelDescriptor]:
        """Return the model with display *name* only when it is unambiguous."""
        matches = [model for model in self._models.values() if model.name == name]
        return matches[0] if len(matches) == 1 else None

    def is_registered(self, model_id: str) -> bool:
        return model_id in self._models

    def __contains__(self, model_id: str) -> bool:
        return self.is_registered(model_id)

    def __len__(self) -> int:
        return len(self._models)

    # -- listing -----------------------------------------------------------

    def list_models(self) -> List[ModelDescriptor]:
        """Return a new list of catalogued descriptors in insertion order."""
        return list(self._models.values())

    def list_ids(self) -> List[str]:
        """Return a new list of registered model identifiers."""
        return list(self._models.keys())

    def list_by_modality(self, modality: str) -> List[ModelDescriptor]:
        """List models declaring *modality* (case-insensitive)."""
        wanted = modality.casefold()
        return [
            model
            for model in self._models.values()
            if any(entry.casefold() == wanted for entry in model.modalities)
        ]

    def list_by_format(self, file_format: str) -> List[ModelDescriptor]:
        """List models declaring support for *file_format* (case-insensitive)."""
        wanted = file_format.casefold()
        return [
            model
            for model in self._models.values()
            if model.supported_formats
            and any(entry.casefold() == wanted for entry in model.supported_formats)
        ]

    def list_by_runtime(self, runtime_name: str) -> List[ModelDescriptor]:
        """List models whose metadata declares *runtime_name* as compatible."""
        return [
            model
            for model in self._models.values()
            if model_runtime_compatibility(model, runtime_name) is True
        ]

    def list_by_capability(
        self, capability: str, profile: Optional[Profile] = None
    ) -> List[ModelDescriptor]:
        """List the catalogued model mapped to *capability* for *profile*.

        Capability mapping is owned by :class:`CapabilityRegistry`; this method
        only exposes the mapped model when it is also catalogued here.  With no
        capability registry attached the result is empty.
        """
        if self._capability_registry is None:
            return []
        mapped = self._capability_registry.resolve(capability, profile)
        if mapped is None:
            return []
        catalogued = self._models.get(mapped.identifier)
        return [catalogued] if catalogued is not None else []

    def list_for_platform(self, platform: str) -> List[ModelDescriptor]:
        """List models whose platform metadata allows *platform*."""
        return [
            model
            for model in self._models.values()
            if model_platform_compatibility(model, platform)
        ]

    def list_compatible_with_hardware(
        self, hardware: HardwareInfo
    ) -> List[ModelDescriptor]:
        """List models whose known requirements are met by *hardware*.

        Models with unknown requirements or requirements that cannot be
        evaluated against *hardware* are excluded: compatibility is never
        assumed.
        """
        return [
            model
            for model in self._models.values()
            if model_hardware_compatibility(model, hardware) is True
        ]

    # -- compatibility queries --------------------------------------------

    def runtime_compatibility(
        self, model_id: str, runtime_name: str
    ) -> Optional[bool]:
        """``True``/``False`` when declared, ``None`` when unknown."""
        return model_runtime_compatibility(self.get(model_id), runtime_name)

    def hardware_compatibility(
        self, model_id: str, hardware: HardwareInfo
    ) -> Optional[bool]:
        """``True``/``False`` when determinable, ``None`` when unknown."""
        return model_hardware_compatibility(self.get(model_id), hardware)

    def platform_compatibility(self, model_id: str, platform: str) -> bool:
        return model_platform_compatibility(self.get(model_id), platform)


# ---------------------------------------------------------------------------
# Compatibility helpers
# ---------------------------------------------------------------------------

def model_runtime_compatibility(
    descriptor: ModelDescriptor, runtime_name: str
) -> Optional[bool]:
    """Return whether *descriptor* declares *runtime_name* as compatible.

    ``None`` means the descriptor carries no runtime compatibility metadata.
    """
    runtimes = descriptor.compatible_runtimes
    if runtimes is None:
        return None
    return runtime_name in runtimes


def model_platform_compatibility(
    descriptor: ModelDescriptor, platform: str
) -> bool:
    """Return whether *descriptor* is unrestricted for or supports *platform*.

    ``supported_platforms=None`` means no platform restriction is declared, so
    the model is treated as platform-agnostic.
    """
    supported = descriptor.supported_platforms
    if supported is None:
        return True
    wanted = platform.strip().casefold()
    return any(entry.strip().casefold() == wanted for entry in supported)


# Keys understood in ``ModelDescriptor.hardware_requirements``.  Values are the
# corresponding :class:`HardwareInfo` attribute names.  Unrecognized keys make
# the result unknown instead of being ignored.
_HARDWARE_REQUIREMENT_FIELDS = {
    "ram_gb": "total_ram_bytes",
    "min_ram_gb": "total_ram_bytes",
    "total_ram_gb": "total_ram_bytes",
    "available_ram_gb": "available_ram_bytes",
    "min_available_ram_gb": "available_ram_bytes",
    "cores": "logical_cores",
    "logical_cores": "logical_cores",
    "min_cores": "logical_cores",
    "min_logical_cores": "logical_cores",
    "gpu_memory_gb": "gpu_memory_bytes",
    "min_gpu_memory_gb": "gpu_memory_bytes",
    "vram_gb": "gpu_memory_bytes",
}

# Fields compared in bytes; comparisons use exact detected values so display
# rounding can never turn a just-below machine into a compatible one.
_MEMORY_REQUIREMENT_FIELDS = frozenset(
    {"total_ram_bytes", "available_ram_bytes", "gpu_memory_bytes"}
)

_REQUIREMENT_PATTERN = re.compile(
    r"^\s*(?P<number>[0-9]+(?:\.[0-9]+)?)\s*"
    r"(?P<unit>gb|gib|mb|mib|cores?|threads?)?\s*$",
    re.IGNORECASE,
)

_MEMORY_REQUIREMENT_UNITS = (None, "gb", "gib", "mb", "mib")
_CORE_REQUIREMENT_UNITS = (None, "core", "cores", "thread", "threads")


def _parse_hardware_requirement(raw_value, field: str) -> Optional[float]:
    """Parse a requirement value into the unit used by *field*.

    Returns ``None`` when the value is not parseable for that field.
    """
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, (int, float)):
        number = float(raw_value)
        if not math.isfinite(number):
            return None
        unit = None
    elif isinstance(raw_value, str):
        match = _REQUIREMENT_PATTERN.match(raw_value)
        if match is None:
            return None
        number = float(match.group("number"))
        unit = match.group("unit")
        unit = unit.lower() if unit is not None else None
    else:
        return None

    if field == "logical_cores":
        if unit not in _CORE_REQUIREMENT_UNITS:
            return None
        return number

    if field in _MEMORY_REQUIREMENT_FIELDS:
        if unit not in _MEMORY_REQUIREMENT_UNITS:
            return None
        if unit in ("mb", "mib"):
            return number * 1024 ** 2
        return number * 1024 ** 3

    return None


def _evaluate_hardware_requirement(
    key: str, raw_value, hardware: HardwareInfo
) -> Optional[bool]:
    normalized = key.strip().lower().replace("-", "_")
    field = _HARDWARE_REQUIREMENT_FIELDS.get(normalized)
    if field is None:
        return None
    required = _parse_hardware_requirement(raw_value, field)
    if required is None:
        return None
    actual = getattr(hardware, field)
    if actual is None:
        return None
    return actual >= required


def model_hardware_compatibility(
    descriptor: ModelDescriptor, hardware: HardwareInfo
) -> Optional[bool]:
    """Return whether *descriptor*'s known requirements fit *hardware*.

    ``True`` only when every requirement is known, evaluable and met.
    ``False`` when a requirement is demonstrably not met.  ``None`` when the
    descriptor states no usable requirements, a requirement cannot be
    evaluated, or the hardware value needed is unknown – unknown hardware is
    never treated as compatibility.
    """
    requirements = descriptor.hardware_requirements
    if not requirements:
        return None

    unknown = False
    for key, raw_value in requirements.items():
        result = _evaluate_hardware_requirement(key, raw_value, hardware)
        if result is False:
            return False
        if result is None:
            unknown = True
    return None if unknown else True


# ---------------------------------------------------------------------------
# Model manager
# ---------------------------------------------------------------------------

class ModelManager:
    """Coordination layer for runtimes, model metadata and profiles.

    The manager owns a :class:`ModelRegistry` (catalogue), a
    :class:`CapabilityRegistry` (capability/profile selection) and the profile
    state.  It is agnostic to the underlying runtime implementation.
    """

    def __init__(
        self,
        detector: HardwareDetector | None = None,
        capability_registry: CapabilityRegistry | None = None,
        model_registry: ModelRegistry | None = None,
    ) -> None:
        self._runtimes: Dict[str, InferenceRuntime] = {}
        self._profile: Profile = Profile.EFFICIENT
        self._detector = detector or HardwareDetector()
        self._runtime_model_map: Dict[str, Dict[str, str]] = {}
        self._capability_registry: CapabilityRegistry = capability_registry or CapabilityRegistry()
        self._model_registry: ModelRegistry = (
            model_registry if model_registry is not None else ModelRegistry()
        )
        self._model_registry.bind_capability_registry(self._capability_registry)

    @property
    def capability_registry(self) -> CapabilityRegistry:
        """Return the integrated CapabilityRegistry."""
        return self._capability_registry

    @property
    def model_registry(self) -> ModelRegistry:
        """Return the integrated ModelRegistry (model catalogue)."""
        return self._model_registry

    def register_capability(
        self,
        capability: str,
        descriptor: ModelDescriptor,
        profile: Optional[Profile] = None,
    ) -> None:
        """Register a capability in the integrated CapabilityRegistry.

        Passing *profile* scopes the registration to that profile, so the same
        capability can map to a different descriptor per profile.  Without a
        profile the registration applies to every profile.

        The descriptor is validated and catalogued in the ModelRegistry when it
        is not already registered there.  If the identifier already exists, the
        incoming metadata must match the catalogue entry exactly; conflicting
        metadata raises :class:`ModelValidationError` rather than replacing it.
        The capability mapping always references the canonical catalogue
        descriptor, never a divergent instance.
        """
        self._model_registry.validate_descriptor(descriptor)
        existing = self._model_registry.find(descriptor.identifier)
        if existing is not None:
            if existing != descriptor:
                raise ModelValidationError(
                    f"Model '{descriptor.identifier}' is already registered "
                    "with different metadata"
                )
            canonical = existing
        else:
            # Catalogue first, so the capability mapping can only reference a
            # registered descriptor.
            self._model_registry.register(descriptor)
            canonical = self._model_registry.get(descriptor.identifier)
        self._capability_registry.register(capability, canonical, profile=profile)

    # Runtime management
    def register_runtime(self, runtime: InferenceRuntime) -> None:
        if runtime.name in self._runtimes:
            raise ValueError(f"Runtime '{runtime.name}' already registered")
        self._runtimes[runtime.name] = runtime

    def get_runtime(self, name: str) -> InferenceRuntime:
        try:
            return self._runtimes[name]
        except KeyError:
            raise KeyError(f"Runtime '{name}' not registered")

    def list_runtimes(self) -> List[str]:
        return list(self._runtimes.keys())

    def unregister_runtime(self, runtime_name: str) -> bool:
        """Remove a registered runtime and its model associations.

        Returns ``True`` when the runtime was registered and removed, and
        ``False`` when the name is unknown.  Model descriptors and capability
        registrations are never touched, and no other runtime is affected.
        """
        if runtime_name not in self._runtimes:
            return False
        self._runtimes.pop(runtime_name)
        self._runtime_model_map.pop(runtime_name, None)
        return True

    # Model descriptor management (backed by the ModelRegistry)
    def register_descriptor(self, descriptor: ModelDescriptor) -> str:
        """Validate and catalogue *descriptor*, returning its identifier."""
        return self._model_registry.register(descriptor)

    def get_descriptor(self, model_id: str) -> ModelDescriptor:
        """Return the catalogued model for *model_id* (raises ``KeyError``)."""
        return self._model_registry.get(model_id)

    def list_descriptors(self) -> List[str]:
        """Return the identifiers of all catalogued models."""
        return self._model_registry.list_ids()

    # Model catalogue access
    def get_model(self, model_id: str) -> ModelDescriptor:
        """Return model metadata by its stable identifier."""
        return self._model_registry.get(model_id)

    def find_model(self, model_id: str) -> Optional[ModelDescriptor]:
        """Return model metadata by identifier, or ``None`` when unknown."""
        return self._model_registry.find(model_id)

    def list_models(self) -> List[ModelDescriptor]:
        """Return all catalogued model descriptors."""
        return self._model_registry.list_models()

    def models_for_capability(self, capability: str) -> List[ModelDescriptor]:
        """Return the catalogued model selected for *capability*."""
        return self._model_registry.list_by_capability(capability, self._profile)

    # Profile handling
    def set_profile(self, profile: Profile) -> None:
        try:
            normalized_profile = Profile(profile)
        except (TypeError, ValueError):
            raise ValueError("Invalid profile") from None

        self._profile = normalized_profile

    def get_profile(self) -> Profile:
        return self._profile

    def get_capability_descriptor(self, capability: str) -> Optional[ModelDescriptor]:
        """Return the ModelDescriptor for *capability* under the current profile.

        Returns ``None`` when the capability is not allowed in the current
        profile, or when no descriptor is available for it (graceful fallback).

        Resolution order:
        1. a registration scoped to the current profile,
        2. a profile-agnostic registration,
        3. the descriptor named by ``PROFILE_CAPABILITIES`` for this profile.
        """
        cap_map = PROFILE_CAPABILITIES.get(self._profile, {})
        descriptor_name = cap_map.get(capability)
        if descriptor_name is None:
            return None
        registered = self._capability_registry.resolve(capability, self._profile)
        if registered is not None:
            # Only a descriptor present in the catalogue may be resolved.
            # Orphaned or conflicting direct registry writes fail closed.
            canonical = self._model_registry.find(registered.identifier)
            return canonical if canonical == registered else None
        # ``PROFILE_CAPABILITIES`` refers to display names; an identifier that
        # happens to equal one of those values must not shadow the intended
        # model.  Ambiguous or missing names fall back to ``None``.
        return self._model_registry.find_by_name(descriptor_name)

    # Associate models with compatible runtimes
    def associate_runtime_with_model(self, runtime_name: str, model_id: str) -> None:
        """Associate a runtime with a catalogued model identifier.

        This allows the ModelManager to track which runtime can execute which
        model.  The association is stored in an internal mapping; it does not
        imply that the runtime is currently loaded or available.
        """
        if runtime_name not in self._runtimes:
            raise KeyError(f"Runtime '{runtime_name}' is not registered")
        if not self._model_registry.is_registered(model_id):
            raise KeyError(f"Model descriptor '{model_id}' is not registered")
        # Store the association in a separate mapping
        if runtime_name not in self._runtime_model_map:
            self._runtime_model_map[runtime_name] = {}
        self._runtime_model_map[runtime_name][model_id] = model_id

    def get_models_for_runtime(self, runtime_name: str) -> List[str]:
        """Return the model identifiers associated with the given runtime.

        Returns an empty list if the runtime is not registered or has no
        associated models.
        """
        return list(self._runtime_model_map.get(runtime_name, {}).keys())

    def get_runtime_for_model(self, model_id: str) -> Optional[str]:
        """Return the runtime name associated with the given model identifier.

        Returns ``None`` if the model has no associated runtime.
        """
        for runtime_name, models in self._runtime_model_map.items():
            if model_id in models:
                return runtime_name
        return None

    def get_compatible_runtime_for_capability(self, capability: str) -> Optional[str]:
        """Return a runtime that can execute the given capability under the current profile.

        An explicitly associated runtime is preferred, but it is ignored when
        the descriptor declares it incompatible.  Otherwise a registered
        runtime declared in ``compatible_runtimes`` is used.  Availability is
        not checked here; use :meth:`can_execute_capability` for that.
        """
        descriptor = self.get_capability_descriptor(capability)
        if descriptor is None:
            return None
        runtime_name = self.get_runtime_for_model(descriptor.identifier)
        if runtime_name is not None:
            # Unspecified compatibility (``None``) keeps the association; an
            # explicit ``False`` blocks it.
            if model_runtime_compatibility(descriptor, runtime_name) is not False:
                return runtime_name
        if descriptor.compatible_runtimes:
            for candidate in descriptor.compatible_runtimes:
                if candidate in self._runtimes:
                    return candidate
        return None

    def can_execute_capability(self, capability: str) -> bool:
        """Return ``True`` if the current profile can execute the given capability.

        Accepts either an explicitly associated runtime or a registered runtime
        declared in the model's ``compatible_runtimes``.  The capability must
        also be allowed in the current profile, and the selected runtime must
        report itself as available.  A failing availability probe is treated as
        unavailable.
        """
        runtime_name = self.get_compatible_runtime_for_capability(capability)
        if runtime_name is None:
            return False
        try:
            runtime = self.get_runtime(runtime_name)
        except KeyError:
            return False
        try:
            available = runtime.is_available()
        except Exception:
            # The probe itself failed; availability is unknown, so fail closed.
            return False
        return bool(available)

    # Compatibility checks
    def is_model_compatible_with_runtime(
        self, model_id: str, runtime_name: str
    ) -> Optional[bool]:
        """Check *model_id*'s **declared** compatibility with a runtime.

        This is metadata only: it does not check whether the runtime is
        available or operational.  Returns ``None`` when the runtime is not
        registered or when the model has no runtime compatibility metadata.
        Raises ``KeyError`` when the model identifier is unknown.
        """
        if runtime_name not in self._runtimes:
            return None
        return self._model_registry.runtime_compatibility(model_id, runtime_name)

    def is_model_compatible_with_hardware(
        self, model_id: str, hardware: Optional[HardwareInfo] = None
    ) -> Optional[bool]:
        """Check *model_id*'s known requirements against hardware information.

        Detected hardware is used when *hardware* is omitted.  Unknown hardware
        never yields ``True``.
        """
        if hardware is None:
            hardware = self.detect_hardware()
        return self._model_registry.hardware_compatibility(model_id, hardware)

    # Hardware detection and profile recommendation
    def detect_hardware(self) -> HardwareInfo:
        """Return the hardware snapshot from the configured detector."""
        return self._detector.detect()

    def recommend_profile(self) -> Profile:
        """Return the hardware-based profile suggestion.

        Advisory only: the selected profile from :meth:`get_profile` is never
        changed.  Call :meth:`set_profile` explicitly to apply a suggestion.
        """
        return self._detector.recommend_profile()

    # Graceful fallback helpers
    def try_execute(self, capability: str, *args, **kwargs):
        """Resolve a capability and check that a runtime can serve it.

        This performs descriptor resolution and an availability check only.  It
        does **not** run inference, call the runtime, or use ``args``/``kwargs``.
        Returns the resolved descriptor, or ``None`` when the capability is not
        allowed in the current profile, has no usable runtime, or that runtime
        is unavailable (including a failing availability probe).
        """
        descriptor = self.get_capability_descriptor(capability)
        if descriptor is None:
            return None
        runtime_name = self.get_compatible_runtime_for_capability(capability)
        if runtime_name is None:
            return None
        try:
            runtime = self.get_runtime(runtime_name)
        except KeyError:
            return None
        try:
            available = runtime.is_available()
        except Exception:
            # The probe itself failed; availability is unknown, so fail closed.
            return None
        if not available:
            return None
        return descriptor

# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

__all__ = [
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
]