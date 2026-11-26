import unittest
from unittest.mock import MagicMock, patch

from backend.ai.ocr_runtime import (
    OCRRuntime,
    OCRResult,
    OCREngineError,
    OCRCapability,
)


class TestOCRRuntimeProtocol(unittest.TestCase):
    """Verify OCRRuntime protocol structure and conventions."""

    def test_ocr_result_creation(self):
        """OCRResult can be created with all fields."""
        result = OCRResult(text="Hello World", confidence=0.95)
        self.assertEqual(result.text, "Hello World")
        self.assertEqual(result.confidence, 0.95)
        self.assertIsNone(result.error)

    def test_ocr_result_with_error(self):
        """OCRResult can carry an error message."""
        result = OCRResult(text="", confidence=0.0, error="Tessdata not found")
        self.assertEqual(result.text, "")
        self.assertEqual(result.error, "Tessdata not found")

    def test_ocr_result_defaults(self):
        """OCRResult uses defaults for optional fields."""
        result = OCRResult(text="Test")
        self.assertEqual(result.text, "Test")
        self.assertIsNone(result.confidence)
        self.assertIsNone(result.text_angle)
        self.assertEqual(result.detected_languages, ())
        self.assertEqual(result.bounding_boxes, [])
        self.assertIsNone(result.error)

    def test_ocre_engine_error_is_runtime_error(self):
        """OCREngineError inherits from RuntimeError."""
        self.assertIsInstance(OCREngineError(), RuntimeError)


class TestOCRRuntimeMock(unittest.TestCase):
    """Verify mock OCRRuntime satisfies the protocol."""

    def setUp(self):
        self.mock_runtime = MagicMock(spec=OCRRuntime)

    def test_mock_can_execute_ocr(self):
        """Mock OCRRuntime can be instructed to execute OCR."""
        self.mock_runtime.execute_ocr.return_value = OCRResult(
            text="Sample text", confidence=0.92
        )
        result = self.mock_runtime.execute_ocr(b"image_data", language="eng")
        self.assertEqual(result.text, "Sample text")
        self.assertEqual(result.confidence, 0.92)

    def test_mock_initialize(self):
        """Mock OCRRuntime initialize call."""
        self.mock_runtime.initialize()
        self.mock_runtime.initialize.assert_called_once()

    def test_mock_shutdown(self):
        """Mock OCRRuntime shutdown call."""
        self.mock_runtime.shutdown()
        self.mock_runtime.shutdown.assert_called_once()


class TestOCRCapability(unittest.TestCase):
    """Verify OCRCapability enum values."""

    def test_ocr_capability_name(self):
        self.assertEqual(OCRCapability.OCR.value, "ocr")

    def test_ocr_capability_members(self):
        self.assertIn(OCRCapability.OCR, list(OCRCapability))


if __name__ == "__main__":
    unittest.main()