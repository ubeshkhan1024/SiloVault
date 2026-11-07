"""Tests for the uploads endpoints.

PLACEHOLDER — These tests verify that the uploads endpoints exist and return
placeholder responses. Real upload tests will be added in Phase 3.
"""

from fastapi.testclient import TestClient

from backend.main import app


def test_list_uploads_returns_placeholder():
    """The uploads list endpoint should return a placeholder response."""
    client = TestClient(app)
    response = client.get("/api/v1/uploads")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "placeholder"
    assert payload["uploads"] == []


def test_create_upload_returns_placeholder():
    """The upload creation endpoint should return a placeholder response."""
    client = TestClient(app)
    response = client.post("/api/v1/uploads")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "placeholder"