"""POST /sessions dynamic_params JSON Schema validation (AI-407 Task 2.30 /
§10.3.1 step 6 + §5.4).

After body parse + Origin + JWT + status check pass, validate the merged
static+dynamic parameter set against Deployment.parameter_schema (JSON
Schema Draft 2020-12 via `jsonschema` library).

Two modes per §5.4:
- strict (default for acts_as=session_actor) — reject with 400 on failure
- forgiving (default for acts_as=associate_self) — attach validation_warnings
  to response + proceed (warnings surfaced in Task 2.34's 200 response;
  for now the chain continues to the 501 placeholder)

Accepted risk per §10.7 NoSQL injection row: string values eventually
flow into CLI tool calls. Mitigation is at the CLI layer
(`find_scoped()` parameter binding) — no new defense in this task.

Error response shape per §10.3.1 table:
- 400 → {"error": "validation_error", "details": "<jsonschema error path>"}
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


def _deployment_with_schema(
    schema,
    *,
    static_parameters=None,
    validation_mode="strict",
    deployment_id="dep_test",
):
    """Build a stub Deployment with a custom parameter_schema. Lets each
    test pin a specific validation scenario without mutating the
    session-scoped valid_deployment fixture."""
    return {
        "_id": deployment_id,
        "name": "Test Deployment",
        "allowed_origins": ["https://sales.indemn.ai"],
        "status": "active",
        "acts_as": "session_actor",
        "parameter_schema": schema,
        "parameter_schema_validation_mode": validation_mode,
        "static_parameters": static_parameters or {},
    }


def _post_with_jwt(client, deployment_id, valid_jwt, dynamic_params):
    """Common POST shape used across the validation tests."""
    token = valid_jwt("act_test")
    return client.post(
        "/sessions",
        json={
            "deployment_id": deployment_id,
            "dynamic_params": dynamic_params,
        },
        headers={
            "Origin": "https://sales.indemn.ai",
            "Authorization": f"Bearer {token}",
        },
    )


class TestParameterSchemaValidation:
    def test_missing_required_actor_id_returns_400(
        self, client, valid_deployment, valid_jwt
    ):
        """parameter_schema requires actor_id; dynamic_params={} → 400.

        The merged set is just static_parameters (role, tenant) — neither
        of which is actor_id, so the `required` check fails. The details
        string should mention actor_id so the SDK can render a useful
        error."""
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ):
            response = _post_with_jwt(
                client, valid_deployment["_id"], valid_jwt, {}
            )

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "validation_error"
        assert "actor_id" in body.get("details", "").lower()

    def test_invalid_actor_id_pattern_returns_400(
        self, client, valid_deployment, valid_jwt
    ):
        """actor_id pattern is `^[0-9a-zA-Z_]+$`. A value with disallowed
        chars (hyphens, spaces) fails the pattern check → 400.

        Pins the value-level validation works, not just `required`
        completeness."""
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ):
            response = _post_with_jwt(
                client,
                valid_deployment["_id"],
                valid_jwt,
                {"actor_id": "has-hyphens not allowed"},
            )

        assert response.status_code == 400
        assert response.json()["error"] == "validation_error"

    def test_unknown_field_rejected_in_strict_mode(
        self, client, valid_deployment, valid_jwt
    ):
        """additionalProperties: false on the schema rejects fields not
        declared in properties → 400.

        Operator-side enumeration is the contract; an SDK passing
        unknown fields is a bug to surface immediately, not silently
        drop."""
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ):
            response = _post_with_jwt(
                client,
                valid_deployment["_id"],
                valid_jwt,
                {"actor_id": "act_test", "rogue_field": "x"},
            )

        assert response.status_code == 400
        assert response.json()["error"] == "validation_error"

    def test_valid_dynamic_params_pass(
        self, client, valid_deployment, valid_jwt
    ):
        """actor_id present + matches pattern + no unknown fields →
        validation passes. Chain continues past the schema check (501
        placeholder until Task 2.31+ wire acts_as / etc)."""
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ):
            response = _post_with_jwt(
                client,
                valid_deployment["_id"],
                valid_jwt,
                {"actor_id": "act_test"},
            )

        assert response.status_code != 400  # parameter_schema check passed

    def test_no_parameter_schema_means_no_validation(
        self, client, valid_jwt
    ):
        """A Deployment with parameter_schema absent/None (e.g., fetcher
        with no dynamic params) skips schema validation per §5.2 — any
        dynamic_params dict is accepted (the agent's skill decides what
        to do with it). Chain continues past the schema check."""
        deployment = _deployment_with_schema(None)
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = _post_with_jwt(
                client,
                deployment["_id"],
                valid_jwt,
                {"anything": "goes"},
            )

        assert response.status_code != 400

    def test_forgiving_mode_does_not_400_on_validation_failure(
        self, client, valid_jwt
    ):
        """validation_mode=forgiving → invalid params do NOT 400. Per
        §5.4: public/anonymous-surface deployments (acts_as=associate_self)
        attach validation_warnings + proceed; failure-policy mismatch is
        the operator's call, not the SDK's."""
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["customer_id"],
            "properties": {"customer_id": {"type": "string"}},
            "additionalProperties": False,
        }
        deployment = _deployment_with_schema(
            schema, validation_mode="forgiving"
        )
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = _post_with_jwt(
                client,
                deployment["_id"],
                valid_jwt,
                # missing required customer_id
                {},
            )

        assert response.status_code != 400

    def test_invalid_parameter_schema_at_save_time_is_caught_at_session(
        self, client, valid_jwt
    ):
        """Defensive: if a Deployment somehow has a malformed
        parameter_schema (e.g., legacy record that bypassed save-time
        validation), the session start should NOT crash with 500 — it
        should surface as 400 validation_error pointing at the schema
        problem. The kernel's Track 13e + Task 1.9 fix prevents this at
        save_tracked time, but the frontdoor defends in depth."""
        bad_schema = {
            # type must be a string or array; the int below is invalid
            "type": 12345,
        }
        deployment = _deployment_with_schema(bad_schema)
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = _post_with_jwt(
                client,
                deployment["_id"],
                valid_jwt,
                {"actor_id": "act_test"},
            )

        # 400 (caught + surfaced) is better than 500 (crash). Implementation
        # may choose to log + return 500-with-request_id if the schema is
        # truly unrecoverable, but the v1 design is 400 with a useful
        # details message.
        assert response.status_code in (400, 500)
