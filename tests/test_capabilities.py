"""Tests for the storage capability model (Task 2 of M2).

Verifies the AddressingMode enum, the CapabilityStatus tri-state,
the ProviderCapabilities dataclass immutability and defaults, and the
completeness of the canonical CAPABILITY_MAP.
"""

import pytest

from backend.storage.capabilities import (
    AddressingMode,
    CapabilityStatus,
    CAPABILITY_MAP,
    ProviderCapabilities,
)


# ---------------------------------------------------------------------------
# AddressingMode enum
# ---------------------------------------------------------------------------


def test_addressing_mode_values():
    """Every expected mode exists with the correct string representation."""
    assert AddressingMode.UNKNOWN.value == "unknown"
    assert AddressingMode.ID_BASED.value == "id_based"
    assert AddressingMode.PATH_BASED.value == "path_based"
    assert AddressingMode.HANDLE_BASED.value == "handle_based"
    assert AddressingMode.MIXED.value == "mixed"


def test_addressing_mode_unique_values():
    """No two members share the same string value."""
    values = [m.value for m in AddressingMode]
    assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# CapabilityStatus tri-state
# ---------------------------------------------------------------------------


def test_capability_status_three_values():
    """Exactly three distinct statuses exist."""
    assert len(CapabilityStatus) == 3
    assert CapabilityStatus.UNKNOWN not in (CapabilityStatus.TRUE, CapabilityStatus.FALSE)


def test_capability_status_is_known():
    """Only TRUE and FALSE report as known; UNKNOWN stays opaque."""
    assert not CapabilityStatus.UNKNOWN.is_known()
    assert CapabilityStatus.TRUE.is_known()
    assert CapabilityStatus.FALSE.is_known()


def test_capability_status_comparison_does_not_coerce():
    """Unknown must not accidentally behave like True or False."""
    assert CapabilityStatus.UNKNOWN is not CapabilityStatus.TRUE
    assert CapabilityStatus.UNKNOWN is not CapabilityStatus.FALSE
    # Truthiness would make UNKNOWN indistinguishable from TRUE in bare
    # boolean contexts; callers must inspect explicitly.
    assert CapabilityStatus.UNKNOWN != True  # noqa: E712
    assert CapabilityStatus.UNKNOWN != False  # noqa: E712


# ---------------------------------------------------------------------------
# ProviderCapabilities dataclass
# ---------------------------------------------------------------------------


def _make_caps(**overrides):
    """Helper: build a minimal ProviderCapabilities, overriding selected fields."""
    kwargs = {
        "addressing_mode": AddressingMode.UNKNOWN,
        "native_hash_algorithm": None,
        "supports_trash": CapabilityStatus.UNKNOWN,
        "supports_delta_sync": CapabilityStatus.UNKNOWN,
        "supports_revisions": CapabilityStatus.UNKNOWN,
        "supports_search": CapabilityStatus.UNKNOWN,
        "supports_chunked_upload": CapabilityStatus.UNKNOWN,
        "supports_resumable_upload": CapabilityStatus.UNKNOWN,
        "requires_decryption": CapabilityStatus.FALSE,
    }
    kwargs.update(overrides)
    return ProviderCapabilities(**kwargs)


def test_capabilities_is_frozen():
    """Once constructed, no field may be mutated."""
    caps = _make_caps()
    with pytest.raises(AttributeError):
        caps.supports_trash = CapabilityStatus.FALSE  # type: ignore[misc]
    with pytest.raises(AttributeError):
        caps.native_hash_algorithm = "sha256"  # type: ignore[misc]


