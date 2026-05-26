"""POST /sessions skeleton (AI-407 §10.3.1).

Task 2.25 — skeleton route returns 501 (Not Implemented) once the
validation chain passes. As each subsequent task wires the next link,
the skeleton-reaching test must satisfy that link too. Today's chain:

  body parse (Task 2.26) → Deployment load + Origin (Task 2.27) →
  JWT (Task 2.28) → ... → 501 placeholder

Tasks 2.29-2.36 progressively fill: status check → parameter_schema →
acts_as → rate-limit → Interaction → LiveKit dispatch → 200 response.
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


def test_sessions_skeleton_returns_501(client, valid_jwt):
    """Once body parse + Deployment load + Origin + JWT (Task 2.28) all
    pass, the skeleton's downstream-not-implemented branch returns 501.

    Updated with Task 2.28: supplies a valid JWT so the chain reaches
    501 instead of stopping at 401. Subsequent tasks (2.29 status check,
    2.30 schema validation, etc.) will require this test to provide
    additional state on the deployment fixture."""
    token = valid_jwt("act_test")
    with patch(
        "harness.sessions._load_deployment",
        new=AsyncMock(return_value=_stub_deployment()),
    ):
        response = client.post(
            "/sessions",
            json={"deployment_id": "test", "dynamic_params": {}},
            headers={
                "Origin": "https://test.example.com",
                "Authorization": f"Bearer {token}",
            },
        )
    assert response.status_code == 501
