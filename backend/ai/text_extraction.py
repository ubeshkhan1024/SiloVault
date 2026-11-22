"""Provider-independent extraction of plain text from uploaded files.

The extractor works purely on in-memory bytes.  It never touches the
filesystem, the network, a model runtime, or any external service, and it does
not modify the bytes it is given.

Supported formats are deliberately limited to :data:`SUPPORTED_EXTENSIONS`:
``.txt``, ``.md``, ``.csv`` and ``.json``.  Content must be UTF-8 (an optional
UTF-8 BOM is accepted) and must look like text; binary payloads are rejected
rather than decoded leniently.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_TEXT_EXTRACTION_BYTES = 5 * 1024 * 1024

_TEXT_PLAIN = "text/plain"

#: Supported filename extensions mapped to the normalised MIME types accepted
#: for them.  ``text/plain`` is allowed for every supported text format.
SUPPORTED_EXTENSIONS: dict[str, frozenset[str]] = {
    ".txt": frozenset({_TEXT_PLAIN}),
    ".md": frozenset({_TEXT_PLAIN, "text/markdown", "text/x-markdown"}),
    ".csv": frozenset({_TEXT_PLAIN, "text/csv", "application/csv"}),
    ".json": frozenset({_TEXT_PLAIN, "application/json", "text/json"}),
}

UTF8_BOM = codecs.BOM_UTF8
_UTF8_BOM_OFFSET = len(UTF8_BOM)

# Control bytes that legitimately occur in plain text files.
_ALLOWED_CONTROL_BYTES = frozenset(b"\t\n\r\f")
# Fraction of disallowed control bytes above which content is treated as binary.
_MAX_CONTROL_BYTE_RATIO = 0.10


class TextExtractionError(ValueError):
    """Base class for all text extraction failures."""


class UnsupportedFormatError(TextExtractionError):
    """The filename extension or MIME type is not supported."""


class FileTooLargeError(TextExtractionError):
    """The supplied content exceeds :data:`MAX_TEXT_EXTRACTION_BYTES`."""


class InvalidContentError(TextExtractionError):
    """The content is empty or is not clearly text."""


class InvalidEncodingError(InvalidContentError):
    """The content is not valid UTF-8 text."""


@dataclass(frozen=True)
class ExtractedText:
    """Decoded text plus the metadata used to extract it."""

    text: str
    filename: str
    mime_type: str
    extension: str
    byte_size: int


class TextExtractionService:
    """Extracts UTF-8 text from supported in-memory file content.

    The service is stateless and holds no configuration, so a single instance
    can be shared safely.
    """

    def __init__(
        self, *, max_bytes: int = MAX_TEXT_EXTRACTION_BYTES
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError("max_bytes must be an integer")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        """Return the maximum accepted content size in bytes."""
        return self._max_bytes

    def extract(
        self, content: bytes, filename: str, mime_type: str
    ) -> ExtractedText:
        """Extract text from *content*.

        Raises :class:`UnsupportedFormatError` for an unsupported extension or
        MIME type, :class:`FileTooLargeError` when the content is larger than
        :attr:`max_bytes`, and :class:`InvalidContentError` (or its subclass
        :class:`InvalidEncodingError`) for empty, binary, or non-UTF-8 content.
        """
        data = _coerce_content(content)
        extension = _validate_filename(filename)
        normalised_mime = _validate_mime_type(mime_type, extension)
        _require_size(data, self._max_bytes)
        _require_textual(data)
        return ExtractedText(
            text=_decode_utf8(data),
            filename=filename,
            mime_type=normalised_mime,
            extension=extension,
            byte_size=len(data),
        )


def _coerce_content(content: bytes) -> bytes:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TypeError("content must be bytes-like")
    # A copy keeps the caller's buffer untouched and immune to later mutation.
    return bytes(content)


def _validate_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise UnsupportedFormatError("filename must be a non-empty string")
    # Accept both separators so Windows-style names reduce to their basename.
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not basename or basename in {".", ".."}:
        raise UnsupportedFormatError(f"filename '{filename}' has no usable name")
    extension = PurePosixPath(basename).suffix.casefold()
    if not extension:
        raise UnsupportedFormatError(
            f"filename '{filename}' has no file extension"
        )
    if extension not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise UnsupportedFormatError(
            f"unsupported file extension '{extension}'; supported: {supported}"
        )
    return extension


def _validate_mime_type(mime_type: str, extension: str) -> str:
    if not isinstance(mime_type, str) or not mime_type.strip():
        raise UnsupportedFormatError("mime type must be a non-empty string")
    normalised = mime_type.split(";", 1)[0].strip().casefold()
    allowed = SUPPORTED_EXTENSIONS[extension]
    if normalised not in allowed:
        raise UnsupportedFormatError(
            f"unsupported mime type '{normalised}' for '{extension}'"
        )
    return normalised


def _require_size(data: bytes, max_bytes: int) -> None:
    if len(data) > max_bytes:
        raise FileTooLargeError(
            f"content is {len(data)} bytes; the maximum is {max_bytes} bytes"
        )


def _require_textual(data: bytes) -> None:
    if not data:
        raise InvalidContentError("content is empty")
    body = _strip_bom(data)
    if not body:
        raise InvalidContentError("content is empty")
    if b"\x00" in body:
        raise InvalidContentError("content contains binary data")
    disallowed = sum(
        1
        for byte in body
        if byte < 0x20 and byte not in _ALLOWED_CONTROL_BYTES
    )
    if disallowed / len(body) > _MAX_CONTROL_BYTE_RATIO:
        raise InvalidContentError("content contains binary data")


def _strip_bom(data: bytes) -> bytes:
    if data.startswith(UTF8_BOM):
        return data[_UTF8_BOM_OFFSET:]
    return data


def _decode_utf8(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidEncodingError(
            f"content is not valid UTF-8: {exc.reason} at byte {exc.start}"
        ) from exc
    if not text:
        raise InvalidContentError("content is empty")
    return text