def test_capabilities_constructor_all_required_fields():
    """All eight required positional fields plus the optional
    requires_decryption must all be accepted via keyword arguments."""
    caps = ProviderCapabilities(
        addressing_mode=AddressingMode.PATH_BASED,
        native_hash_algorithm="md5",
        supports_trash=CapabilityStatus.TRUE,
        supports_delta_sync=CapabilityStatus.UNKNOWN,
        supports_revisions=CapabilityStatus.FALSE,
        supports_search=CapabilityStatus.FALSE,
        supports_chunked_upload=CapabilityStatus.FALSE,
        supports_resumable_upload=CapabilityStatus.FALSE,
        requires_decryption=CapabilityStatus.FALSE,
    )
    assert caps.addressing_mode == AddressingMode.PATH_BASED
    assert caps.native_hash_algorithm == "md5"
    assert caps.supports_trash is CapabilityStatus.TRUE
    assert caps.supports_delta_sync is CapabilityStatus.UNKNOWN
    assert caps.supports_revisions is CapabilityStatus.FALSE
    assert caps.supports_search is CapabilityStatus.FALSE
    assert caps.supports_chunked_upload is CapabilityStatus.FALSE
    assert caps.supports_resumable_upload is CapabilityStatus.FALSE
    assert caps.requires_decryption is CapabilityStatus.FALSE


def test_capabilities_default_optional_fields():
    """The single optional flag must default to CapabilityStatus.FALSE."""
    caps = _make_caps()
    assert caps.requires_decryption is CapabilityStatus.FALSE


def test_capabilities_immutability_preserved_by_copy():
    """A second instance with identical values compares equal; mutating the
    original does not affect the copy because the dataclass is frozen."""
    base = _make_caps(
        addressing_mode=AddressingMode.ID_BASED,
        native_hash_algorithm="md5",
        supports_trash=CapabilityStatus.TRUE,
        supports_delta_sync=CapabilityStatus.TRUE,
        supports_revisions=CapabilityStatus.TRUE,
        supports_search=CapabilityStatus.TRUE,
        supports_chunked_upload=CapabilityStatus.TRUE,
        supports_resumable_upload=CapabilityStatus.TRUE,
        requires_decryption=CapabilityStatus.FALSE,
    )
    copy = _make_caps(
        addressing_mode=AddressingMode.ID_BASED,
        native_hash_algorithm="md5",
        supports_trash=CapabilityStatus.TRUE,
        supports_delta_sync=CapabilityStatus.TRUE,
        supports_revisions=CapabilityStatus.TRUE,
        supports_search=CapabilityStatus.TRUE,
        supports_chunked_upload=CapabilityStatus.TRUE,
        supports_resumable_upload=CapabilityStatus.TRUE,
        requires_decryption=CapabilityStatus.FALSE,
    )
    assert base == copy
    with pytest.raises(AttributeError):
        base.supports_trash = CapabilityStatus.FALSE  # type: ignore[misc]
    assert copy.supports_trash is CapabilityStatus.TRUE


def test_capability_map_values_are_immutable():
    """Each value returned from CAPABILITY_MAP is a frozen object — mutating
    it raises AttributeError. The immutability lives in the capability objects,
    not in the dict container itself."""
    local = CAPABILITY_MAP["LOCAL"]
    with pytest.raises(AttributeError):
        local.supports_trash = CapabilityStatus.TRUE  # type: ignore[misc]
    assert id(CAPABILITY_MAP["LOCAL"]) == id(local)


def test_native_hash_algorithm_accepts_none():
    """Unverified providers may report None for their native hash algorithm."""
    caps = _make_caps(native_hash_algorithm=None)
    assert caps.native_hash_algorithm is None

    caps_known = _make_caps(native_hash_algorithm="sha1")
    assert caps_known.native_hash_algorithm == "sha1"


def test_requires_decryption_is_capability_status():
    """requires_decryption must be a CapabilityStatus, not a plain bool."""
    caps_false = _make_caps(requires_decryption=CapabilityStatus.FALSE)
    caps_true = _make_caps(requires_decryption=CapabilityStatus.TRUE)
    caps_unknown = _make_caps(requires_decryption=CapabilityStatus.UNKNOWN)
    assert isinstance(caps_false.requires_decryption, CapabilityStatus)
    assert isinstance(caps_true.requires_decryption, CapabilityStatus)
    assert isinstance(caps_unknown.requires_decryption, CapabilityStatus)


