from __future__ import annotations

import io
import re
import subprocess
import tempfile
from html.parser import HTMLParser
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from backend.ai.ocr_runtime import (
    OCRRuntime,
    OCRResult,
    OCREngineError,
)


def _validate_image(image_bytes: bytes) -> bool:
    """
    Validate that image_bytes contains a recognizable, loadable image.

    Uses a two-stage Pillow validation:
    1. img.verify() - checks header integrity (rejects structurally corrupted data)
    2. Reopen + img.load() - forces full pixel decoding (catches truncated/corrupted data
       that has a valid header but invalid body)

    Returns True if valid, raises ValueError otherwise.
    """
    try:
        # First open and verify header integrity
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()

        # Reopen with fresh BytesIO to force full pixel decoding
        # This catches images that have valid headers but corrupted/insufficient body data
        img2 = Image.open(io.BytesIO(image_bytes))
        img2.load()  # Force full decode - will raise if data is truncated/corrupted

        return True
    except Exception as e:
        raise ValueError(f"Invalid image data: {e}")


class _HOCRWordExtractor(HTMLParser):
    """Parser that extracts ocrx_word elements from HOCR output."""

    def __init__(self):
        super().__init__()
        self.words: List[Tuple[str, str]] = []  # list of (title, text)
        self._in_ocrx_word = False
        self._current_title = ""
        self._text_buffer: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "span":
            attr_dict = dict(attrs)
            cls = attr_dict.get("class", "")
            # Match ocrx_word regardless of attribute order
            classes = cls.split()
            if "ocrx_word" in classes:
                self._in_ocrx_word = True
                self._current_title = attr_dict.get("title", "")
                self._text_buffer = []

    def handle_endtag(self, tag):
        if self._in_ocrx_word and tag == "span":
            self.words.append((self._current_title, "".join(self._text_buffer)))
            self._in_ocrx_word = False
            self._current_title = ""
            self._text_buffer = []

    def handle_data(self, data):
        if self._in_ocrx_word:
            self._text_buffer.append(data)

    def handle_entityref(self, name):
        if self._in_ocrx_word:
            self._text_buffer.append(f"&{name};")

    def handle_charref(self, name):
        if self._in_ocrx_word:
            try:
                if name.startswith("x") or name.startswith("X"):
                    self._text_buffer.append(chr(int(name, 16)))
                else:
                    self._text_buffer.append(chr(int(name)))
            except (ValueError, OverflowError):
                pass


def _parse_hocr(hocr_path: Path) -> Tuple[str, List[Dict[str, int]], Optional[float]]:
    """
    Parse HOCR output file using HTML parser.

    Returns:
        - recognized text (concatenated words)
        - bounding boxes as [{'x': x, 'y': y, 'w': w, 'h': h}, ...]
        - average normalized confidence score (0.0 to 1.0) or None
    """
    with open(hocr_path, "r", encoding="utf-8") as f:
        content = f.read()

    extractor = _HOCRWordExtractor()
    extractor.feed(content)

    text_parts: List[str] = []
    bounding_boxes: List[Dict[str, int]] = []
    valid_confidences: List[float] = []

    for title, raw_text in extractor.words:
        # Parse bbox: "bbox x1 y1 x2 y2"
        bbox_match = re.search(r"bbox\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", title, re.IGNORECASE)
        if not bbox_match:
            continue

        x1, y1, x2, y2 = map(int, bbox_match.groups())
        if x2 <= x1 or y2 <= y1:
            continue  # Skip invalid bounding boxes

        w = x2 - x1
        h = y2 - y1

        # Parse confidence: "x_wconf score"
        conf_match = re.search(r"x_wconf\s+(-?\d+(?:\.\d+)?)", title, re.IGNORECASE)
        if conf_match:
            try:
                raw_conf = float(conf_match.group(1))
                if 0.0 <= raw_conf <= 100.0:
                    valid_confidences.append(raw_conf / 100.0)
            except ValueError:
                pass

        # Decode HTML entities and capture text
        word_text = unescape(raw_text).strip()
        if word_text:
            text_parts.append(word_text)

        bounding_boxes.append({"x": x1, "y": y1, "w": w, "h": h})

    full_text = " ".join(text_parts)

    avg_confidence: Optional[float] = None
    if valid_confidences:
        avg_confidence = round(sum(valid_confidences) / len(valid_confidences), 4)

    return full_text, bounding_boxes, avg_confidence


class TesseractOCRAdapter:
    """Standalone Tesseract OCR adapter using subprocess."""

    def __init__(self, binary_path: Path, tessdata_dir: Path):
        self.binary_path = binary_path
        self.tessdata_dir = tessdata_dir
        self._initialized = False

    def initialize(self) -> None:
        if not self.binary_path.exists():
            raise OCREngineError(f"Tesseract binary not found: {self.binary_path}")
        if not self.tessdata_dir.exists():
            raise OCREngineError(f"Tessdata directory not found: {self.tessdata_dir}")
        self._initialized = True

    def is_initialized(self) -> bool:
        return self._initialized

    def execute_ocr(self, image_bytes, language: str = "eng") -> OCRResult:
        if not self._initialized:
            raise OCREngineError("Adapter not initialized")

        # Validate image data
        if not image_bytes:
            return OCRResult(text="", error="Empty image data provided")

        # Verify image is valid using Pillow
        try:
            _validate_image(image_bytes)
        except ValueError as e:
            return OCRResult(text="", error=str(e))

        # Verify language data exists
        lang_file = self.tessdata_dir / f"{language}.traineddata"
        if not lang_file.exists():
            return OCRResult(text="", error=f"Language data missing: {language}")

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.png"
            output_base = Path(tmpdir) / "output"
            output_path = output_base.with_suffix(".hocr")

            with open(input_path, "wb") as f:
                f.write(image_bytes)

            # Construct command
            cmd = [
                str(self.binary_path),
                str(input_path),
                str(output_base),
                "-l", language,
                "--tessdata-dir", str(self.tessdata_dir),
                "hocr"
            ]

            try:
                # Bounded subprocess execution
                subprocess.run(cmd, check=True, capture_output=True, timeout=10)

                # Parse HOCR output
                if not output_path.exists():
                    return OCRResult(text="", error="No HOCR output generated")

                text, bounding_boxes, confidence = _parse_hocr(output_path)

                return OCRResult(
                    text=text,
                    confidence=confidence,
                    bounding_boxes=bounding_boxes,
                    error=None
                )

            except subprocess.TimeoutExpired:
                raise OCREngineError("Tesseract execution timed out")
            except subprocess.CalledProcessError as e:
                error_msg = e.stderr.decode() if e.stderr else "Unknown Tesseract error"
                # Sanitize error message - remove sensitive paths
                if "Error at " in error_msg:
                    error_msg = error_msg.split("Error at ", 1)[0] + "Error at [REDACTED_PATH]"
                raise OCREngineError(f"Tesseract failed: {error_msg.strip()}")

    def unload(self) -> None:
        self._initialized = False

    def shutdown(self) -> None:
        self.unload()

    def get_status(self) -> Dict[str, Any]:
        return {"initialized": self._initialized, "binary": str(self.binary_path)}