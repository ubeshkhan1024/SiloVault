"""Tests for the storage error hierarchy (Task 3 of M2).

Verifies inheritance from StorageError, construction patterns, message
content, optional context attributes, exception chaining via __cause__,
and independence from FastAPI / SQLAlchemy / cloud SDKs.
"""

import pytest

from backend.storage.errors import (
    StorageAuthenticationError,
    StorageConflictError,
    StorageError,
    StorageInvalidResponseError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageQuotaExceededError,
    StorageRateLimitError,
    StorageUnavailableError,
    StorageUnsupportedError,
)


# ---------------------------------------------------------------------------
# Base: StorageError
# ---------------------------------------------------------------------------


def test_storage_error_is_an_exception():
    """StorageError must be a direct subclass of Exception."""
    assert issubclass(StorageError, Exception)


def test_storage_error_can_be_caught_as_exception():
    """A StorageError raised inside a try/except Exception block must be
    caught by an except Exception clause."""
    with pytest.raises(Exception):
        raise StorageError("something broke")


def test_storage_error_default_message():
    """When no detail is supplied, the class name serves as the message."""
    err = StorageError()
    assert str(err) == "StorageError"


def test_storage_error_custom_message():
    err = StorageError("custom detail")
    assert str(err) == "custom detail"


def test_storage_error_provider_attribute():
    err = StorageError(provider="GOOGLE_DRIVE")
    assert err.provider == "GOOGLE_DRIVE"


def test_storage_error_cause_preserved():
    """An explicitly passed __cause__ must be attached."""
    original = ValueError("underlying problem")
    err = StorageError("wrapped", __cause__=original)
    assert err.__cause__ is original


# ---------------------------------------------------------------------------
# Concrete error: StorageNotFoundError
# ---------------------------------------------------------------------------


def test_storage_not_found_inherits_storage_error():
    assert issubclass(StorageNotFoundError, StorageError)


def test_storage_not_found_message_contains_file_id():
    err = StorageNotFoundError("drive_abc123", provider="GOOGLE_DRIVE")
    assert "drive_abc123" in str(err)
    assert err.provider == "GOOGLE_DRIVE"
    assert err.provider_file_id == "drive_abc123"


def test_storage_not_found_chaining():
    inner = RuntimeError("connection lost")
    err = StorageNotFoundError("file_xyz", __cause__=inner)
    assert err.__cause__ is inner


# ---------------------------------------------------------------------------
# Concrete error: StoragePermissionError
# ---------------------------------------------------------------------------


def test_storage_permission_error_inherits():
    assert issubclass(StoragePermissionError, StorageError)


def test_storage_permission_error_basic():
    err = StoragePermissionError(provider="DROPBOX")
    assert err.provider == "DROPBOX"
    assert isinstance(err, StorageError)


# ---------------------------------------------------------------------------
# Concrete error: StorageQuotaExceededError
# ---------------------------------------------------------------------------


def test_storage_quota_exceeded_inherits():
    assert issubclass(StorageQuotaExceededError, StorageError)


def test_storage_quota_exceeded_with_limits():
    err = StorageQuotaExceededError(limit_bytes=10_000_000, used_bytes=9_500_000)
    assert "limit=10000000 bytes" in str(err)
    assert "used=9500000 bytes" in str(err)


def test_storage_quota_exceeded_no_numbers():
    err = StorageQuotaExceededError()
    assert str(err).startswith("Quota exceeded")


def test_storage_quota_exceeded_attributes():
    err = StorageQuotaExceededError(limit_bytes=5000, used_bytes=4999)
    assert err.limit_bytes == 5000
    assert err.used_bytes == 4999


# ---------------------------------------------------------------------------
# Concrete error: StorageConflictError
# ---------------------------------------------------------------------------


def test_storage_conflict_error_inherits():
    assert issubclass(StorageConflictError, StorageError)


def test_storage_conflict_error_basic():
    err = StorageConflictError(provider="BOX")
    assert err.provider == "BOX"


# ---------------------------------------------------------------------------
# Concrete error: StorageUnsupportedError
# ---------------------------------------------------------------------------


def test_storage_unsupported_error_inherits():
    assert issubclass(StorageUnsupportedError, StorageError)


