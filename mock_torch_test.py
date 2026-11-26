import sys
import types

# Directly mock torch at module level BEFORE any other imports
torch_mock = types.ModuleType('torch')
torch_mock.tensor = lambda *args, **kwargs: [[1, 2, 3]]
torch_mock.float = float
torch_mock.device = str
torch_mock.cuda = types.ModuleType('cuda')
torch_mock.cuda.is_available = lambda: False

# Add inference_mode context manager
class _InferenceModeContext:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False

torch_mock.inference_mode = _InferenceModeContext()

# Replace in sys.modules BEFORE importing anything else
sys.modules['torch'] = torch_mock

# Now import the runtime
from backend.ai.florence2_runtime import Florence2Runtime
runtime = Florence2Runtime()
print('Runtime created successfully')
print('Torch mocked:', 'torch' in sys.modules)