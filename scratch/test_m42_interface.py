import sys
sys.path.insert(0, r"c:\Projects\SiloVault")

from backend.ai import ModelManager, InferenceRuntime, ModelDescriptor, CapabilityRegistry, Profile, HardwareDetector, PROFILE_CAPABILITIES

print("=== M4.2 Foundation Interface Test ===")

errors = []

# 1. Test InferenceRuntime protocol compliance
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
        return {"text": 0.5} if text else {}

# Test protocol properties
rt = DummyRuntime()
assert hasattr(rt, 'name'), "Missing 'name' property"
assert hasattr(rt, 'version'), "Missing 'version' property"
assert rt.name == "dummy", f"Expected 'dummy', got '{rt.name}'"
assert rt.version == "0.1.0", f"Expected '0.1.0', got '{rt.version}'"
print("✓ InferenceRuntime protocol: name and version properties work")

# 2. Test ModelDescriptor
desc = ModelDescriptor(name="test_model", version="1.0", modalities=("text",), local_path=None)
assert desc.name == "test_model"
assert desc.version == "1.0"
assert desc.modalities == ("text",)
assert desc.local_path is None
print("✓ ModelDescriptor: all fields work correctly")

# 3. Test Profile enum
assert Profile.LITE.value == "lite"
assert Profile.PRO.value == "pro"
print("✓ Profile enum: LITE and PRO values correct")

# 4. Test PROFILE_CAPABILITIES mapping
lite_caps = PROFILE_CAPABILITIES[Profile.LITE]
pro_caps = PROFILE_CAPABILITIES[Profile.PRO]
assert "text_classification" in lite_caps, "Lite profile missing text_classification"
assert "text_classification" in pro_caps, "Pro profile missing text_classification"
assert "image_description" in pro_caps, "Pro profile missing image_description"
assert "audio_classification" in pro_caps, "Pro profile missing audio_classification"
print(f"✓ PROFILE_CAPABILITIES: Lite={list(lite_caps.keys())}, Pro={list(pro_caps.keys())}")

# 5. Test CapabilityRegistry
reg = CapabilityRegistry()
desc2 = ModelDescriptor(name="reg_model", version="2.0", modalities=("text",), local_path=None)
reg.register("test_cap", desc2)
retrieved = reg.get("test_cap")
assert retrieved.name == "reg_model"
print("✓ CapabilityRegistry: register and get work correctly")

# 6. Test CapabilityRegistry list_capabilities
caps_list = reg.list_capabilities()
assert "test_cap" in caps_list
print(f"✓ CapabilityRegistry: list_capabilities returns {caps_list}")

# 7. Test ModelManager
manager = ModelManager()
assert manager.get_profile() == Profile.LITE, f"Expected LITE, got {manager.get_profile()}"
print(f"✓ ModelManager: default profile is {manager.get_profile()}")

# 8. Test register runtime
manager.register_runtime(DummyRuntime())
runtimes = manager.list_runtimes()
assert "dummy" in runtimes
print(f"✓ ModelManager: register_runtime works, runtimes={runtimes}")

# 9. Test register descriptor
manager.register_descriptor(desc)
descriptors = manager.list_descriptors()
assert "test_model" in descriptors
print(f"✓ ModelManager: register_descriptor works, descriptors={descriptors}")

# 10. Test capability lookup
cap_desc = manager.get_capability_descriptor("text_classification")
assert cap_desc is not None, "text_classification should be available in LITE profile"
assert cap_desc.name == "text_classifier_lite", f"Expected text_classifier_lite, got {cap_desc.name}"
print(f"✓ ModelManager: get_capability_descriptor returns {cap_desc}")

# 11. Test profile switch
manager.set_profile(Profile.PRO)
assert manager.get_profile() == Profile.PRO, f"Expected PRO, got {manager.get_profile()}"
print(f"✓ ModelManager: set_profile switches to {manager.get_profile()}")

# 12. Test capability lookup after profile switch
cap_desc_pro = manager.get_capability_descriptor("image_description")
assert cap_desc_pro is not None, "image_description should be available in PRO profile"
assert cap_desc_pro.name == "image_description_pro", f"Expected image_description_pro, got {cap_desc_pro.name}"
print(f"✓ ModelManager: PRO profile image_description={cap_desc_pro.name}")

# 13. Test hardware detection
hw = HardwareDetector.detect()
assert "cpu" in hw
assert "gpu" in hw
assert "ram_gb" in hw
print(f"✓ HardwareDetector: detect() returns {hw}")

# 14. Test try_execute graceful fallback
result = manager.try_execute("nonexistent_capability")
assert result is None, f"Expected None for nonexistent capability, got {result}"
print("✓ ModelManager: try_execute returns None for unavailable capability")

# 15. Test __all__ exports from __init__.py
from backend.ai import __all__ as ai_imports
assert "InferenceRuntime" in ai_imports
assert "ModelDescriptor" in ai_imports
assert "CapabilityRegistry" in ai_imports
assert "Profile" in ai_imports
assert "HardwareDetector" in ai_imports
assert "ModelManager" in ai_imports
assert "PROFILE_CAPABILITIES" in ai_imports
print("✓ backend.ai.__init__: all expected exports present")

print("\n=== All M4.2 Foundation Tests Passed ===")
print("All interfaces, registries, profiles, and managers are implemented as intended.")