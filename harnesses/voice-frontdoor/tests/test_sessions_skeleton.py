"""POST /sessions skeleton (AI-407 §10.3.1).

Task 2.25 — skeleton route returns 501 (Not Implemented) for any valid
request shape. Subsequent tasks (2.26+) progressively fill the validation
chain: body parse → Origin → JWT → Deployment → params → acts_as →
rate-limit → Interaction → LiveKit dispatch → 200 response.

The skeleton's purpose: route exists, accepts POST, hands off to the
sessions handler module — so Tasks 2.26+ can fill the body without
restructuring the route table.
"""

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


def test_sessions_endpoint_registered(client):
    """POST /sessions returns SOMETHING (not 404). Auth/validation not
    yet wired in skeleton."""
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
    """Skeleton response is 501 (Not Implemented) — subsequent tasks
    replace with progressively-validated success/error responses."""
    response = client.post(
        "/sessions",
        json={"deployment_id": "test", "dynamic_params": {}},
    )
    assert response.status_code == 501
