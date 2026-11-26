from __future__ import annotations


"""OCR runtime protocol and result types for SiloVault M5.9.2.

This module defines an independent OCR runtime protocol and result types,
entirely separate from the VisionRuntime protocol used by Florence-2.
No existing code is modified; this is a new foundation for OCR pack integration.
"""


import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union, runtime_checkable

from backend.ai.interfaces import ModelDescriptor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OCR capability enum
# ---------------------------------------------------------------------------

class OCRCapability(Enum):
    """Capabilities an OCR runtime may expose."""
    OCR = "ocr"


# ---------------------------------------------------------------------------
# OCR result dataclass
# ---------------------------------------------------------------------------

@dataclass
class OCRResult:
    """Result of OCR inference execution.

    Attributes:
        text: Extracted text string.
        confidence: Average confidence score in [0.0, 1.0]; None when unknown.
        text_angle: Page skew angle in degrees; None when unknown.
        detected_languages: Language codes detected during inference.
        bounding_boxes: List of dicts with keys x, y, w, h for text blocks.
        error: Error message when OCR fails; None when successful.
    """
    text: str
    confidence: Optional[float] = None
    text_angle: Optional[float] = None
    detected_languages: Tuple[str, ...] = ()
    bounding_boxes: List[Dict[str, int]] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# OCR engine error
# ---------------------------------------------------------------------------

class OCREngineError(RuntimeError):
    """Raised for unrecoverable OCR engine or system errors.

    Used when the OCR binary is missing, tessdata is corrupted, or a
    fundamental system failure prevents OCR from running.  Callers should
    present this to the user rather than silently returning empty results.
    """
    pass


# ---------------------------------------------------------------------------
# OCRRuntime protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class OCRRuntime(Protocol):
    """Protocol for local OCR runtime implementations.

    This protocol is entirely independent of VisionRuntime (Florence-2).
    Runtimes may implement one or both protocols.
    """

    @property
    def name(self) -> str:
        ...  # e.g. "tesseract-ocr"

    @property
    def version(self) -> str:
        ...  # e.g. "1.0.0"

    def initialize(self) -> None:
        ...  # Initialize OCR engine (load tessdata, etc.)

    def is_initialized(self) -> bool:
        ...  # Return True if engine is initialized and ready

    def execute_ocr(self, image_data: bytes, language: str = "eng") -> OCRResult:
        ...  # Execute OCR on image data with the given language tag

    def unload(self) -> None:
        ...  # Release engine resources

    def shutdown(self) -> None:
        ...  # Clean shutdown of the engine

    def get_status(self) -> Dict[str, Any]:
        ...  # Return runtime state (initialized?, last error?, etc.)
