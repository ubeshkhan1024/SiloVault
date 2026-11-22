"""Focused tests for the basic AI text extraction service."""

import pytest

from backend.ai.text_extraction import (
    MAX_TEXT_EXTRACTION_BYTES,
    FileTooLargeError,
    InvalidContentError,
    InvalidEncodingError,
    TextExtractionService,
    UnsupportedFormatError,
)


@pytest.fixture
def service():
    return TextExtractionService()


@pytest.mark.parametrize(
    ("filename", "mime_type", "payload", "expected"),
    [
        ("notes.txt", "text/plain", "plain text\n", "plain text\n"),
        ("readme.md", "text/markdown", "# Heading\n\nBody\n", "# Heading\n\nBody\n"),
        ("data.csv", "text/csv", "a,b\n1,2\n", "a,b\n1,2\n"),
        (
            "config.json",
            "application/json",
            '{"name": "SiloVault"}\n',
            '{"name": "SiloVault"}\n',
        ),
    ],
)
def test_supported_formats_extract_text(service, filename, mime_type, payload, expected):
    result = service.extract(payload.encode("utf-8"), filename, mime_type)

    assert result.text == expected
    assert result.filename == filename
    assert result.mime_type == mime_type
    assert result.extension == "." + filename.rsplit(".", 1)[-1]
    assert result.byte_size == len(payload.encode("utf-8"))


def test_utf8_bom_is_removed(service):
    content = b"\xef\xbb\xbf" + "vault notes".encode("utf-8")

    result = service.extract(content, "notes.txt", "text/plain")

    assert result.text == "vault notes"
    assert not result.text.startswith("\ufeff")


def test_bom_only_content_is_rejected_as_empty(service):
    with pytest.raises(InvalidContentError, match="empty"):
        service.extract(b"\xef\xbb\xbf", "notes.txt", "text/plain")


def test_invalid_utf8_is_rejected(service):
    with pytest.raises(InvalidEncodingError, match="not valid UTF-8"):
        service.extract(b"caf\xe9 au lait", "notes.txt", "text/plain")


def test_invalid_encoding_is_an_invalid_content_error(service):
    with pytest.raises(InvalidContentError):
        service.extract(b"\xc3\x28", "notes.txt", "text/plain")


def test_empty_input_is_rejected(service):
    with pytest.raises(InvalidContentError, match="empty"):
        service.extract(b"", "notes.txt", "text/plain")


def test_unsupported_extension_is_rejected(service):
    with pytest.raises(UnsupportedFormatError, match="unsupported file extension"):
        service.extract(b"%PDF-1.7", "report.pdf", "text/plain")


def test_missing_extension_is_rejected(service):
    with pytest.raises(UnsupportedFormatError, match="no file extension"):
        service.extract(b"text", "notes", "text/plain")


@pytest.mark.parametrize("filename", ["", "   ", None, 42])
def test_invalid_filename_is_rejected(service, filename):
    with pytest.raises(UnsupportedFormatError):
        service.extract(b"text", filename, "text/plain")


@pytest.mark.parametrize(
    "mime_type",
    ["application/octet-stream", "application/pdf", "image/png", "", None],
)
def test_unsupported_mime_type_is_rejected(service, mime_type):
    with pytest.raises(UnsupportedFormatError, match="mime type"):
        service.extract(b"text", "notes.txt", mime_type)


def test_mime_type_must_match_the_extension(service):
    with pytest.raises(UnsupportedFormatError, match="unsupported mime type"):
        service.extract(b"a,b\n", "data.csv", "application/json")


def test_mime_type_parameters_and_case_are_normalised(service):
    result = service.extract(
        b"hello", "NOTES.TXT", "Text/Plain; charset=utf-8"
    )

    assert result.mime_type == "text/plain"
    assert result.extension == ".txt"
    assert result.text == "hello"


def test_content_larger_than_limit_is_rejected():
    service = TextExtractionService()
    content = b"a" * (MAX_TEXT_EXTRACTION_BYTES + 1)

    with pytest.raises(FileTooLargeError, match="maximum"):
        service.extract(content, "notes.txt", "text/plain")


def test_content_at_the_limit_is_accepted():
    service = TextExtractionService()
    content = b"a" * MAX_TEXT_EXTRACTION_BYTES

    result = service.extract(content, "notes.txt", "text/plain")

    assert result.byte_size == MAX_TEXT_EXTRACTION_BYTES
    assert len(result.text) == MAX_TEXT_EXTRACTION_BYTES


def test_configurable_limit_is_enforced():
    service = TextExtractionService(max_bytes=4)

    with pytest.raises(FileTooLargeError):
        service.extract(b"12345", "notes.txt", "text/plain")


def test_binary_content_with_nul_bytes_is_rejected(service):
    content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

    with pytest.raises(InvalidContentError, match="binary"):
        service.extract(content, "image.txt", "text/plain")


def test_binary_content_with_control_bytes_is_rejected(service):
    content = bytes(range(0, 32)) * 4

    with pytest.raises(InvalidContentError, match="binary"):
        service.extract(content, "blob.txt", "text/plain")


def test_supplied_input_bytes_are_not_modified(service):
    original = bytearray(b"col1,col2\n1,2\n")
    snapshot = bytes(original)

    result = service.extract(original, "data.csv", "text/csv")

    assert bytes(original) == snapshot
    assert result.text == snapshot.decode("utf-8")


def test_bytes_are_not_mutated_by_rejected_extraction(service):
    original = bytearray(b"caf\xe9")
    snapshot = bytes(original)

    with pytest.raises(InvalidEncodingError):
        service.extract(original, "notes.txt", "text/plain")

    assert bytes(original) == snapshot


@pytest.mark.parametrize("content", [None, "text", 42, ["text"]])
def test_non_bytes_content_is_rejected(service, content):
    with pytest.raises(TypeError, match="bytes-like"):
        service.extract(content, "notes.txt", "text/plain")


def test_text_content_is_preserved_verbatim(service):
    payload = "line one\r\nline two\n\nΩ unicode ✓\tTabbed"

    result = service.extract(payload.encode("utf-8"), "notes.md", "text/markdown")

    assert result.text == payload


def test_path_components_in_filename_are_ignored(service):
    result = service.extract(
        b"hello", "C:\\Users\\me\\notes.txt", "text/plain"
    )

    assert result.extension == ".txt"
    assert result.filename == "C:\\Users\\me\\notes.txt"


def test_whitespace_only_content_is_preserved(service):
    result = service.extract(b"  \n\t\n", "notes.txt", "text/plain")

    assert result.text == "  \n\t\n"


def test_extracted_text_is_immutable(service):
    result = service.extract(b"hello", "notes.txt", "text/plain")

    with pytest.raises(AttributeError):
        result.text = "changed"


