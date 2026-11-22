import sys
sys.path.insert(0, r"c:\Projects\SiloVault")

from backend.ai import ModelManager, InferenceRuntime, ModelDescriptor, CapabilityRegistry, Profile, HardwareDetector, PROFILE_CAPABILITIES

print("All imports successful")

# Quick sanity checks
manager = ModelManager()
print(f"Default profile: {manager.get_profile()}")

# Register a dummy runtime
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

runtime = DummyRuntime()
manager.register_runtime(runtime)
print(f"Registered runtimes: {manager.list_runtimes()}")

# Register a model descriptor
desc = ModelDescriptor(name="text_classifier_lite", version="1.0", modalities=("text",), local_path=None)
manager.register_descriptor(desc)
print(f"Registered descriptors: {manager.list_descriptors()}")

# Test capability lookup
cap_desc = manager.get_capability_descriptor("text_classification")
print(f"Capability descriptor for text_classification: {cap_desc}")

# Test profile switch
manager.set_profile(Profile.PRO)
print(f"Profile after switch: {manager.get_profile()}")
cap_desc_pro = manager.get_capability_descriptor("image_description")
print(f"Image description descriptor in PRO: {cap_desc_pro}")

# Test hardware detection
hw = HardwareDetector.detect()
print(f"Hardware detection: {hw}")

# Test CapabilityRegistry standalone
reg = CapabilityRegistry()
reg.register("test_cap", ModelDescriptor(name="test", version="1", modalities=("text",)))
print(f"CapabilityRegistry get: {reg.get('test_cap')}")

print("\nAll M4.2 foundation tests passed!")