"""POST /sessions skeleton (AI-407 §10.3.1).

Task 2.25 — skeleton route returns 501 (Not Implemented) once parsing +
Origin + Deployment-load passes (Task 2.26 + 2.27). Subsequent tasks
(2.28+) progressively fill the rest of the validation chain: JWT →
parameter_schema → acts_as → rate-limit → Interaction → LiveKit dispatch →
200 response.

Post-Task-2.27: these tests mock _load_deployment + supply an allowed
Origin header so the chain reaches the skeleton's 501.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


def _stub_deployment():
    """Minimal Deployment dict permitting Origin check to pass."""
    return {
        "_id": "dep_test",
        "name": "Test",
        "allowed_origins": ["https://test.example.com"],
        "status": "active",
    }


def test_sessions_endpoint_registered(client):
    """POST /sessions returns SOMETHING (not 404). Validation chain may
    still 400/403/etc but the route IS registered."""
    response = client.post(
        "/sessions",
        json={"deployment_id": "test", "dynamic_params": {}},
    )
    assert response.status_code != 404


def test_sessions_get_not_allowed(client):
    """GET /sessions returns 405 — endpoint is POST-only."""
    response = client.get("/sessions")
    assert response.status_code == 405


def test_sessions_skeleton_returns_501(client):
    """Post-parsing + Origin check, the skeleton returns 501 (Not
    Implemented) — subsequent tasks fill the rest of the chain."""
    with patch(
        "harness.sessions._load_deployment",
        new=AsyncMock(return_value=_stub_deployment()),
    ):
        response = client.post(
            "/sessions",
            json={"deployment_id": "test", "dynamic_params": {}},
            headers={"Origin": "https://test.example.com"},
        )
    assert response.status_code == 501
