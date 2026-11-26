import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

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
from backend.ai.pack_manager import PackManager, PackMetadata, PackCapability, PackManifest, PackApproval

class TestPackManager(unittest.TestCase):
    def setUp(self):
        # Mock hardware detector
        self.hardware_info = HardwareInfo(
        os_name="Windows",
        os_version="10",
        architecture="x64",
        cpu_name="Intel Core i7",
        logical_cores=8,
        total_ram_bytes=16*1024**3,  # 16 GB in bytes
    )
        self.hardware_detector = MagicMock()
        self.hardware_detector.detect.return_value = self.hardware_info

        # Mock capability registry
        self.capability_registry = MagicMock(spec=CapabilityRegistry)
        florence_descriptor = ModelDescriptor(
            name="Florence-2 Base",
            version="1.0",
            modalities=("vision",),
            model_id="microsoft.florence-2-base",
            compatible_runtimes=("florence2-cpu",),
            hardware_requirements={"cpu_cores": "8"},
        )
        self.capability_registry.resolve.return_value = florence_descriptor

        # Mock model registry
        self.model_registry = MagicMock(spec=ModelRegistry)
        self.model_registry.is_registered.return_value = True
        self.model_registry.find.return_value = florence_descriptor

        # Mock model storage
        self.model_storage = MagicMock(spec=ModelStorage)

        # Create PackManager
        self.pack_manager = PackManager(
            self.hardware_detector,
            self.capability_registry,
            self.model_registry,
            self.model_storage,
        )

    def test_discover_packs_finds_florence(self):
        """Test that discover_packs returns Florence-2 for image_description capability."""
        packs = self.pack_manager.discover_packs("image_description")
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0].model_descriptor.model_id, "microsoft.florence-2-base")
        self.assertEqual(packs[0].capability.name, "image_description")

    def test_discover_packs_no_capability(self):
        """Test that discover_packs returns empty list when capability not registered."""
        self.capability_registry.resolve.return_value = None
        packs = self.pack_manager.discover_packs("ocr")
        self.assertEqual(len(packs), 0)

    def test_hardware_compatibility_check(self):
        """Test hardware compatibility checks for Florence-2 pack."""
        florence_pack = self.pack_manager.discover_packs("image_description")[0]
        compatible, reasons = self.pack_manager.check_hardware_compatibility(florence_pack)
        self.assertTrue(compatible)
        self.assertIn("Hardware compatible", reasons)
        self.assertNotIn("Insufficient RAM", reasons)

    def test_hardware_compatibility_insufficient_ram(self):
        """Test that insufficient RAM is detected correctly."""
        florence_pack = self.pack_manager.discover_packs("image_description")[0]
        # Modify pack to require more RAM than available
        florence_pack = replace(florence_pack, estimated_ram_mb=20000)  # 20 GB
        compatible, reasons = self.pack_manager.check_hardware_compatibility(florence_pack)
        self.assertFalse(compatible)
        self.assertTrue(any("Insufficient RAM" in reason for reason in reasons))

    def test_hardware_compatibility_unsupported_platform(self):
        """Test platform compatibility check."""
        florence_pack = self.pack_manager.discover_packs("image_description")[0]
        florence_pack = replace(florence_pack, supported_platforms=("Linux",))
        compatible, reasons = self.pack_manager.check_hardware_compatibility(florence_pack)
        self.assertFalse(compatible)
        self.assertTrue(any("Platform Windows not supported" in reason for reason in reasons))

    def test_recommendations_returns_sorted(self):
        """Test that recommendations are sorted with verified packs first."""
        # Create a second unverified pack
        unverified_descriptor = ModelDescriptor(
            name="MobileNetV3 Small",
            version="1.0",
            modalities=("vision",),
            model_id="tensorflow.mobilenet_v3_small",
        )
        unverified_pack = PackMetadata(
            model_descriptor=unverified_descriptor,
            capability=PackCapability(name="classification", description="Image classification"),
            is_verified=False,
        )
        # Mock discover to return both packs
        self.pack_manager.discover_packs = MagicMock(return_value=[unverified_pack, self.pack_manager.discover_packs("image_description")[0]])
        recommendations = self.pack_manager.get_recommendations("classification")
        self.assertEqual(len(recommendations), 2)
        # First should be verified (Florence-2), second unverified
        self.assertTrue(recommendations[0][0].is_verified)
        self.assertFalse(recommendations[1][0].is_verified)

    def test_approval_workflow(self):
        """Test that approval state is tracked correctly."""
        florence_pack = self.pack_manager.discover_packs("image_description")[0]
        approval = self.pack_manager.request_approval(florence_pack.identifier)
        self.assertIsNotNone(approval)
        self.assertFalse(approval.is_approved)
        # Request approval again should return same state
        approval2 = self.pack_manager.request_approval(florence_pack.identifier)
        self.assertIs(approval, approval2)
        # Approve
        approval = self.pack_manager.request_approval(
            florence_pack.identifier, user_id="alice"
        )
        self.assertTrue(approval.is_approved)
        self.assertEqual(approval.approved_by, "alice")

    def test_pack_manifest_validation(self):
        """Test that PackManifest can be created and validated."""
        valid_manifest = PackManifest(
            schema_version="2",
            identifier="test-model",
            version="1.0",
            files=[{"path": "model.safetensors", "size": 1024, "sha256": "a" * 64}],
            source={"type": "https", "url": "https://example.com/model.safetensors"},
        )
        # Should not raise
        PackManifest.from_dict({
            "schema_version": "2",
            "identifier": "test-model",
            "version": "1.0",
            "files": [{"path": "model.safetensors", "size": 1024, "sha256": "a" * 64}],
            "source": {"type": "https", "url": "https://example.com/model.safetensors"},
        })

    def test_pack_manifest_invalid_schema(self):
        """Test that invalid manifest raises ValueError."""
        with self.assertRaises(ValueError):
            PackManifest.from_dict({
                "schema_version": "1",  # Unsupported version
                "identifier": "test-model",
                "version": "1.0",
                "files": [],
            })

    def test_existing_model_manager_unchanged(self):
        """Test that creating PackManager does not affect ModelManager behavior.

        This verifies we didn't accidentally modify existing AI interfaces.
        """
        # Verify we can still use ModelManager as before
        from backend.ai.interfaces import ModelManager
        # This should not raise
        _ = ModelManager(model_registry=self.model_registry)

    def test_hardware_compatibility_unknown_requirement_fails_closed(self):
        """Unknown hardware requirements must not be treated as compatible."""
        florence_pack = self.pack_manager.discover_packs("image_description")[0]

        descriptor = replace(
            florence_pack.model_descriptor,
            hardware_requirements={"unknown_accelerator": "required"},
        )
        florence_pack = replace(florence_pack, model_descriptor=descriptor)

        compatible, reasons = self.pack_manager.check_hardware_compatibility(
            florence_pack
        )

        self.assertFalse(compatible)
        self.assertTrue(
            any("unknown_accelerator" in reason for reason in reasons)
        )
        
if __name__ == "__main__":
    unittest.main()



