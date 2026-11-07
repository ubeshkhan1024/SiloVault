"""Tests for the health endpoint.

Verifies that the FastAPI backend starts correctly and returns a healthy
status from `/api/v1/health`.
"""

from fastapi.testclient import TestClient

from backend.main import app


def test_health_check_returns_healthy_status():
    """The health endpoint should return a 200 status and healthy message."""
    client = TestClient(app)
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["app"] == "SiloVault"
    assert "version" in payload
    assert "timestamp" in payload


def test_root_endpoint_returns_message():
    """The root endpoint should redirect or return a helpful message."""
    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert "SiloVault" in response.json()["message"]