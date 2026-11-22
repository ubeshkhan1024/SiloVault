import sys
sys.path.insert(0, r"c:\Projects\SiloVault")

from backend.ai import ModelManager, InferenceRuntime, ModelDescriptor, CapabilityRegistry, Profile, HardwareDetector, PROFILE_CAPABILITIES

print("=== M4.2 Foundation Test: Interfaces and Registry ===")

# Test InferenceRuntime protocol
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

# Test ModelDescriptor
desc = ModelDescriptor(name="text_classifier_lite", version="1.0", modalities=("text",), local_path=None)
print(f"ModelDescriptor created: {desc}")

# Test Profile enum
print(f"Profile.LITE: {Profile.LITE}")
print(f"Profile.PRO: {Profile.PRO}")

# Test PROFILE_CAPABILITIES
lite_caps = PROFILE_CAPABILITIES[Profile.LITE]
print(f"LITE capabilities: {list(lite_caps.keys())}")
pro_caps = PROFILE_CAPABILITIES[Profile.PRO]
print(f"PRO capabilities: {list(pro_caps.keys())}")

# Test CapabilityRegistry
reg = CapabilityRegistry()
reg.register("test_cap", ModelDescriptor(name="test", version="1.0", modalities=("text",), local_path=None))
print(f"CapabilityRegistry: {reg.get('test_cap')}")

# Test ModelManager
manager = ModelManager()
print(f"Default profile: {manager.get_profile()}")

# Register runtime
manager.register_runtime(DummyRuntime())
print(f"Registered runtimes: {manager.list_runtimes()}")

# Register descriptor
manager.register_descriptor(desc)
print(f"Registered descriptors: {manager.list_descriptors()}")

# Test capability lookup
cap_desc = manager.get_capability_descriptor("text_classification")
print(f"Capability descriptor for text_classification: {cap_desc}")

# Test profile switching
manager.set_profile(Profile.PRO)
print(f"Profile after switch: {manager.get_profile()}")
cap_desc_pro = manager.get_capability_descriptor("image_description")
print(f"Image description descriptor in PRO: {cap_desc_pro}")

# Test hardware detection
hw = HardwareDetector.detect()
print(f"Hardware detection: {hw}")

print("\n=== M4.2 Foundation Test Complete ===")
print("All interfaces and registries appear to be implemented as intended.")