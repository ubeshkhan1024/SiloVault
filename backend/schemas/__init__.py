"""Pydantic schemas for requests and responses."""

import copy
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FileMetadata(BaseModel):
    """Unified file metadata response schema."""

    id: str = Field(..., description="Internal UUID primary key (file_<UUID>)")
    provider: str = Field(..., description="Storage provider name")
    provider_account_id: Optional[str] = Field(
        None, description="Provider account identifier"
    )
    provider_file_id: str = Field(..., description="Provider-specific file identifier")
    name: str = Field(..., description="File name")
    logical_path: str = Field(..., description="Logical path within the provider")
    mime_type: str = Field(..., description="MIME type")
    size_bytes: int = Field(..., description="File size in bytes")
    sha256: Optional[str] = Field(None, description="SHA-256 content hash")
    category: Optional[str] = Field(None, description="File category")
    tags: List[str] = Field(default_factory=list, description="User tags")
    sync_state: str = Field(..., description="Sync state machine value")
    created_at: datetime = Field(..., description="Creation timestamp (UTC)")
    updated_at: datetime = Field(..., description="Last update timestamp (UTC)")
    last_indexed_at: Optional[datetime] = Field(
        None, description="Last indexing timestamp (UTC)"
    )
    provider_metadata: Optional[Dict[str, Any]] = Field(
        None, description="Provider-specific metadata"
    )

    model_config = ConfigDict(from_attributes=True)


class FileListResponse(BaseModel):
    """Paginated file list response."""

    items: List[FileMetadata]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Rename / Move request schemas
# ---------------------------------------------------------------------------

class RenameRequest(BaseModel):
    """Request body for the rename endpoint."""

    new_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="New user-facing filename for the file.",
    )

    model_config = ConfigDict(str_strip_whitespace=True)


class MoveRequest(BaseModel):
    """Request body for the logical move endpoint."""

    target_logical_path: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="New logical path (directory prefix + filename) for the file.",
    )

    model_config = ConfigDict(str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# AI Task schemas
# ---------------------------------------------------------------------------

class AiTaskCreateRequest(BaseModel):
    """Request body for creating an AI task."""

    file_id: str = Field(..., description="ID of the file to process")
    task_type: str = Field(..., description="Type of AI task (e.g. 'text_classification')")


class AiTaskResponse(BaseModel):
    """AI task details response."""

    id: str
    file_id: Optional[str]
    task_type: str
    status: str
    retries: int
    max_retries: int
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    proposal_json: Optional[Dict[str, Any]]
    error_message: Optional[str]
    decision: Optional[str]
    decided_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class AiTaskListResponse(BaseModel):
    """Paginated AI task list response."""

    items: List[AiTaskResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Activity schemas
# ---------------------------------------------------------------------------

class ActivityRecordSchema(BaseModel):
    """Single activity / audit-log entry."""

    id: str = Field(..., description="Internal UUID primary key (act_<UUID>)")
    action_type: str = Field(..., description="Operation kind: UPLOAD, DOWNLOAD, DELETE")
    file_id: Optional[str] = Field(None, description="Unified file ID (NULL after DELETE)")
    file_name: str = Field(..., description="Snapshot of the original filename")
    provider: str = Field(..., description="Storage provider the operation targeted")
    status: str = Field(..., description="Operation status")
    timestamp: datetime = Field(..., description="Operation timestamp (UTC, timezone-aware)")
    details: Optional[Dict[str, Any]] = Field(None, description="Optional free-form context")

    model_config = ConfigDict(from_attributes=True)


class ActivityListResponse(BaseModel):
    """Paginated activity list response."""

    items: List[ActivityRecordSchema]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Storage account schemas (HVSA H1.5.3)
# ---------------------------------------------------------------------------

# Same canonical provider pattern as backend.storage.registry and
# backend.storage.account_manager, and the same width as the
# storage_accounts.provider_id column.
_PROVIDER_ID_PATTERN = r"^[A-Z][A-Z0-9_]*$"
_PROVIDER_ID_MAX_LENGTH = 64
_DISPLAY_NAME_MAX_LENGTH = 255


def _normalize_display_name(value: Optional[str]) -> Optional[str]:
    """Trim a display name and reject empty/whitespace-only values.

    Mirrors ``AccountManager``, which stores names trimmed and never accepts
    a blank one.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("display_name must be a non-empty string")
    return stripped


class StorageAccountCreate(BaseModel):
    """Request body for creating a storage account.

    ``credentials_ref`` is deliberately absent: references are generated
    server-side, so a client can never select another account's keyring entry.
    Unknown fields are rejected instead of ignored.
    """

    provider_id: str = Field(
        ...,
        min_length=1,
        max_length=_PROVIDER_ID_MAX_LENGTH,
        pattern=_PROVIDER_ID_PATTERN,
        description="Canonical provider identifier (e.g. 'LOCAL', 'GOOGLE_DRIVE')",
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=_DISPLAY_NAME_MAX_LENGTH,
        description="User-defined account name (stored trimmed)",
    )
    configuration: Dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific configuration (JSON object)",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("display_name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        return _normalize_display_name(value)  # type: ignore[return-value]


class StorageAccountUpdate(BaseModel):
    """Request body for a partial storage-account update (PATCH).

    Every field is optional; omitted fields are left unchanged.  ``provider_id``
    and ``credentials_ref`` are intentionally not accepted: provider identity
    cannot change through an update, and credential references stay
    server-generated.  Unknown fields are rejected instead of ignored.
    """

    display_name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=_DISPLAY_NAME_MAX_LENGTH,
        description="New user-defined account name (stored trimmed)",
    )
    configuration: Optional[Dict[str, Any]] = Field(
        None,
        description="Replacement provider configuration (JSON object)",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("display_name")
    @classmethod
    def _normalize_name(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_display_name(value)


class StorageAccountResponse(BaseModel):
    """Storage account response.

    Excludes ``credentials_ref`` and every credential/token field: account
    responses are not a channel for keyring metadata.  ``configuration`` is
    deep-copied on validation so callers cannot mutate ORM state.
    """

    id: str
    provider_id: str
    display_name: str
    status: str
    configuration: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("configuration")
    @classmethod
    def _defensive_copy(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return copy.deepcopy(value)


class StorageAccountListResponse(BaseModel):
    """Storage account collection response.

    Non-paginated: ``AccountManager.list_accounts`` returns every account, so
    no ``limit``/``offset`` are advertised.
    """

    items: List[StorageAccountResponse]
    total: int


__all__: list[str] = [
    "FileMetadata",
    "FileListResponse",
    "RenameRequest",
    "MoveRequest",
    "AiTaskCreateRequest",
    "AiTaskResponse",
    "AiTaskListResponse",
    "ActivityRecordSchema",
    "ActivityListResponse",
    "StorageAccountCreate",
    "StorageAccountUpdate",
    "StorageAccountResponse",
    "StorageAccountListResponse",
]