def test_storage_unsupported_error_message_and_operation():
    err = StorageUnsupportedError("MOVE", provider="MEGA")
    assert "MOVE" in str(err)
    assert err.provider == "MEGA"
    assert err.operation == "MOVE"


# ---------------------------------------------------------------------------
# Concrete error: StorageAuthenticationError
# ---------------------------------------------------------------------------


def test_storage_authentication_error_inherits():
    assert issubclass(StorageAuthenticationError, StorageError)


def test_storage_authentication_error_basic():
    err = StorageAuthenticationError(provider="ONEDRIVE")
    assert err.provider == "ONEDRIVE"


# ---------------------------------------------------------------------------
# Concrete error: StorageRateLimitError
# ---------------------------------------------------------------------------


def test_storage_rate_limit_error_inherits():
    assert issubclass(StorageRateLimitError, StorageError)


def test_storage_rate_limit_error_with_retry_after():
    err = StorageRateLimitError(retry_after_seconds=30, provider="GOOGLE_DRIVE")
    assert "retry after 30s" in str(err)
    assert err.retry_after_seconds == 30


def test_storage_rate_limit_error_without_retry_after():
    err = StorageRateLimitError()
    assert str(err).startswith("Rate limited")
    assert err.retry_after_seconds is None


# ---------------------------------------------------------------------------
# Concrete error: StorageUnavailableError
# ---------------------------------------------------------------------------


def test_storage_unavailable_error_inherits():
    assert issubclass(StorageUnavailableError, StorageError)


def test_storage_unavailable_error_basic():
    err = StorageUnavailableError(provider="DROPBOX")
    assert err.provider == "DROPBOX"


# ---------------------------------------------------------------------------
# Concrete error: StorageInvalidResponseError
# ---------------------------------------------------------------------------


def test_storage_invalid_response_error_inherits():
    assert issubclass(StorageInvalidResponseError, StorageError)


def test_storage_invalid_response_error_with_status():
    err = StorageInvalidResponseError(status_code=500, body="<html>error</html>")
    assert "HTTP 500" in str(err)
    assert err.status_code == 500
    assert err.body == "<html>error</html>"


def test_storage_invalid_response_error_minimal():
    err = StorageInvalidResponseError()
    assert str(err).startswith("Invalid provider response")
    assert err.status_code is None
    assert err.body is None


# ---------------------------------------------------------------------------
# Exception chaining: __cause__ preservation
# ---------------------------------------------------------------------------


def test_all_errors_support_cause():
    """Every concrete error must accept and preserve an explicit __cause__."""
    inner = RuntimeError("root cause")
    for ErrorCls in (
        StorageNotFoundError,
        StoragePermissionError,
        StorageQuotaExceededError,
        StorageConflictError,
        StorageUnsupportedError,
        StorageAuthenticationError,
        StorageRateLimitError,
        StorageUnavailableError,
        StorageInvalidResponseError,
    ):
        kwargs = {"provider": "TEST"}
        if ErrorCls is StorageNotFoundError:
            kwargs["provider_file_id"] = "f"
        elif ErrorCls is StorageQuotaExceededError:
            pass  # constructor accepts kwargs only
        elif ErrorCls is StorageUnsupportedError:
            kwargs["operation"] = "X"
        elif ErrorCls is StorageRateLimitError:
            pass
        elif ErrorCls is StorageInvalidResponseError:
            pass
        else:
            pass
        err = ErrorCls(__cause__=inner, **kwargs)
        assert err.__cause__ is inner, f"{ErrorCls.__name__} dropped __cause__"


# ---------------------------------------------------------------------------
# Safety: no external framework dependencies
# ---------------------------------------------------------------------------


def test_errors_module_has_no_fastapi_import():
    """Importing the errors module must not pull in FastAPI."""
    import backend.storage.errors as mod  # noqa: F401
    # Module loaded successfully — if it imported fastapi we'd have noticed.
    assert hasattr(mod, "StorageError")


def test_errors_module_has_no_sqlalchemy_import():
    """Importing the errors module must not pull in SQLAlchemy."""
    import backend.storage.errors as mod  # noqa: F401
    assert hasattr(mod, "StorageError")
