"""POST /sessions acts_as security gate (AI-407 Task 2.31 / §5.6 + §10.7).

**This is the load-bearing security check of the entire session_actor
capability.** Per §10.7 row "JWT impersonation via dynamic_params.actor_id":

  > For `acts_as = session_actor`: the runtime extracts `actor_id` from
  > the VALIDATED JWT, not from `dynamic_params`. If
  > `dynamic_params.actor_id` is present, it MUST equal the JWT's actor —
  > mismatch = reject. This is the load-bearing security gate of the
  > entire `session_actor` capability.

The implementation semantic that makes it load-bearing:
- `effective_actor_id = authenticated_actor_id` (JWT.sub) — UNCONDITIONALLY
  for session_actor mode, not derived from dynamic_params.
- `dynamic_params.actor_id` is consulted ONLY for the mismatch check.

A reviewer must be able to read ONE LINE to verify the gate is correct
("effective_actor_id = the JWT's sub"), not trace a fallback chain.

Error response shape per §10.3.1 table:
- 403 → {"error": "forbidden", "reason": "actor_mismatch"}
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def client():
    from harness.app import app
    from starlette.testclient import TestClient
    return TestClient(app)


def _stub_deployment_session_actor(deployment_id="dep_test"):
    """Deployment with acts_as=session_actor + parameter_schema requiring
    actor_id (the normal shape for an internal-team Deployment).
    additionalProperties=False with role/tenant declared so the merged
    set validates."""
    return {
        "_id": deployment_id,
        "name": "Test Deployment",
        "allowed_origins": ["https://sales.indemn.ai"],
        "status": "active",
        "acts_as": "session_actor",
        "associate_id": "act_associate",
        "parameter_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["actor_id"],
            "properties": {
                "actor_id": {
                    "type": "string",
                    "pattern": "^[0-9a-zA-Z_]+$",
                },
                "role": {"type": "string"},
                "tenant": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "parameter_schema_validation_mode": "strict",
        "static_parameters": {"role": "sales", "tenant": "indemn-internal"},
    }


def _stub_deployment_associate_self(deployment_id="dep_pub"):
    """Public-surface Deployment with acts_as=associate_self — no actor_id
    requirement; effective_actor_id is the associate's own id."""
    return {
        "_id": deployment_id,
        "name": "Public Test Deployment",
        "allowed_origins": ["https://sales.indemn.ai"],
        "status": "active",
        "acts_as": "associate_self",
        "associate_id": "act_public_assistant",
        "parameter_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "additionalProperties": False,
        },
        "parameter_schema_validation_mode": "forgiving",
        "static_parameters": {},
    }


class TestActsAsGate:
    def test_actor_mismatch_returns_403(self, client, jwt_for_actor):
        """JWT.sub=act_alice + dynamic_params.actor_id=act_bob → 403
        actor_mismatch. The load-bearing security check: a supplied
        actor_id that disagrees with the authenticated JWT is rejected,
        not silently overridden."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={
                    "deployment_id": deployment["_id"],
                    "dynamic_params": {"actor_id": "act_bob"},
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        assert response.status_code == 403
        body = response.json()
        assert body["error"] == "forbidden"
        assert body["reason"] == "actor_mismatch"

    def test_actor_matches_proceeds(self, client, jwt_for_actor):
        """JWT.sub=act_alice + dynamic_params.actor_id=act_alice →
        passes the gate (chain continues; 501 placeholder until 2.32+
        wire Interaction / LiveKit)."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={
                    "deployment_id": deployment["_id"],
                    "dynamic_params": {"actor_id": "act_alice"},
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        assert response.status_code != 403

    def test_effective_actor_id_is_jwt_sub_in_session_actor_mode(
        self, client, jwt_for_actor
    ):
        """JWT IS source of truth. When dynamic_params.actor_id matches
        JWT.sub, the effective_actor_id passed to _create_interaction
        must equal JWT.sub — never derived from dynamic_params, even
        when they're identical. Pins the semantic that code review can
        verify in one line: `effective_actor_id = authenticated_actor_id`.

        Verified via the _create_interaction call args (Task 2.34's
        200 success shape doesn't surface effective_actor_id; the
        Interaction record's created_by carries it server-side)."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        captured = []

        async def _capture(deployment, effective_actor_id, dynamic_params):
            captured.append(effective_actor_id)
            return {
                "_id": "int_test",
                "correlation_id": "cor_test",
                "channel_type": "voice",
            }

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_interaction",
            new=_capture,
        ):
            response = client.post(
                "/sessions",
                json={
                    "deployment_id": deployment["_id"],
                    "dynamic_params": {"actor_id": "act_alice"},
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        assert response.status_code != 403
        # effective_actor_id was JWT.sub, not the supplied value
        assert captured == ["act_alice"]

    def test_associate_self_effective_actor_is_associate_id(
        self, client, jwt_for_actor
    ):
        """For public-surface Deployments (acts_as=associate_self), the
        agent acts AS itself with its own role's permissions. JWT only
        proves the caller is authenticated; actor_id check is skipped
        because there's no per-user identity model on a public surface.
        effective_actor_id = Deployment.associate_id."""
        token = jwt_for_actor("anon_visitor_xyz")
        deployment = _stub_deployment_associate_self()
        captured = []

        async def _capture(deployment, effective_actor_id, dynamic_params):
            captured.append(effective_actor_id)
            return {
                "_id": "int_test",
                "correlation_id": "cor_test",
                "channel_type": "voice",
            }

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_interaction",
            new=_capture,
        ):
            response = client.post(
                "/sessions",
                json={
                    "deployment_id": deployment["_id"],
                    # actor_id MUST NOT come from the user on public
                    # surfaces; the field isn't in the schema. Sending
                    # nothing.
                    "dynamic_params": {},
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        assert response.status_code != 403
        assert captured == ["act_public_assistant"]

    def test_associate_self_ignores_supplied_actor_id(
        self, client, jwt_for_actor
    ):
        """In associate_self mode, even if a user maliciously supplies
        actor_id in dynamic_params, the runtime uses the Associate's
        own id — never the supplied value, never the JWT.sub. The
        parameter_schema for a public Deployment shouldn't even
        declare actor_id in properties, so this would normally be
        caught at the JSON Schema step. We pin the gate logic itself
        by using a forgiving schema that wouldn't reject the extra
        field."""
        token = jwt_for_actor("anon_visitor_xyz")
        deployment = _stub_deployment_associate_self()
        # widen the schema to allow extra fields for this test (mimics a
        # legacy/forgiving config)
        deployment = {
            **deployment,
            "parameter_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "actor_id": {"type": "string"},
                },
            },
        }
        captured = []

        async def _capture(deployment, effective_actor_id, dynamic_params):
            captured.append(effective_actor_id)
            return {
                "_id": "int_test",
                "correlation_id": "cor_test",
                "channel_type": "voice",
            }

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_interaction",
            new=_capture,
        ):
            response = client.post(
                "/sessions",
                json={
                    "deployment_id": deployment["_id"],
                    "dynamic_params": {"actor_id": "act_attacker_admin"},
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        assert response.status_code != 403
        # NOT the supplied value, NOT the JWT.sub — the associate's id.
        assert captured == ["act_public_assistant"]
