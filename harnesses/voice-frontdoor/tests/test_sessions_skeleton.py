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
    from harness.app import app
    from starlette.testclient import TestClient
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


def test_sessions_happy_path_returns_200(client, valid_jwt):
    """Once Task 2.34 wired the full chain end-to-end, the skeleton's
    happy path returns 200 with the canonical §10.3.1 4-key shape.
    Renamed from `_returns_501` — the placeholder is gone; the success
    response is the contract."""
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
    assert response.status_code == 200
    # AI-408 Task 3.6 follow-up: validation_warnings field added per plan §3.6.
    # Empty list when no forgiving-mode warnings — kept as stable shape so
    # SDKs can iterate without null-checking.
    assert set(response.json().keys()) == {
        "room_name",
        "livekit_url",
        "livekit_token",
        "interaction_id",
        "validation_warnings",
    }
