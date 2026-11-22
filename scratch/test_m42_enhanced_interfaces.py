import sys
sys.path.insert(0, r"c:\Projects\SiloVault")

from backend.ai import ModelManager, InferenceRuntime, ModelDescriptor, CapabilityRegistry, Profile, HardwareDetector, PROFILE_CAPABILITIES

print("=== M4.2 Enhanced Interfaces Test ===")

# Test 1: Enhanced ModelDescriptor with optional metadata
print("\n1. Testing enhanced ModelDescriptor...")
desc = ModelDescriptor(
    name="test_model",
    version="1.0",
    modalities=("text", "vision"),
    local_path=None,
    model_id="model_123",
    framework="pytorch",
    supported_formats=(".pdf", ".jpg", ".png"),
    model_size_bytes=1024000,
    checksum="abc123def456",
    licensing="MIT",
    hardware_requirements={"cpu": "i5", "gpu": "GTX1060", "ram_gb": "8"}
)

assert desc.name == "test_model"
assert desc.version == "1.0"
assert desc.modalities == ("text", "vision")
assert desc.model_id == "model_123"
assert desc.framework == "pytorch"
assert desc.supported_formats == (".pdf", ".jpg", ".png")
assert desc.model_size_bytes == 1024000
assert desc.checksum == "abc123def456"
assert desc.licensing == "MIT"
assert desc.hardware_requirements == {"cpu": "i5", "gpu": "GTX1060", "ram_gb": "8"}
print("✓ Enhanced ModelDescriptor with optional metadata works correctly")

# Test 2: Backward compatibility - ModelDescriptor without optional fields
desc2 = ModelDescriptor(name="simple_model", version="2.0", modalities=("text",))
assert desc2.name == "simple_model"
assert desc2.version == "2.0"
assert desc2.modalities == ("text",)
assert desc2.model_id is None
assert desc2.framework is None
assert desc2.supported_formats is None
assert desc2.model_size_bytes is None
assert desc2.checksum is None
assert desc2.licensing is None
assert desc2.hardware_requirements is None
print("✓ Backward compatibility - ModelDescriptor without optional fields works")

# Test 3: Enhanced ModelManager with new methods
print("\n2. Testing enhanced ModelManager...")
manager = ModelManager()

# Test associate_runtime_with_model
manager.register_runtime(DummyRuntime())
manager.register_descriptor(desc)
manager.associate_runtime_with_model("dummy", "test_model")
print("✓ associate_runtime_with_model works")

# Test get_models_for_runtime
models = manager.get_models_for_runtime("dummy")
assert "test_model" in models
print(f"✓ get_models_for_runtime returns {models}")

# Test get_runtime_for_model
runtime = manager.get_runtime_for_model("test_model")
assert runtime == "dummy"
print(f"✓ get_runtime_for_model returns {runtime}")

# Test get_compatible_runtime_for_capability
manager.set_profile(Profile.PRO)
compatible_runtime = manager.get_compatible_runtime_for_capability("image_description")
assert compatible_runtime == "dummy"
print(f"✓ get_compatible_runtime_for_capability returns {compatible_runtime}")

# Test can_execute_capability
can_execute = manager.can_execute_capability("image_description")
assert can_execute == True
print(f"✓ can_execute_capability returns {can_execute}")

# Test 4: Enhanced CapabilityRegistry
print("\n3. Testing enhanced CapabilityRegistry...")
reg = CapabilityRegistry()
reg.register("test_cap", desc2)
assert reg.get("test_cap").name == "simple_model"
print("✓ Enhanced CapabilityRegistry works correctly")

# Test 5: Profile preservation
print("\n4. Testing Profile preservation...")
assert Profile.LITE.value == "lite"
assert Profile.PRO.value == "pro"
print("✓ Profile.LITE and Profile.PRO preserved")

# Test 6: Backward compatibility with existing code
print("\n5. Testing backward compatibility...")
# Test that existing code patterns still work
old_style_desc = ModelDescriptor("old_model", "1.0", ("text",))
assert old_style_desc.name == "old_model"
assert old_style_desc.version == "1.0"
assert old_style_desc.modalities == ("text",)
print("✓ Backward compatibility with existing code patterns")

# Test 7: Enhanced ModelManager with profile switching
print("\n6. Testing ModelManager profile switching...")
manager.set_profile(Profile.LITE)
assert manager.get_profile() == Profile.LITE
print(f"✓ Profile switching works: {manager.get_profile()}")

# Test 8: Enhanced ModelManager with capability lookup
print("\n7. Testing ModelManager capability lookup...")
manager.set_profile(Profile.PRO)
cap_desc = manager.get_capability_descriptor("image_description")
assert cap_desc is not None
assert cap_desc.name == "image_description_pro"
print(f"✓ Capability lookup works: {cap_desc.name}")

# Test 9: Enhanced ModelManager with graceful fallback
print("\n8. Testing ModelManager graceful fallback...")
fallback_result = manager.try_execute("nonexistent_capability")
assert fallback_result is None
print("✓ Graceful fallback works")

# Test 10: Enhanced ModelManager with hardware detection
print("\n9. Testing ModelManager hardware detection...")
hw = HardwareDetector.detect()
assert "cpu" in hw
assert "gpu" in hw
assert "ram_gb" in hw
print(f"✓ Hardware detection works: {hw}")

print("\n=== All Enhanced Interface Tests Passed ===")
print("All enhancements are backward compatible and functional.")

# Dummy runtime for testing
class DummyRuntime:
    @property
    def name(self):
        return "dummy"
    @property
    def version(self):
        return "0.1.0"
    def is_available(self):
        return True
    def supported_modalities(self):
        return ["text"]
    def generate(self, prompt, **kwargs):
        return f"Generated: {prompt}"
    def embed(self, text):
        return [0.1, 0.2, 0.3]
    def classify(self, image=None, text=None):
        return {"text": 0.5} if text else None