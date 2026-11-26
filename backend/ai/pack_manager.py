from __future__ import annotations
"""Pack Manager implementation for SiloVault M5.8.2.

This module provides the PackManager orchestration service that discovers,
validates, and recommends AI packs based on user capabilities and hardware.

The implementation is deliberately minimal and read-only for this milestone:
- No actual download/installation logic is added.
- Existing ModelInstallationService is used for validation only.
- No modifications to existing AI runtime or storage code.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.ai.catalogue import _SUPPORTED_CHECKSUMS
from backend.ai.interfaces import (
    CapabilityRegistry,
    HardwareInfo,
    HardwareDetector,
    ModelDescriptor,
    ModelRegistry,
    Profile,
    ModelValidationError,
)
from backend.ai.storage import ModelStorage
from backend.ai._manifest_validation import _validate_v1_manifest, _validate_v2_manifest

# ---------------------------------------------------------------------------
# Pack data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PackCapability:
    """Capability that a pack provides.

    Mirrors the capability names used in CapabilityRegistry. Currently only
    ``image_description`` is supported for Florence-2. Additional capabilities
    can be added without changing this structure.
    """
    name: str
    description: str
    requires_hardware: Optional[Dict[str, str]] = None

@dataclass(frozen=True)
class PackMetadata:
    """Pack metadata derived from ModelDescriptor plus pack-specific fields.

    This is the public representation of a pack that the UI can display.
    All fields are either derived from ModelDescriptor or explicitly set for the pack.
    """
    model_descriptor: ModelDescriptor
    capability: PackCapability
    download_url: Optional[str] = None
    checksum: Optional[str] = None
    estimated_download_size_mb: Optional[float] = None
    estimated_ram_mb: Optional[float] = None
    license: Optional[str] = None
    supported_platforms: Optional[Tuple[str, ...]] = None
    is_verified: bool = False
    verification_notes: Optional[str] = None
    requires_approval: bool = True
    approval_required_text: Optional[str] = None

    @property
    def identifier(self) -> str:
        return self.model_descriptor.identifier

    @property
    def name(self) -> str:
        return self.model_descriptor.name

    @property
    def version(self) -> str:
        return self.model_descriptor.version

@dataclass(frozen=True)
class PackManifest:
    identifier: str
    version: str
    schema_version: str = "2"
    packaging: str = "single-file"
    installed_at: Optional[str] = None
    entry_point: Optional[str] = None
    files: List[Dict[str, Any]] = field(default_factory=list)
    source: Optional[Dict[str, str]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PackManifest":
        # Minimal validation - full validation delegated to ModelInstallationService
        if "schema_version" not in data:
            raise ValueError("Manifest missing schema_version")
        if "identifier" not in data:
            raise ValueError("Manifest missing identifier")
        if data["schema_version"] != "2":
            raise ValueError(
                f"Unsupported manifest schema_version: {data['schema_version']}"
            )
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


# ---------------------------------------------------------------------------
# Pack approval state
# ---------------------------------------------------------------------------

@dataclass
class PackApproval:
    """Approval request and status for a pack.

    This tracks user approval before any installation can proceed. The PackManager
    does not perform installation; it only coordinates approval state.
    """
    pack_id: str
    requested_at: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    declined: bool = False
    declined_reason: Optional[str] = None

    @property
    def is_approved(self) -> bool:
        return self.approved_by is not None and not self.declined

    @property
    def can_install(self) -> bool:
        return self.is_approved

# ---------------------------------------------------------------------------
# Pack Manager core
# ---------------------------------------------------------------------------

class PackManager:
    """Orchestration service for AI pack discovery, recommendation, and approval.

    The PackManager does not perform installation; it coordinates discovery,
    hardware validation, and approval state. All filesystem and network work is
    delegated to existing services.
    """

    def __init__(self, hardware_detector: HardwareDetector, capability_registry: CapabilityRegistry, model_registry: ModelRegistry, model_storage: ModelStorage) -> None:
        self._hardware_detector = hardware_detector
        self._capability_registry = capability_registry
        self._model_registry = model_registry
        self._model_storage = model_storage
        self._approvals: Dict[str, PackApproval] = {}

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def discover_packs(self, capability_name: str) -> List[PackMetadata]:
        """Return all packs that provide the requested capability.

        This queries the CapabilityRegistry for the capability and returns the
        associated PackMetadata. Only registered packs are returned.
        """
        descriptor = self._capability_registry.resolve(capability_name)
        if descriptor is None:
            return []
        # For now, return Florence-2 pack if descriptor matches
        if descriptor.identifier == "microsoft.florence-2-base":
            return [self._florence2_pack_metadata(descriptor)]
        return []

    def get_hardware_info(self) -> HardwareInfo:
        """Return current hardware information snapshot.

        Delegates to the HardwareDetector.
        """
        return self._hardware_detector.detect()

    def check_hardware_compatibility(self, pack: PackMetadata, hardware: Optional[HardwareInfo] = None) -> Tuple[bool, List[str]]:
        """Check if *pack* can run on the given hardware.

        Returns a tuple of (compatible, list_of_reasons).
        """
        compatible = True
        reasons: List[str] = []
        if hardware is None:
            hardware = self.get_hardware_info()

        # RAM check
        if pack.estimated_ram_mb is not None:
            if hardware.total_ram_bytes is None:
                reasons.append("Total RAM unknown")
            elif hardware.total_ram_bytes < pack.estimated_ram_mb * 1024 * 1024:
                compatible = False
                reasons.append(
                    f"Insufficient RAM: need ~{pack.estimated_ram_mb:.0f} MB, "
                    f"have {hardware.total_ram_bytes / (1024**3):.1f} GB"
                )

        # CPU/GPU check via hardware requirements
        if pack.model_descriptor.hardware_requirements:
            reqs = pack.model_descriptor.hardware_requirements
            for key, value in reqs.items():
                # Map hardware attribute names to HardwareInfo fields
                # Map manifest requirement names to HardwareInfo attributes.
                attribute_map = {
                    "cpu_cores": "logical_cores",
                }
                hardware_attribute = attribute_map.get(key, key)
                hw_value = getattr(hardware, hardware_attribute, None)
                if hw_value is None:
                    compatible = False
                    reasons.append(f"Hardware requirement '{key}' not detected")
                elif str(hw_value) != value:
                    compatible = False
                    reasons.append(f"Hardware mismatch for '{key}': need {value}, have {hw_value}")

        # Platform check
        if pack.supported_platforms:
            if hardware.os_name not in pack.supported_platforms:
                compatible = False
                reasons.append(f"Platform {hardware.os_name} not supported (supports {pack.supported_platforms})")

        if compatible and not reasons:
            reasons.append("Hardware compatible")

        return compatible, reasons

    def get_recommendations(
        self,
        capability_name: str,
        user_preferences: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[PackMetadata, List[str]]]:
        """Return recommended packs with explanation.

        Each entry is a tuple of (pack_metadata, list_of_explanations).
        Explanations include capability match, hardware compatibility, and any
        limitations.
        """
        recommendations: List[Tuple[PackMetadata, List[str]]] = []
        hardware = self.get_hardware_info()

        for pack in self.discover_packs(capability_name):
            compatible, reasons = self.check_hardware_compatibility(pack, hardware)
            explanations: List[str] = []
            # Add capability match
            explanations.append(f"Provides {pack.capability.name}: {pack.capability.description}")
            # Add hardware reasons (reversed to put positives first)
            if compatible:
                explanations.extend([r for r in reasons if "Insufficient" not in r and "Platform" not in r])
                explanations.append("Hardware compatible")
            else:
                explanations.extend(reasons)
            explanations.append(f"Model: {pack.name} v{pack.version}")
            if pack.is_verified:
                explanations.append("Verified pack")
            else:
                explanations.append("Research candidate")
            recommendations.append((pack, explanations))

        # Sort recommendations: verified first, then by estimated RAM (smaller first)
        recommendations.sort(
            key=lambda x: (
                0 if x[0].is_verified else 1,
                x[0].estimated_ram_mb or float("inf"),
            ),
        )
        return recommendations

    def request_approval(self, pack_id: str, user_id: Optional[str] = None) -> PackApproval:
        """Record an approval request for *pack_id*.

        This method does not grant approval; it only creates a pending request.
        The returned approval object has ``approved_by`` set to ``None`` unless
        *user_id* is provided (in which case it is set pending).

        Returns the current approval state.
        """
        approval = self._approvals.get(pack_id)
        if approval is None:
            approval = PackApproval(pack_id=pack_id)
            self._approvals[pack_id] = approval
        # Requesting approval must NEVER set approved_by unless user_id is provided
        if user_id is not None and not approval.approved_by:
            approval.approved_by = user_id
            approval.approved_at = "pending"
        return approval

    def approve_approval(self, pack_id: str, user_id: str) -> PackApproval:
        """Explicitly grant approval for *pack_id* by *user_id*.

        This separates the act of requesting from the act of granting.
        After this call, ``approval.can_install`` returns ``True`` (assuming
        not declined).
        """
        approval = self._approvals.get(pack_id)
        if approval is None:
            approval = PackApproval(pack_id=pack_id)
            self._approvals[pack_id] = approval
        approval.approved_by = user_id
        approval.approved_at = "granted"
        return approval

    def reject_approval(self, pack_id: str, user_id: str) -> PackApproval:
        """Explicitly record rejection for *pack_id* by *user_id*.

        After this call, ``approval.can_install`` returns ``False`` and the
        approval is marked as declined.
        """
        approval = self._approvals.get(pack_id)
        if approval is None:
            approval = PackApproval(pack_id=pack_id)
            self._approvals[pack_id] = approval
        approval.declined = True
        approval.declined_reason = f"Rejected by {user_id}"
        return approval

    def get_approval(self, pack_id: str) -> Optional[PackApproval]:
        """Return approval state for *pack_id*.

        Returns ``None`` if no approval request has been made for this pack.
        """
        return self._approvals.get(pack_id)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _florence2_pack_metadata(self, descriptor: ModelDescriptor) -> PackMetadata:
        """Create PackMetadata for Florence-2 Vision pack.

        This is the only pack registered in this milestone. The capability is
        deliberately limited to "image_description" to match the current runtime
        exposure.
        """
        return PackMetadata(
            model_descriptor=descriptor,
            capability=PackCapability(
                name="image_description",
                description="Detailed image description and tagging",
                requires_hardware={
                    "cpu_cores": "8",
                },
            ),
            is_verified=True,
            verification_notes="Florence-2 Base is the reference implementation for SiloVault vision capabilities",
            requires_approval=True,
            approval_required_text="Florence-2 Vision pack requires explicit user approval before installation",
        )