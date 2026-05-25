"""POST /sessions Origin allowlist validation (AI-407 §10.7 + §5.1).

Task 2.27 — second link in the §10.3.1 validation chain. Validate the
incoming `Origin` header against Deployment.allowed_origins (loaded from
the OS API).

Per §5.1: empty allowed_origins = reject all (Deployment must explicitly
enumerate allowed origins to accept connections — no implicit allow).
Per §10.7 threat model: CORS / origin spoofing is the headline risk this
mitigates — a malicious site could try to open sessions to a Deployment
that isn't theirs; the Origin allowlist prevents.

Response shape per §10.3.1 table:
- 403 (origin_not_allowed) → {"error": "forbidden",
  "reason": "origin_not_allowed"}
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


def _make_deployment(allowed_origins, deployment_id="dep_test"):
    """Build a minimal mock Deployment dict — only the fields Task 2.27
    consumes are set. Subsequent tasks will fill in status, parameter_schema,
    associate_id, etc."""
    return {
        "_id": deployment_id,
        "name": "Test Deployment",
        "allowed_origins": allowed_origins,
        "status": "active",
    }


class TestOriginValidation:
    def test_unknown_origin_returns_403(self, client):
        """Origin not in Deployment.allowed_origins → 403."""
        deployment = _make_deployment(["https://sales.indemn.ai"])

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={"deployment_id": "dep_test", "dynamic_params": {}},
                headers={"Origin": "https://malicious.example.com"},
            )

        assert response.status_code == 403
        body = response.json()
        assert body.get("error") == "forbidden"
        assert body.get("reason") == "origin_not_allowed"

    def test_allowed_origin_proceeds_past_check(self, client):
        """Origin in allowlist → not 403 (continues to other checks).
        Downstream is still 501 (Task 2.28+ JWT not wired) — we just check
        that the Origin check itself passed."""
        deployment = _make_deployment(["https://sales.indemn.ai"])

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={"deployment_id": "dep_test", "dynamic_params": {}},
                headers={"Origin": "https://sales.indemn.ai"},
            )

        assert response.status_code != 403

    def test_empty_allowed_origins_rejects_all(self, client):
        """Track 13f — empty `allowed_origins = []` rejects every origin.
        Per design §5.1: 'Empty list `[]` = reject all (Deployment must
        explicitly enumerate allowed origins to accept connections).'
        Without this test, an implementation that treats empty list as
        'allow all' silently opens the surface to any origin."""
        deployment = _make_deployment([])  # explicit empty list

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={"deployment_id": "dep_test", "dynamic_params": {}},
                headers={"Origin": "https://sales.indemn.ai"},
            )

        assert response.status_code == 403
        assert response.json().get("reason") == "origin_not_allowed"

    def test_missing_origin_header_rejects(self, client):
        """A request without Origin header is rejected (can't be matched
        against the allowlist) — same 403 origin_not_allowed."""
        deployment = _make_deployment(["https://sales.indemn.ai"])

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={"deployment_id": "dep_test", "dynamic_params": {}},
                # no Origin header
            )

        assert response.status_code == 403
        assert response.json().get("reason") == "origin_not_allowed"

    def test_origin_check_case_sensitive(self, client):
        """Origin headers are case-sensitive per the spec — 'Https://X.com'
        does NOT match 'https://X.com'. Defensive: don't normalize."""
        deployment = _make_deployment(["https://sales.indemn.ai"])

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={"deployment_id": "dep_test", "dynamic_params": {}},
                headers={"Origin": "HTTPS://SALES.INDEMN.AI"},
            )

        assert response.status_code == 403

    def test_deployment_not_found_returns_404(self, client):
        """If the Deployment lookup raises a NotFound, we surface 404 with
        resource=deployment per §10.3.1. (Task 2.29 will formalize the
        404 path; this test pins the behavior already.)"""
        from harness.sessions import DeploymentNotFound

        async def _raise(deployment_id):
            raise DeploymentNotFound(deployment_id)

        with patch("harness.sessions._load_deployment", new=_raise):
            response = client.post(
                "/sessions",
                json={"deployment_id": "dep_missing", "dynamic_params": {}},
                headers={"Origin": "https://sales.indemn.ai"},
            )

        assert response.status_code == 404
        assert response.json().get("resource") == "deployment"
