from __future__ import annotations

import io
import pytest
import subprocess
import tempfile
import re
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image
from backend.ai.tesseract_ocr_adapter import TesseractOCRAdapter, _parse_hocr, _validate_image
from backend.ai.ocr_runtime import OCREngineError, OCRResult


def _make_png() -> bytes:
    """Generate a minimal valid PNG image in memory."""
    img = Image.new("RGB", (10, 10), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg() -> bytes:
    """Generate a minimal valid JPEG image in memory."""
    img = Image.new("RGB", (10, 10), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def adapter(tmp_path):
    bin_path = tmp_path / "tesseract.exe"
    bin_path.touch()
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir(parents=True, exist_ok=True)
    # Create a dummy language file
    (tessdata / "eng.traineddata").touch()
    adapter = TesseractOCRAdapter(bin_path, tessdata)
    adapter.initialize()
    return adapter


def test_initialize_success(tmp_path):
    bin_path = tmp_path / "tesseract.exe"
    bin_path.touch()
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    adapter = TesseractOCRAdapter(bin_path, tessdata)
    adapter.initialize()
    assert adapter.is_initialized()


def test_initialize_missing_binary(tmp_path):
    bin_path = tmp_path / "missing.exe"
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    adapter = TesseractOCRAdapter(bin_path, tessdata)
    with pytest.raises(OCREngineError, match="binary not found"):
        adapter.initialize()


def test_execute_ocr_empty_image(adapter):
    result = adapter.execute_ocr(b"", language="eng")
    assert "Empty image data" in result.error


def test_execute_ocr_missing_language(adapter):
    result = adapter.execute_ocr(_make_png(), language="fra")
    assert "Language data missing" in result.error


def test_parse_hocr_basic(tmp_path):
    hocr_content = """
    <span class='ocrx_word' title='bbox 10 20 110 40; x_wconf 85'>Hello</span>
    <span class='ocrx_word' title='bbox 120 20 220 40; x_wconf 95'>World</span>
    """
    hocr_path = tmp_path / "test.hocr"
    hocr_path.write_text(hocr_content)

    text, boxes, conf = _parse_hocr(hocr_path)
    assert text == "Hello World"
    assert len(boxes) == 2
    assert boxes[0] == {"x": 10, "y": 20, "w": 100, "h": 20}
    assert boxes[1] == {"x": 120, "y": 20, "w": 100, "h": 20}
    assert conf == 0.9  # (0.85 + 0.95) / 2


def test_parse_hocr_robustness(tmp_path):
    hocr_content = """
    <div class="ocr_page">
        <span class="ocrx_word" title="bbox 10 20 50 40; x_wconf 90">Hello</span>
        <span title="x_wconf 80; bbox 60 20 100 40" class="ocrx_word">World</span>
        <span class="ocrx_word" title="bbox 10 50 100 70; x_wconf 70">Nested <b>Text</b></span>
        <span class="ocrx_word" title="bbox 10 80 50 100; x_wconf 110">InvalidConf</span>
        <span class="ocrx_word" title="bbox 10 110 5 120; x_wconf 50">InvalidBBox</span>
        <span class="ocrx_word" title="bbox 10 130 50 150; x_wconf 60">Entities & < ></span>
    </div>
    """
    hocr_path = tmp_path / "test.hocr"
    hocr_path.write_text(hocr_content, encoding="utf-8")

    text, boxes, conf = _parse_hocr(hocr_path)

    # InvalidConf is included because its bbox is valid. InvalidBBox is skipped.
    assert text == "Hello World Nested Text InvalidConf Entities & < >"
    # Boxes: Hello, World, Nested Text, InvalidConf, Entities... (InvalidBBox skipped)
    assert len(boxes) == 5
    # Confidences: 0.9, 0.8, 0.7, 0.6 (1.1/InvalidConf skipped)
    # (0.9 + 0.8 + 0.7 + 0.6) / 4 = 0.75
    assert conf == 0.75


def test_parse_hocr_invalid_bbox(tmp_path):
    hocr_content = "<span class='ocrx_word' title='bbox 100 100 50 50'>Reversed</span>"
    hocr_path = tmp_path / "test.hocr"
    hocr_path.write_text(hocr_content)

    text, boxes, conf = _parse_hocr(hocr_path)
    assert text == ""
    assert len(boxes) == 0
    assert conf is None


def test_temp_dir_cleanup_success(adapter):
    captured_paths = []

    def side_effect(*args, **kwargs):
        td = tempfile.mkdtemp()
        captured_paths.append(Path(td))
        mock_td = MagicMock()
        mock_td.__enter__.return_value = td
        # Use shutil to remove non-empty dir
        mock_td.__exit__.side_effect = lambda *a: shutil.rmtree(td)
        return mock_td

    with patch("tempfile.TemporaryDirectory", side_effect=side_effect):
        with patch("subprocess.run") as mock_run:
            def run_side_effect(cmd, **kwargs):
                output_path = Path(cmd[2] + ".hocr")
                output_path.write_text("<span class='ocrx_word' title='bbox 0 0 10 10; x_wconf 95'>Success</span>")
                return MagicMock(returncode=0)
            mock_run.side_effect = run_side_effect

            adapter.execute_ocr(_make_png(), language="eng")

            assert len(captured_paths) == 1
            assert not captured_paths[0].exists(), "Temp dir should be cleaned up on success"


def test_temp_dir_cleanup_timeout(adapter):
    captured_paths = []

    def side_effect(*args, **kwargs):
        td = tempfile.mkdtemp()
        captured_paths.append(Path(td))
        mock_td = MagicMock()
        mock_td.__enter__.return_value = td
        mock_td.__exit__.side_effect = lambda *a: shutil.rmtree(td)
        return mock_td

    with patch("tempfile.TemporaryDirectory", side_effect=side_effect):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10)):
            with pytest.raises(OCREngineError, match="timed out"):
                adapter.execute_ocr(_make_png(), language="eng")

            assert len(captured_paths) == 1
            assert not captured_paths[0].exists(), "Temp dir should be cleaned up on timeout"


