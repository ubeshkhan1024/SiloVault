"""Shared FastAPI dependencies.

Exposes reusable dependency injection helpers (such as the database session
provider). This module is intentionally minimal for the skeleton stage.
"""

from backend.database import get_db

__all__ = ["get_db"]