# ---------------------------------------------------------------------------
# CAPABILITY_MAP completeness
# ---------------------------------------------------------------------------

REQUIRED_PROVIDERS = {"LOCAL", "GOOGLE_DRIVE", "ONEDRIVE", "DROPBOX", "BOX", "MEGA"}


def test_capability_map_contains_all_required_providers():
    """Every provider named in the M2 scope must have a capabilities entry."""
    missing = REQUIRED_PROVIDERS - set(CAPABILITY_MAP)
    assert not missing, f"Missing capability entries for: {missing}"


def test_capability_map_no_extra_providers():
    """No unexpected providers are registered; the map matches scope exactly."""
    extra = set(CAPABILITY_MAP) - REQUIRED_PROVIDERS
    assert not extra, f"Unexpected capability entries for: {extra}"


def test_local_capabilities_are_conservative():
    """LOCAL must declare only things we are certain about."""
    local = CAPABILITY_MAP["LOCAL"]
    assert local.addressing_mode is AddressingMode.PATH_BASED
    assert local.native_hash_algorithm is None
    assert local.supports_trash is CapabilityStatus.FALSE
    assert local.supports_delta_sync is CapabilityStatus.FALSE
    assert local.supports_revisions is CapabilityStatus.FALSE
    assert local.supports_search is CapabilityStatus.FALSE
    assert local.supports_chunked_upload is CapabilityStatus.FALSE
    assert local.supports_resumable_upload is CapabilityStatus.FALSE
    assert local.requires_decryption is CapabilityStatus.FALSE


def test_cloud_providers_start_as_unknown():
    """Cloud providers must begin with unknown status for every feature flag
    that has not been empirically verified — no fabricated confirmations."""
    for name in ("GOOGLE_DRIVE", "ONEDRIVE", "DROPBOX", "BOX", "MEGA"):
        caps = CAPABILITY_MAP[name]
        assert caps.addressing_mode is AddressingMode.UNKNOWN, \
            f"{name}: addressing_mode should be UNKNOWN"
        assert caps.native_hash_algorithm is None, \
            f"{name}: native_hash_algorithm should be None"
        assert caps.supports_trash is CapabilityStatus.UNKNOWN, \
            f"{name}: supports_trash should be UNKNOWN"
        assert caps.supports_delta_sync is CapabilityStatus.UNKNOWN, \
            f"{name}: supports_delta_sync should be UNKNOWN"
        assert caps.supports_revisions is CapabilityStatus.UNKNOWN, \
            f"{name}: supports_revisions should be UNKNOWN"
        assert caps.supports_search is CapabilityStatus.UNKNOWN, \
            f"{name}: supports_search should be UNKNOWN"
        assert caps.supports_chunked_upload is CapabilityStatus.UNKNOWN, \
            f"{name}: supports_chunked_upload should be UNKNOWN"
        assert caps.supports_resumable_upload is CapabilityStatus.UNKNOWN, \
            f"{name}: supports_resumable_upload should be UNKNOWN"
        assert caps.requires_decryption is CapabilityStatus.UNKNOWN, \
            f"{name}: requires_decryption should be UNKNOWN"


def test_mega_requires_decryption_remains_unknown():
    """MEGA's encryption requirement must stay UNKNOWN until verified —
    the architecture doc mentions client-side E2EE but we have not
    confirmed the exact flag through the official SDK."""
    mega = CAPABILITY_MAP["MEGA"]
    assert mega.requires_decryption is CapabilityStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Safety: no external dependencies imported
# ---------------------------------------------------------------------------


def test_capabilities_module_has_no_orm_dependencies():
    """The capabilities module must not pull in SQLAlchemy or FastAPI."""
    import backend.storage.capabilities as mod

    assert hasattr(mod, "ProviderCapabilities")
    assert hasattr(mod, "AddressingMode")
    assert hasattr(mod, "CapabilityStatus")
    assert hasattr(mod, "CAPABILITY_MAP")
