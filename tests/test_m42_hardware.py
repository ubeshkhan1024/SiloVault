"""Hardware detection and profile recommendation tests for M4.2.

All platform information is mocked; nothing here depends on the hardware of the
machine running the suite.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.ai.interfaces import (
    BALANCED_MIN_GPU_MEMORY_BYTES,
    BALANCED_MIN_LOGICAL_CORES,
    BALANCED_MIN_TOTAL_RAM_BYTES,
    GpuInfo,
    HardwareDetector,
    HardwareInfo,
    ModelDescriptor,
    ModelManager,
    Profile,
)

GIB = 1024 ** 3
_WINDOWS_CLASS_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _info(**overrides) -> HardwareInfo:
    data = {
        "os_name": "Windows",
        "architecture": "AMD64",
        "cpu_name": "Test CPU",
        "logical_cores": 4,
        "total_ram_bytes": 8 * GIB,
        "gpus": (),
        "npu_available": None,
    }
    data.update(overrides)
    return HardwareInfo(**data)


class _FakeDetector:
    """Detector stub returning fixed information for ModelManager tests."""

    def __init__(self, info: HardwareInfo) -> None:
        self.info = info

    def detect(self) -> HardwareInfo:
        return self.info

    def recommend_profile(self, info=None) -> Profile:
        return HardwareDetector().recommend_profile(
            self.info if info is None else info
        )


class _FakeKey:
    def __init__(self, path, values=None, subkeys=None) -> None:
        self.path = path
        self.values = values or {}
        self.subkeys = subkeys or []


class _FakeWinreg:
    """Minimal winreg stand-in for the Windows display-adapter probe."""

    HKEY_LOCAL_MACHINE = object()

    def __init__(self, keys) -> None:
        self.keys = keys

    def OpenKey(self, parent, name):
        path = name if parent is self.HKEY_LOCAL_MACHINE else f"{parent.path}\\{name}"
        try:
            return self.keys[path]
        except KeyError:
            raise OSError(2, "not found")

    def EnumKey(self, key, index):
        try:
            return key.subkeys[index]
        except IndexError:
            raise OSError(259, "no more data")

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise OSError(2, "value not found")
        return key.values[name], 1

    def CloseKey(self, key):
        pass


# ---------------------------------------------------------------------------
# Windows detection
# ---------------------------------------------------------------------------

def test_windows_detection_with_mocked_platform():
    detector = HardwareDetector(system="Windows", machine="AMD64", os_version="10")
    with patch.object(
        detector, "_detect_windows_cpu", return_value="Intel(R) Core(TM) i7-12700H"
    ), patch.object(
        detector, "_detect_windows_memory", return_value=(32 * GIB, 12 * GIB)
    ), patch.object(
        detector,
        "_detect_windows_gpus",
        return_value=(GpuInfo("NVIDIA GeForce RTX 3060", "NVIDIA", 6 * GIB),),
    ), patch.object(
        detector, "_logical_cores", return_value=16
    ):
        info = detector.detect()

    assert info.os_name == "Windows"
    assert info.os_version == "10"
    assert info.architecture == "AMD64"
    assert info.cpu_name == "Intel(R) Core(TM) i7-12700H"
    assert info.logical_cores == 16
    assert info.total_ram_bytes == 32 * GIB
    assert info.total_ram_gb == 32.0
    assert info.available_ram_gb == 12.0
    assert info.has_gpu is True
    assert info.gpu_name == "NVIDIA GeForce RTX 3060"
    assert info.gpu_vendor == "NVIDIA"
    assert info.gpu_memory_bytes == 6 * GIB
    assert info.npu_available is None

    data = info.to_dict()
    assert data["os"] == "Windows"
    assert data["ram_gb"] == 32.0
    assert data["gpu_memory_gb"] == 6.0


def test_windows_gpu_detection_reads_registry(monkeypatch):
    root = _FakeKey(_WINDOWS_CLASS_KEY, subkeys=["0000", "0001", "Configuration"])
    discrete = _FakeKey(
        f"{_WINDOWS_CLASS_KEY}\\0000",
        values={
            "DriverDesc": "NVIDIA GeForce RTX 4050 Laptop GPU",
            "HardwareInformation.qwMemorySize": 6 * GIB,
        },
    )
    integrated = _FakeKey(
        f"{_WINDOWS_CLASS_KEY}\\0001",
        values={"DriverDesc": "Intel(R) UHD Graphics"},
    )
    configuration = _FakeKey(f"{_WINDOWS_CLASS_KEY}\\Configuration")
    monkeypatch.setitem(
        sys.modules,
        "winreg",
        _FakeWinreg(
            {
                _WINDOWS_CLASS_KEY: root,
                f"{_WINDOWS_CLASS_KEY}\\0000": discrete,
                f"{_WINDOWS_CLASS_KEY}\\0001": integrated,
                f"{_WINDOWS_CLASS_KEY}\\Configuration": configuration,
            }
        ),
    )

    detector = HardwareDetector(system="Windows")
    gpus = detector._detect_windows_gpus()

    assert [gpu.name for gpu in gpus] == [
        "NVIDIA GeForce RTX 4050 Laptop GPU",
        "Intel(R) UHD Graphics",
    ]
    assert gpus[0].vendor == "NVIDIA"
    assert gpus[0].memory_bytes == 6 * GIB
    assert gpus[1].vendor == "Intel"
    assert gpus[1].memory_bytes is None


# ---------------------------------------------------------------------------
# Linux detection
# ---------------------------------------------------------------------------

def test_linux_detection_with_mocked_platform():
    detector = HardwareDetector(system="Linux", machine="x86_64", os_version="6.8.0")
    with patch.object(
        detector, "_detect_linux_cpu", return_value="AMD Ryzen 7 5800X 8-Core Processor"
    ), patch.object(
        detector, "_detect_linux_memory", return_value=(32 * GIB, 20 * GIB)
    ), patch.object(
        detector,
        "_detect_linux_gpus",
        return_value=(GpuInfo(vendor="NVIDIA", memory_bytes=8 * GIB),),
    ), patch.object(
        detector, "_logical_cores", return_value=16
    ), patch.object(
        detector, "_detect_npu", return_value=None
    ):
        info = detector.detect()

    assert info.os_name == "Linux"
    assert info.architecture == "x86_64"
    assert info.cpu_name == "AMD Ryzen 7 5800X 8-Core Processor"
    assert info.logical_cores == 16
    assert info.total_ram_gb == 32.0
    assert info.available_ram_gb == 20.0
    assert info.gpu_vendor == "NVIDIA"
    assert info.gpu_memory_gb == 8.0
    assert info.npu_available is None


def test_linux_proc_and_sysfs_parsing(tmp_path):
    cpuinfo = _write(
        tmp_path / "cpuinfo",
        "processor\t: 0\nmodel name\t: AMD Ryzen 5 5600H with Radeon Graphics\n",
    )
    meminfo = _write(
        tmp_path / "meminfo",
        "MemTotal:       16316372 kB\nMemAvailable:    8000000 kB\n",
    )

    detector = HardwareDetector(system="Linux")
    assert detector._detect_linux_cpu(cpuinfo) == "AMD Ryzen 5 5600H with Radeon Graphics"

    total, available = detector._detect_linux_memory(meminfo)
    assert total == 16316372 * 1024
    assert available == 8000000 * 1024

    drm_root = tmp_path / "drm"
    device = drm_root / "card1" / "device"
    _write(device / "vendor", "0x10de\n")
    _write(device / "mem_info_vram_total", f"{8 * GIB}\n")
    # Connector node must be ignored.
    (drm_root / "card1-DP-1").mkdir(parents=True)

    gpus = detector._detect_linux_gpus(drm_root)
    assert len(gpus) == 1
    assert gpus[0].vendor == "NVIDIA"
    assert gpus[0].memory_bytes == 8 * GIB
    assert gpus[0].name is None


def test_linux_missing_files_leave_values_unknown(tmp_path):
    detector = HardwareDetector(system="Linux")
    assert detector._detect_linux_cpu(tmp_path / "missing") is None
    assert detector._detect_linux_memory(tmp_path / "missing") == (None, None)
    assert detector._detect_linux_gpus(tmp_path / "missing") == ()


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

def test_missing_optional_dependency_fails_gracefully(monkeypatch):
    detector = HardwareDetector(system="Windows")
    monkeypatch.setitem(sys.modules, "winreg", None)

    assert detector._detect_windows_cpu() is None
    assert detector._detect_windows_gpus() == ()


def test_unknown_hardware_stays_unknown():
    info = HardwareInfo()
    assert info.cpu_name is None
    assert info.logical_cores is None
    assert info.total_ram_bytes is None
    assert info.available_ram_bytes is None
    assert info.gpus == ()
    assert info.gpu_name is None
    assert info.gpu_memory_bytes is None
    assert info.npu_available is None

    data = info.to_dict()
    assert data["cpu"] is None
    assert data["ram_gb"] is None
    assert data["gpu"] is None
    assert data["gpu_memory_gb"] is None

    detector = HardwareDetector(system="Windows", machine="AMD64")
    with patch.object(detector, "_detect_windows_cpu", return_value=None), patch.object(
        detector, "_detect_windows_memory", return_value=(None, None)
    ), patch.object(
        detector, "_detect_windows_gpus", return_value=()
    ), patch.object(
        detector, "_logical_cores", return_value=None
    ):
        detected = detector.detect()

    assert detected.cpu_name is None
    assert detected.total_ram_bytes is None
    assert detected.logical_cores is None
    assert detected.gpus == ()
    assert detected.npu_available is None


def test_unsupported_platform_does_not_fabricate_hardware():
    detector = HardwareDetector(system="Plan9", machine="mips")
    info = detector.detect()

    assert info.os_name == "Plan9"
    assert info.architecture == "mips"
    assert info.cpu_name is None
    assert info.total_ram_bytes is None
    assert info.gpus == ()
    assert info.npu_available is None


def test_android_is_recognised_without_desktop_probes(monkeypatch):
    detector = HardwareDetector(system="Android", machine="aarch64")
    with patch.object(
        detector, "_detect_linux_cpu", side_effect=AssertionError("probe not allowed")
    ), patch.object(
        detector, "_detect_linux_memory", side_effect=AssertionError("probe not allowed")
    ), patch.object(
        detector, "_detect_linux_gpus", side_effect=AssertionError("probe not allowed")
    ):
        info = detector.detect()

    assert info.os_name == "Android"
    assert info.architecture == "aarch64"
    assert info.cpu_name is None
    assert info.total_ram_bytes is None
    assert info.gpus == ()
    assert info.npu_available is None

    # Android environments report ``platform.system() == "Linux"``; the
    # environment markers are what identify them.
    monkeypatch.setenv("ANDROID_ROOT", "/system")
    from_linux = HardwareDetector(system="Linux").detect()
    assert from_linux.os_name == "Android"
    assert from_linux.cpu_name is None


# ---------------------------------------------------------------------------
# NPU
# ---------------------------------------------------------------------------

def test_npu_reported_only_from_trustworthy_signal(tmp_path):
    detector = HardwareDetector()

    assert detector._detect_npu("Windows") is None
    assert detector._detect_npu("Darwin") is None

    accel_root = tmp_path / "accel"
    assert detector._detect_npu("Linux", accel_root=accel_root) is None

    # A bare accelerator node carries no proof that it is an NPU.
    (accel_root / "accel0").mkdir(parents=True)
    assert detector._detect_npu("Linux", accel_root=accel_root) is None


@pytest.mark.parametrize("driver", ["amdxdna", "intel_vpu", "ivpu"])
def test_npu_detected_from_known_npu_driver(tmp_path, driver):
    accel_root = tmp_path / "accel"
    _write(accel_root / "accel0" / "device" / "uevent", f"DRIVER={driver}\n")

    assert HardwareDetector()._detect_npu("Linux", accel_root=accel_root) is True


def test_generic_accelerator_is_not_reported_as_npu(tmp_path):
    accel_root = tmp_path / "accel"
    # An accelerator that is not a known NPU driver stays unknown.
    _write(
        accel_root / "accel0" / "device" / "uevent",
        "DRIVER=some_generic_accelerator\n",
    )

    assert HardwareDetector()._detect_npu("Linux", accel_root=accel_root) is None


def test_npu_ignores_unrelated_accelerator_devices(tmp_path):
    accel_root = tmp_path / "accel"
    _write(accel_root / "accel0" / "device" / "uevent", "DRIVER=generic_accel\n")
    _write(accel_root / "accel1" / "device" / "uevent", "DRIVER=amdxdna\n")

    assert HardwareDetector()._detect_npu("Linux", accel_root=accel_root) is True


# ---------------------------------------------------------------------------
# Profile recommendation
# ---------------------------------------------------------------------------

def test_recommend_profile_conservative_when_hardware_unknown():
    detector = HardwareDetector()

    assert detector.recommend_profile(HardwareInfo()) == Profile.EFFICIENT
    # Capable CPU/RAM without known GPU memory is still not enough.
    assert (
        detector.recommend_profile(
            _info(logical_cores=32, total_ram_bytes=64 * GIB)
        )
        == Profile.EFFICIENT
    )
    # Missing core count is likewise treated conservatively.
    assert (
        detector.recommend_profile(
            _info(
                logical_cores=None,
                total_ram_bytes=32 * GIB,
                gpus=(GpuInfo("GPU", "NVIDIA", 8 * GIB),),
            )
        )
        == Profile.EFFICIENT
    )


def test_recommend_profile_efficient_for_limited_hardware():
    detector = HardwareDetector()

    limited = _info(
        logical_cores=4,
        total_ram_bytes=8 * GIB,
        gpus=(GpuInfo("Intel(R) Iris(R) Xe Graphics", "Intel"),),
    )
    assert detector.recommend_profile(limited) == Profile.EFFICIENT

    # Strong GPU but RAM just below the BALANCED minimum.
    assert (
        detector.recommend_profile(
            _info(
                logical_cores=16,
                total_ram_bytes=BALANCED_MIN_TOTAL_RAM_BYTES - 1,
                gpus=(GpuInfo("GPU", "NVIDIA", BALANCED_MIN_GPU_MEMORY_BYTES),),
            )
        )
        == Profile.EFFICIENT
    )

    # Plenty of RAM but not enough cores.
    assert (
        detector.recommend_profile(
            _info(
                logical_cores=BALANCED_MIN_LOGICAL_CORES - 1,
                total_ram_bytes=32 * GIB,
                gpus=(GpuInfo("GPU", "NVIDIA", 8 * GIB),),
            )
        )
        == Profile.EFFICIENT
    )


def test_recommend_profile_balanced_for_capable_hardware():
    detector = HardwareDetector()
    capable = _info(
        logical_cores=BALANCED_MIN_LOGICAL_CORES,
        total_ram_bytes=BALANCED_MIN_TOTAL_RAM_BYTES,
        gpus=(GpuInfo("GPU", "NVIDIA", BALANCED_MIN_GPU_MEMORY_BYTES),),
    )
    assert detector.recommend_profile(capable) == Profile.BALANCED


def test_recommend_profile_uses_detected_hardware():
    detector = HardwareDetector(system="Windows")
    capable = _info(
        logical_cores=12,
        total_ram_bytes=32 * GIB,
        gpus=(GpuInfo("GPU", "NVIDIA", 8 * GIB),),
    )
    with patch.object(detector, "detect", return_value=capable):
        assert detector.recommend_profile() == Profile.BALANCED


def test_model_manager_recommendation_never_overrides_manual_profile():
    capable = _info(
        logical_cores=16,
        total_ram_bytes=32 * GIB,
        gpus=(GpuInfo("GPU", "NVIDIA", 8 * GIB),),
    )
    manager = ModelManager(detector=_FakeDetector(capable))
    manager.set_profile(Profile.EFFICIENT)

    assert manager.recommend_profile() == Profile.BALANCED
    assert manager.get_profile() == Profile.EFFICIENT

    limited = _info(logical_cores=2, total_ram_bytes=4 * GIB)
    limited_manager = ModelManager(detector=_FakeDetector(limited))
    limited_manager.set_profile(Profile.BALANCED)

    assert limited_manager.recommend_profile() == Profile.EFFICIENT
    assert limited_manager.get_profile() == Profile.BALANCED


def test_model_manager_detect_hardware_uses_injected_detector():
    capable = _info(
        logical_cores=16,
        total_ram_bytes=32 * GIB,
        gpus=(GpuInfo("GPU", "NVIDIA", 8 * GIB),),
    )
    manager = ModelManager(detector=_FakeDetector(capable))

    assert manager.detect_hardware() is capable


def test_profile_scoped_capability_resolution_unchanged():
    manager = ModelManager(detector=_FakeDetector(HardwareInfo()))
    efficient = ModelDescriptor(
        name="text_classifier_efficient", version="1.0", modalities=("text",)
    )
    balanced = ModelDescriptor(
        name="text_classifier_balanced", version="1.0", modalities=("text",)
    )
    manager.register_capability(
        "text_classification", efficient, profile=Profile.EFFICIENT
    )
    manager.register_capability(
        "text_classification", balanced, profile=Profile.BALANCED
    )

    manager.set_profile(Profile.EFFICIENT)
    manager.recommend_profile()
    assert manager.get_capability_descriptor("text_classification") is efficient
    assert manager.get_profile() == Profile.EFFICIENT

    manager.set_profile(Profile.BALANCED)
    manager.recommend_profile()
    assert manager.get_capability_descriptor("text_classification") is balanced
    assert manager.get_profile() == Profile.BALANCED

def test_set_profile_normalizes_string_to_enum():
    manager = ModelManager()

    manager.set_profile("balanced")

    assert manager.get_profile() is Profile.BALANCED
    assert isinstance(manager.get_profile(), Profile)


def test_set_profile_rejects_invalid_values():
    manager = ModelManager()

    for invalid in ("invalid", 123, None):
        with pytest.raises(ValueError, match="Invalid profile"):
            manager.set_profile(invalid)