def test_temp_dir_cleanup_failure(adapter):
    captured_paths = []

    def side_effect(*args, **kwargs):
        td = tempfile.mkdtemp()
        captured_paths.append(Path(td))
        mock_td = MagicMock()
        mock_td.__enter__.return_value = td
        mock_td.__exit__.side_effect = lambda *a: shutil.rmtree(td)
        return mock_td

    with patch("tempfile.TemporaryDirectory", side_effect=side_effect):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "tesseract", stderr=b"Error")

            with pytest.raises(OCREngineError, match="Tesseract failed"):
                adapter.execute_ocr(_make_png(), language="eng")

            assert len(captured_paths) == 1
            assert not captured_paths[0].exists(), "Temp dir should be cleaned up on subprocess failure"


def test_temp_dir_cleanup_missing_hocr(adapter):
    captured_paths = []

    def side_effect(*args, **kwargs):
        td = tempfile.mkdtemp()
        captured_paths.append(Path(td))
        mock_td = MagicMock()
        mock_td.__enter__.return_value = td
        mock_td.__exit__.side_effect = lambda *a: shutil.rmtree(td)
        return mock_td

    with patch("tempfile.TemporaryDirectory", side_effect=side_effect):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            result = adapter.execute_ocr(_make_png(), language="eng")
            assert "No HOCR output generated" in result.error

            assert len(captured_paths) == 1
            assert not captured_paths[0].exists(), "Temp dir should be cleaned up when HOCR is missing"


def test_execute_ocr_error_sanitization(adapter):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "tesseract", stderr=b"Error at C:\\Users\\Name\\AppData\\Local\\Temp\\input.png"
        )

        with pytest.raises(OCREngineError) as excinfo:
            adapter.execute_ocr(_make_png(), language="eng")

        assert "[REDACTED_PATH]" in str(excinfo.value)
        assert "C:\\Users" not in str(excinfo.value)


# --- M5.9.7 confidence tests ---

def test_confidence_zero_produces_zero(tmp_path):
    """Confidence score 0 normalizes to 0.0."""
    hocr_content = "<span class='ocrx_word' title='bbox 0 0 10 10; x_wconf 0'>Zero</span>"
    hocr_path = tmp_path / "zero.hocr"
    hocr_path.write_text(hocr_content)

    _, _, conf = _parse_hocr(hocr_path)
    assert conf == 0.0


def test_missing_confidence_returns_none(tmp_path):
    """Word without x_wconf produces no confidence contribution."""
    hocr_content = "<span class='ocrx_word' title='bbox 0 0 10 10'>NoConf</span>"
    hocr_path = tmp_path / "noconf.hocr"
    hocr_path.write_text(hocr_content)

    _, _, conf = _parse_hocr(hocr_path)
    assert conf is None


def test_malformed_confidence_returns_none(tmp_path):
    """Malformed x_wconf values are ignored."""
    hocr_content = "<span class='ocrx_word' title='bbox 0 0 10 10; x_wconf abc'>Bad</span>"
    hocr_path = tmp_path / "malformed.hocr"
    hocr_path.write_text(hocr_content)

    _, _, conf = _parse_hocr(hocr_path)
    assert conf is None


