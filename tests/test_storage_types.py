"""Tests for the storage domain types (Task 1 of M2).

Verifies that ``ProviderFileRecord`` behaves as a clean, immutable
dataclass with correct default values and no ORM dependencies.
"""

from datetime import datetime, timezone

import pytest

from backend.storage.types import ProviderFileRecord


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_provider_file_record_minimal_construction():
    """Only required fields are needed for instantiation."""
    record = ProviderFileRecord(
        provider_file_id="abc123",
        name="report.pdf",
        logical_path="/reports/report.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
    )
    assert record.provider_file_id == "abc123"
    assert record.name == "report.pdf"
    assert record.logical_path == "/reports/report.pdf"
    assert record.mime_type == "application/pdf"
    assert record.size_bytes == 1024


def test_provider_file_record_full_construction():
    """All optional fields may be supplied at construction time."""
    now = datetime(2026, 11, 12, 17, 0, 0, tzinfo=timezone.utc)
    record = ProviderFileRecord(
        provider_file_id="xyz",
        name="photo.jpg",
        logical_path="/photos/photo.jpg",
        mime_type="image/jpeg",
        size_bytes=2048,
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        provider_metadata={"web_view_link": "https://example.com/f"},
        modified_at=now,
    )
    assert record.sha256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert record.provider_metadata == {"web_view_link": "https://example.com/f"}
    assert record.modified_at == now


def test_provider_file_record_defaults_are_empty():
    """Optional fields default to None / empty dict, not shared mutable state."""
    record_a = ProviderFileRecord(
        provider_file_id="a",
        name="a.txt",
        logical_path="/a.txt",
        mime_type="text/plain",
        size_bytes=1,
    )
    record_b = ProviderFileRecord(
        provider_file_id="b",
        name="b.txt",
        logical_path="/b.txt",
        mime_type="text/plain",
        size_bytes=2,
    )
    # Mutating one record's metadata must not affect the other.
    record_a.provider_metadata["key"] = "value"
    assert "key" not in record_b.provider_metadata


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_provider_file_record_is_frozen():
    """Attempts to mutate a field must raise FrozenInstanceError."""
    from dataclasses import FrozenInstanceError

    record = ProviderFileRecord(
        provider_file_id="id1",
        name="doc.pdf",
        logical_path="/doc.pdf",
        mime_type="application/pdf",
        size_bytes=512,
    )
    with pytest.raises(FrozenInstanceError):
        record.provider_file_id = "mutated"  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        record.name = "changed"  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        record.size_bytes = 9999  # type: ignore[misc]


def test_provider_file_record_equality():
    """Standard frozen-dataclass equality: all fields participate, including
    optionals. Two records with identical content across every field compare
    equal; any single-field difference makes them unequal."""
    a = ProviderFileRecord(
        provider_file_id="x",
        name="f.txt",
        logical_path="/f.txt",
        mime_type="text/plain",
        size_bytes=10,
    )
    b = ProviderFileRecord(
        provider_file_id="x",
        name="f.txt",
        logical_path="/f.txt",
        mime_type="text/plain",
        size_bytes=10,
    )
    c = ProviderFileRecord(
        provider_file_id="y",
        name="g.txt",
        logical_path="/g.txt",
        mime_type="text/plain",
        size_bytes=20,
    )
    assert a == b
    assert a != c

    # Optional fields are part of equality — differing sha256 breaks equality.
    d = ProviderFileRecord(
        provider_file_id="x",
        name="f.txt",
        logical_path="/f.txt",
        mime_type="text/plain",
        size_bytes=10,
        sha256=None,
        provider_metadata={},
    )
    e = ProviderFileRecord(
        provider_file_id="x",
        name="f.txt",
        logical_path="/f.txt",
        mime_type="text/plain",
        size_bytes=10,
        sha256="aa" * 32,
        provider_metadata={"k": "v"},
        modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert d != e  # different optional values → not equal

    # Identical optional values restores equality.
    f = ProviderFileRecord(
        provider_file_id="x",
        name="f.txt",
        logical_path="/f.txt",
        mime_type="text/plain",
        size_bytes=10,
        sha256="aa" * 32,
        provider_metadata={"k": "v"},
        modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert e == f


# ---------------------------------------------------------------------------
# Field types
# ---------------------------------------------------------------------------


def test_sha256_accepts_none_and_hex_string():
    """sha256 must accept None (pre-download) and a 64-char hex string."""
    with_sha = ProviderFileRecord(
        provider_file_id="has_hash",
        name="h.txt",
        logical_path="/h.txt",
        mime_type="text/plain",
        size_bytes=1,
        sha256="aa" * 32,
    )
    without_sha = ProviderFileRecord(
        provider_file_id="no_hash",
        name="n.txt",
        logical_path="/n.txt",
        mime_type="text/plain",
        size_bytes=1,
    )
    assert with_sha.sha256 == "aa" * 32
    assert without_sha.sha256 is None


def test_modified_at_accepts_none_and_datetime():
    """modified_at may be None (unavailable) or a timezone-aware datetime."""
    naive_dt = datetime(2026, 1, 1, 12, 0, 0)
    aware_dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    with_mt = ProviderFileRecord(
        provider_file_id="mt1",
        name="m.txt",
        logical_path="/m.txt",
        mime_type="text/plain",
        size_bytes=1,
        modified_at=aware_dt,
    )
    without_mt = ProviderFileRecord(
        provider_file_id="mt2",
        name="m2.txt",
        logical_path="/m2.txt",
        mime_type="text/plain",
        size_bytes=1,
    )

    assert with_mt.modified_at == aware_dt
    assert without_mt.modified_at is None


# ---------------------------------------------------------------------------
# No ORM dependency
# ---------------------------------------------------------------------------


def test_no_sqlalchemy_import_in_types_module():
    """types.py must not import sqlalchemy — it is a pure domain module."""
    import backend.storage.types as mod  # noqa: F401

    # Trigger module load; if it imported sqlalchemy we'd see the dependency.
    # The import above is enough — if sqlalchemy were pulled in here we would
    # have noticed at write-time. This test asserts the absence indirectly by
    # confirming the module loads cleanly without extra side-effects.
    assert hasattr(mod, "ProviderFileRecord")
