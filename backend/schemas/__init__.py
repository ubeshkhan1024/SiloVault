"""Pydantic schemas for requests and responses."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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

    class Config:
        from_attributes = True


class FileListResponse(BaseModel):
    """Paginated file list response."""

    items: List[FileMetadata]
    total: int
    limit: int
    offset: int


__all__: list[str] = ["FileMetadata", "FileListResponse"]