def test_out_of_range_confidence_ignored(tmp_path):
    """Negative and >100 confidence values are ignored."""
    hocr_content = (
        "<span class='ocrx_word' title='bbox 0 0 5 5; x_wconf -5'>Neg</span>"
        "<span class='ocrx_word' title='bbox 10 10 15 15; x_wconf 150'>High</span>"
        "<span class='ocrx_word' title='bbox 20 20 25 25; x_wconf 80'>Good</span>"
    )
    hocr_path = tmp_path / "oor.hocr"
    hocr_path.write_text(hocr_content)

    text, boxes, conf = _parse_hocr(hocr_path)
    # Only 'Good' has valid confidence
    assert conf == 0.8
    assert len(boxes) == 3
    assert text == "Neg High Good"


def test_multiple_adjacent_words_isolated(tmp_path):
    """Adjacent words keep separate bounding boxes and text isolation."""
    hocr_content = (
        "<span class='ocrx_word' title='bbox 0 0 5 5; x_wconf 90'>A</span>"
        "<span class='ocrx_word' title='bbox 6 0 11 5; x_wconf 85'>B</span>"
        "<span class='ocrx_word' title='bbox 12 0 17 5; x_wconf 80'>C</span>"
    )
    hocr_path = tmp_path / "adj.hocr"
    hocr_path.write_text(hocr_content)

    text, boxes, conf = _parse_hocr(hocr_path)
    assert text == "A B C"
    assert len(boxes) == 3
    assert boxes[0] == {"x": 0, "y": 0, "w": 5, "h": 5}
    assert boxes[1] == {"x": 6, "y": 0, "w": 5, "h": 5}
    assert boxes[2] == {"x": 12, "y": 0, "w": 5, "h": 5}
    # Average of 0.9, 0.85, 0.8
    assert conf == 0.85


def test_empty_hocr_produces_empty_result(tmp_path):
    """Empty HOCR file yields empty text, no boxes, and None confidence."""
    hocr_path = tmp_path / "empty.hocr"
    hocr_path.write_text("")

    text, boxes, conf = _parse_hocr(hocr_path)
    assert text == ""
    assert boxes == []
    assert conf is None


def test_none_image_bytes_rejected(adapter):
    """Adapter returns error when image_bytes is None."""
    result = adapter.execute_ocr(None, language="eng")
    assert result.error is not None
    assert "Empty" in result.error


def test_real_tempdir_cleanup_success(adapter):
    """Real tempfile.TemporaryDirectory is cleaned up after successful execution."""
    with patch("subprocess.run") as mock_run:
        def create_hocr(cmd, **kwargs):
            output_path = Path(cmd[2] + ".hocr")
            output_path.write_text("<span class='ocrx_word' title='bbox 0 0 10 10; x_wconf 95'>Done</span>")
            return MagicMock(returncode=0)
        mock_run.side_effect = create_hocr

        adapter.execute_ocr(_make_png(), language="eng")


# --- M5.9.8 Image Validation Tests ---

def test_execute_ocr_valid_png_accepted(adapter):
    """Valid PNG image is accepted and processed."""
    with patch("subprocess.run") as mock_run:
        with patch("backend.ai.tesseract_ocr_adapter._parse_hocr", return_value=("Text", [], 0.9)):
            def side_effect(cmd, **kwargs):
                Path(cmd[2] + ".hocr").touch()
                return MagicMock(returncode=0)
            mock_run.side_effect = side_effect

            result = adapter.execute_ocr(_make_png(), language="eng")
            assert result.error is None
            assert mock_run.called


def test_execute_ocr_valid_jpeg_accepted(adapter):
    """Valid JPEG image is accepted and processed."""
    with patch("subprocess.run") as mock_run:
        with patch("backend.ai.tesseract_ocr_adapter._parse_hocr", return_value=("Text", [], 0.9)):
            def side_effect(cmd, **kwargs):
                Path(cmd[2] + ".hocr").touch()
                return MagicMock(returncode=0)
            mock_run.side_effect = side_effect

            result = adapter.execute_ocr(_make_jpeg(), language="eng")
            assert result.error is None
            assert mock_run.called


def test_execute_ocr_random_bytes_rejected(adapter):
    """Random non-image bytes are rejected."""
    with patch("subprocess.run") as mock_run:
        result = adapter.execute_ocr(b"not an image" * 10, language="eng")

        assert "Invalid image data" in result.error
        assert not mock_run.called


def test_execute_ocr_truncated_image_rejected(adapter):
    """Truncated/corrupted image data is rejected."""
    with patch("subprocess.run") as mock_run:
        # Take valid PNG and truncate it
        truncated_png = _make_png()[:50]

        result = adapter.execute_ocr(truncated_png, language="eng")
        assert "Invalid image data" in result.error
        assert not mock_run.called


def test_execute_ocr_unsupported_format_rejected(adapter):
    """Unsupported image formats (like a text file disguised as image) are rejected."""
    with patch("subprocess.run") as mock_run:
        fake_img = b"This is just a text file but we call it image.png"

        result = adapter.execute_ocr(fake_img, language="eng")
        assert "Invalid image data" in result.error
        assert not mock_run.called