"""Voice frontdoor /health endpoint (AI-407 §10.3).

Railway health-check — confirms the frontdoor process is up + serving
HTTP. Does NOT exercise downstream dependencies; depth health checks
happen via per-request paths.
"""

import pytest


@pytest.fixture
def client():
    from harness.app import app
    from starlette.testclient import TestClient
    return TestClient(app)


def test_health_returns_200(client):
    response = client.get("/health")
    assert response.status_code == 200


def test_health_returns_status_healthy(client):
    response = client.get("/health")
    body = response.json()
    assert body["status"] == "healthy"


def test_health_identifies_service(client):
    response = client.get("/health")
    body = response.json()
    assert body["service"] == "indemn-runtime-voice-frontdoor"


def test_health_responds_to_get_only(client):
    """POST /health should be 405 (Method Not Allowed) — the route is GET-only."""
    response = client.post("/health")
    assert response.status_code == 405
