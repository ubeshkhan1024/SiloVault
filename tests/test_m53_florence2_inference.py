"""M5.3 Florence-2 Inference Behavioral Tests.

pytest-based tests exercising the actual Florence2Runtime._do_execute() method
using module-level mocks. No model downloads, no package installations.

Tests verify the actual inference method behavior with mocked dependencies.
"""

import sys
import struct
import zlib
import types
from unittest.mock import MagicMock
import pytest
from PIL import Image
from backend.ai.florence2_runtime import Florence2Runtime

def create_mock_png():
    """Create a minimal valid 1x1 white PNG (69 bytes) for testing."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xffffffff)
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + ihdr_crc
    raw = b"\x00\xff\xff\xff"
    idat_data = zlib.compress(raw)
    idat_crc = struct.pack(">I", zlib.crc32(b"IDAT" + idat_data) & 0xffffffff)
    idat = struct.pack(">I", len(idat_data)) + b"IDAT" + idat_data + idat_crc
    iend_crc = struct.pack(">I", zlib.crc32(b"IEND") & 0xffffffff)
    iend = struct.pack(">I", 0) + b"IEND" + iend_crc
    return sig + ihdr + idat + iend


@pytest.fixture(autouse=True)
def mock_torch(monkeypatch):
    """Install a fake torch module before each test using monkeypatch."""
    torch_mock = types.ModuleType("torch")
    torch_mock.tensor = lambda *args, **kwargs: [[1, 2, 3]]
    torch_mock.float = float
    torch_mock.device = str
    torch_mock.cuda = types.ModuleType("cuda")
    torch_mock.cuda.is_available = lambda: False

    class _InferenceModeContext:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    class _InferenceModeMock:
        def __call__(self):
            return _InferenceModeContext()

    torch_mock.inference_mode = _InferenceModeMock()
    monkeypatch.setitem(sys.modules, "torch", torch_mock)


@pytest.fixture
def runtime(mock_torch):
    """Provide a fresh Florence2Runtime instance for each test."""
    return Florence2Runtime()


def _make_mock_loaded_model():
    """Create a fake loaded model dict with MagicMock processor and model."""
    mock_processor = MagicMock()
    mock_processor.return_value = {
        "input_ids": [[1, 2, 3, 4]],
        "pixel_values": [[[1.0, 1.0, 1.0]]],
    }
    mock_processor.batch_decode = MagicMock(
        return_value=["<DETAILED_CAPTION> description"]
    )
    mock_processor.post_process_generation = MagicMock(
        return_value={"<DETAILED_CAPTION>": "description"}
    )

    mock_model = MagicMock()
    # Default generate return value
    mock_generate_return = MagicMock()
    mock_generate_return.tolist = MagicMock(return_value=[[1, 2, 3, 4, 5]])
    mock_model.generate = mock_generate_return

    return {"processor": mock_processor, "model": mock_model}


@pytest.fixture
def mock_loaded_model():
    """Provide a fake loaded model dictionary for each test."""
    return _make_mock_loaded_model()


def test_pil_image_conversion(runtime, mock_loaded_model):
    """Test that encoded PNG bytes are converted to RGB PIL Image via _do_execute."""
    runtime._loaded_model = mock_loaded_model
    runtime._state = type(runtime._state).READY

    png_bytes = create_mock_png()

    result = runtime._do_execute(png_bytes)

    # Verify the result structure
    assert "description" in result, "Missing 'description' in result"
    assert "tags" in result, "Missing 'tags' in result"
    assert "model" in result, "Missing 'model' in result"
    # Critical: confidence must be None, not fabricated 0.95
    assert result["confidence"] is None, f"Expected None, got {result['confidence']}"

    # Verify processor was called with task prompt and image
    runtime._loaded_model["processor"].assert_called()


def test_processor_receives_rgb_pil_image(runtime, mock_loaded_model):
    """Test that processor receives <DETAILED_CAPTION> and an RGB PIL Image."""
    runtime._loaded_model = mock_loaded_model
    runtime._state = type(runtime._state).READY

    png_bytes = create_mock_png()

    result = runtime._do_execute(png_bytes)

    # Check that processor was called with correct arguments
    call_kwargs = runtime._loaded_model["processor"].call_args[1]

    # Verify text argument contains task prompt
    has_text = "<DETAILED_CAPTION>" in str(call_kwargs.get("text", ""))
    assert has_text, "Processor should receive <DETAILED_CAPTION> text prompt"

    # Verify images is a PIL Image with RGB mode
    images_arg = call_kwargs.get("images")
    assert images_arg is not None, "Processor should receive image argument"
    assert isinstance(images_arg, Image.Image), (
        f"Processor should receive PIL Image, got {type(images_arg)}"
    )
    assert images_arg.mode == "RGB", (
        f"Processor should receive RGB PIL Image, got mode={images_arg.mode}"
    )

    # Verify the result structure
    assert "description" in result, "Missing 'description' in result"
    assert "tags" in result, "Missing 'tags' in result"
    assert result["confidence"] is None


def test_generation_options(runtime, mock_loaded_model):
    """Test that model.generate() receives correct input_ids, pixel_values and options."""
    runtime._loaded_model = mock_loaded_model
    runtime._state = type(runtime._state).READY

    png_bytes = create_mock_png()

    result = runtime._do_execute(png_bytes)

    # Verify model.generate was called with expected arguments
    generate_call = runtime._loaded_model["model"].generate.call_args

    # Check input_ids is present
    assert "input_ids" in generate_call[1], "generate() should receive input_ids"

    # Check pixel_values is present
    assert "pixel_values" in generate_call[1], "generate() should receive pixel_values"

    # Check generation options
    kwargs = generate_call[1]

    assert kwargs.get("max_new_tokens") == 1024, (
        f"Expected max_new_tokens=1024, got {kwargs.get('max_new_tokens')}"
    )
    assert kwargs.get("do_sample") is False, (
        f"Expected do_sample=False, got {kwargs.get('do_sample')}"
    )
    assert kwargs.get("num_beams") == 3, (
        f"Expected num_beams=3, got {kwargs.get('num_beams')}"
    )

    # Verify the result structure
    assert "description" in result, "Missing 'description' in result"
    assert result["confidence"] is None


def test_batch_decode_and_post_process(runtime, mock_loaded_model):
    """Test that batch_decode and post_process_generation are called correctly."""
    runtime._loaded_model = mock_loaded_model
    runtime._state = type(runtime._state).READY

    png_bytes = create_mock_png()

    result = runtime._do_execute(png_bytes)

    # Verify batch_decode was called with skip_special_tokens=False
    batch_decode_call = runtime._loaded_model["processor"].batch_decode.call_args
    assert "skip_special_tokens" in batch_decode_call[1], (
        "batch_decode() should receive skip_special_tokens kwarg"
    )
    assert batch_decode_call[1].get("skip_special_tokens") is False, (
        f"Expected skip_special_tokens=False, got {batch_decode_call[1].get('skip_special_tokens')}"
    )

    # Verify post_process_generation was called with decoded text, task, and image size
    post_process_call = runtime._loaded_model["processor"].post_process_generation.call_args
    assert post_process_call is not None, "post_process_generation() should be called"

    # Verify generated token IDs are passed to batch_decode.
    assert batch_decode_call[0][0] == runtime._loaded_model["model"].generate.return_value

    # Verify decoded text and Florence task are passed to post-processing.
    assert post_process_call[0][0] == "<DETAILED_CAPTION> description"
    assert post_process_call[1]["task"] == "<DETAILED_CAPTION>"
    assert post_process_call[1]["image_size"] == (1, 1)

    assert result["description"] == "description"
    assert result["tags"] == ("description",)
    assert result["model"] == runtime._get_loaded_model_identifier()

    # Verify the result structure
    assert "description" in result, "Missing 'description' in result"
    assert "tags" in result, "Missing 'tags' in result"
    assert result["confidence"] is None


def test_confidence_not_fabricated(runtime, mock_loaded_model):
    """Test that confidence is None, not a fabricated 0.95 constant."""
    runtime._loaded_model = mock_loaded_model
    runtime._state = type(runtime._state).READY

    result = runtime._do_execute(create_mock_png())

    # Critical assertion: confidence must be None, NOT 0.95
    assert result["confidence"] is None, (
        f"FAIL: confidence is {result['confidence']}, expected None"
    )
    assert "0.95" not in result.get("description", ""), (
        "FAIL: 0.95 appears in description"
    )


def test_invalid_image_raises_execution_error(runtime, mock_loaded_model):
    """Test that invalid image input produces structured ExecutionError."""
    from backend.ai.florence2_runtime import ExecutionError

    runtime._loaded_model = mock_loaded_model
    runtime._state = type(runtime._state).READY

    # Make post_process_generation raise an exception
    runtime._loaded_model["processor"].post_process_generation.side_effect = Exception(
        "Invalid image dimensions"
    )

    runtime._loaded_model["processor"].assert_not_called()
    runtime._loaded_model["model"].generate.assert_not_called()

    try:
        result = runtime._do_execute(b"not an image at all!")
        assert False, "Should have raised ExecutionError"
    except ExecutionError as e:
        # Verify it's a structured error (not a raw exception)
        assert isinstance(e, ExecutionError), (
            f"Expected ExecutionError, got {type(e).__name__}"
        )


def test_generation_failure_raises_execution_error(runtime, mock_loaded_model):
    """Test that generation exceptions produce structured ExecutionError."""
    from backend.ai.florence2_runtime import ExecutionError

    runtime._loaded_model = mock_loaded_model
    runtime._state = type(runtime._state).READY

    # Make model.generate raise an exception
    # This will be caught by _do_execute's except Exception and re-raised as ExecutionError
    runtime._loaded_model["model"].generate.side_effect = RuntimeError(
        "Simulated generation failure"
    )

    try:
        runtime._do_execute(create_mock_png())
        assert False, "Should have raised ExecutionError"
    except ExecutionError as e:
        assert isinstance(e, ExecutionError), (
            f"Expected ExecutionError, got {type(e).__name__}"
        )


def test_cancellation_before_execution(runtime, mock_loaded_model):
    """Test that setting cancellation event before execution raises CancellationError."""
    from backend.ai.florence2_runtime import CancellationError

    runtime._loaded_model = mock_loaded_model

    # Set cancellation token before execution - should raise immediately at line 423-424
    runtime._cancellation_token.set()
    runtime._state = type(runtime._state).READY

    try:
        runtime._do_execute(create_mock_png())
        assert False, "Should have raised CancellationError"
    except CancellationError as e:
        assert isinstance(e, CancellationError), (
            f"Expected CancellationError, got {type(e).__name__}"
        )

    # Verify model.generate was NOT called (cancellation checked before model call)
    runtime._loaded_model["model"].generate.assert_not_called()

    # Verify processor was NOT called (cancellation checked before processor call)
    runtime._loaded_model["processor"].assert_not_called()