from backend.ai.interfaces import ModelDescriptor, ModelRegistry, CapabilityRegistry, Profile
from backend.ai.catalogue import MODEL_CATALOGUE, register_catalogue

# Check current catalogue
print('=== Current Model Catalogue ===')
for desc in MODEL_CATALOGUE:
    print(f'Name: {desc.name}')
    print(f'Version: {desc.version}')
    print(f'Modalities: {desc.modalities}')
    print(f'Model ID: {desc.model_id}')
    print(f'Compatible runtimes: {desc.compatible_runtimes}')
    print(f'Identifier: {desc.identifier}')
    print()

# Check CapabilityRegistry
registry = ModelRegistry()
register_catalogue(registry)

print('=== Capability Registry ===')
print(f'list_by_capability(): {registry.list_by_capability()}')

# Resolve with profile
bal_desc = registry.resolve('image_description', profile=Profile.BALANCED)
print(f'Resolve BALANCED: {bal_desc}')

# Check profile capabilities
bal_caps = registry.list_by_capability(Profile.BALANCED)
print(f'BALANCED capabilities: {bal_caps}')

effic_caps = registry.list_by_capability(Profile.EFFICIENT)
print(f'EFFICIENT capabilities: {effic_caps}')