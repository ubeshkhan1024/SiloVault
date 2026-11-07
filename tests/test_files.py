"""Tests for the files endpoints.

PLACEHOLDER — These tests verify that the files endpoints exist and return
placeholder responses. Real file indexing tests will be added in Phase 1.
"""

from fastapi.testclient import TestClient

from backend.main import app


def test_list_files_returns_placeholder():
    """The files list endpoint should return a placeholder response."""
    client = TestClient(app)
    response = client.get("/api/v1/files")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "placeholder"
    assert payload["files"] == []


def test_get_file_returns_placeholder():
    """The file lookup endpoint should return a placeholder response."""
    client = TestClient(app)
    response = client.get("/api/v1/files/test-file-id")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "placeholder"
    assert payload["file_id"] == "test-file-id"