"""POST /sessions deployment status check (AI-407 Task 2.29 / §10.3.1 step 5).

After body parse + Origin + JWT pass, verify Deployment.status == "active".
Paused / archived / error / configured deployments reject session creation
with 409. Inactive Deployments must not accept new sessions — this is the
operator-level kill-switch for incident response, A/B off-periods, etc.

Error response shape per §10.3.1 table:
- 409 → {"error": "deployment_not_active", "status": "<actual-status>"}
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


def _stub_deployment(status, deployment_id="dep_test"):
    return {
        "_id": deployment_id,
        "name": "Test Deployment",
        "allowed_origins": ["https://sales.indemn.ai"],
        "status": status,
    }


class TestDeploymentStatus:
    def test_paused_deployment_returns_409(
        self, client, paused_deployment, valid_jwt
    ):
        """Deployment.status=paused → 409 with status surfaced in body so
        the SDK can render a useful error to the user ('this assistant
        is temporarily paused' vs the generic 'not found')."""
        token = valid_jwt("act_test")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=paused_deployment),
        ):
            response = client.post(
                "/sessions",
                json={
                    "deployment_id": paused_deployment["_id"],
                    "dynamic_params": {"actor_id": "act_abc"},
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "deployment_not_active"
        assert body["status"] == "paused"

    @pytest.mark.parametrize(
        "status", ["configured", "paused", "archived", "error"]
    )
    def test_all_non_active_statuses_rejected(
        self, client, valid_jwt, status
    ):
        """Per §5.7 state machine, every status except `active` must
        reject session creation. Parametrized to pin all four explicitly
        — a future state addition that defaults to "active" semantics
        could silently allow sessions."""
        token = valid_jwt("act_test")
        deployment = _stub_deployment(status)
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={
                    "deployment_id": deployment["_id"],
                    "dynamic_params": {"actor_id": "act_abc"},
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "deployment_not_active"
        assert body["status"] == status

    def test_active_deployment_proceeds_past_status_check(
        self, client, valid_jwt
    ):
        """Deployment.status=active → not 409; chain continues to
        Tasks 2.30+ (currently 501 since acts_as / params not wired)."""
        token = valid_jwt("act_test")
        deployment = _stub_deployment("active")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={
                    "deployment_id": deployment["_id"],
                    "dynamic_params": {"actor_id": "act_abc"},
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        assert response.status_code != 409
