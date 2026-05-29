"""POST /sessions success response shape (AI-407 Task 2.34 / §10.3.1).

When body parse + Deployment load + Origin + JWT + status + schema +
acts_as + Interaction + LiveKit all pass, flip the 501 placeholder to
200 with the canonical §10.3.1 contract:

  {
    "room_name": "dep-...-int-...",
    "livekit_url": "wss://...",
    "livekit_token": "<participant JWT>",
    "interaction_id": "int_..."
  }

Exactly four keys — no leaked internals (authenticated_actor_id,
effective_actor_id, correlation_id, validation_warnings). The SDK only
needs what the LiveKit JS client + the resume-flow need; everything
else is server-side bookkeeping.

The Interaction + LiveKit dispatch are wrapped in try/except per
§10.3.1 status-500 contract: {"error": "internal", "request_id": "<id>",
"stage": "interaction|livekit"}. request_id is logged server-side with
the full traceback for grep-based debug. On LiveKit failure, best-effort
cleanup marks the orphaned Interaction as failed so we don't leak
abandoned `active` Interactions whose room never spawned.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def client():
    from harness.app import app
    from starlette.testclient import TestClient
    return TestClient(app)


def _stub_deployment_session_actor(deployment_id="dep_test"):
    return {
        "_id": deployment_id,
        "name": "Test",
        "allowed_origins": ["https://sales.indemn.ai"],
        "status": "active",
        "acts_as": "session_actor",
        "associate_id": "act_associate",
        "parameter_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["actor_id"],
            "properties": {
                "actor_id": {"type": "string", "pattern": "^[0-9a-zA-Z_]+$"},
                "role": {"type": "string"},
                "tenant": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "parameter_schema_validation_mode": "strict",
        "static_parameters": {"role": "sales", "tenant": "indemn-internal"},
    }


def _post_happy_path(client, deployment_id, token):
    return client.post(
        "/sessions",
        json={
            "deployment_id": deployment_id,
            "dynamic_params": {"actor_id": "act_alice"},
        },
        headers={
            "Origin": "https://sales.indemn.ai",
            "Authorization": f"Bearer {token}",
        },
    )


class TestSuccessResponse:
    def test_status_is_200(self, client, jwt_for_actor):
        """Happy path returns 200, not 501 or any other status."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = _post_happy_path(client, deployment["_id"], token)

        assert response.status_code == 200

    def test_response_shape_carries_five_keys(self, client, jwt_for_actor):
        """The success response carries the §10.3.1 4 keys + validation_warnings
        (added AI-408 Task 3.6 follow-up per plan §3.6). authenticated_actor_id,
        effective_actor_id, correlation_id stay server-side. validation_warnings
        defaults to [] for strict-mode-passing requests; populated by forgiving
        mode for SDK debugging."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = _post_happy_path(client, deployment["_id"], token)

        body = response.json()
        assert set(body.keys()) == {
            "room_name",
            "livekit_url",
            "livekit_token",
            "interaction_id",
            "validation_warnings",
        }
        # Strict-mode pass → empty list (no warnings to report)
        assert body["validation_warnings"] == []

    def test_forgiving_mode_surfaces_warnings_to_sdk(self, client, jwt_for_actor):
        """AI-408 Task 3.6 follow-up: forgiving-mode validation warnings
        surface to the SDK in the success response per plan §3.6. SDK devs
        get actionable feedback ('your actor_id pattern is wrong') instead
        of silent acceptance that breaks later when the operator flips to
        strict."""
        token = jwt_for_actor("act_alice")
        deployment = {
            **_stub_deployment_session_actor(),
            "parameter_schema_validation_mode": "forgiving",
        }
        # acts_as=session_actor + matching JWT — get past the acts_as gate
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = client.post(
                "/sessions",
                json={
                    "deployment_id": deployment["_id"],
                    "dynamic_params": {
                        "actor_id": "act_alice",  # matches JWT.sub
                        "extra_field": "would_fail_strict",  # additionalProperties:false
                    },
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        # Forgiving mode → 200 (not 400), warnings surface
        assert response.status_code == 200
        body = response.json()
        assert "validation_warnings" in body
        assert len(body["validation_warnings"]) >= 1
        # The warning text names the offending field
        assert any("extra_field" in w for w in body["validation_warnings"])

    def test_response_carries_interaction_id_from_interaction_creation(
        self, client, jwt_for_actor
    ):
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_interaction",
            new=AsyncMock(
                return_value={
                    "_id": "int_specific_abc",
                    "correlation_id": "cor_specific",
                    "channel_type": "voice",
                }
            ),
        ):
            response = _post_happy_path(client, deployment["_id"], token)

        body = response.json()
        assert body["interaction_id"] == "int_specific_abc"


class TestInteractionFailure500:
    def test_interaction_creation_failure_returns_500_with_request_id(
        self, client, jwt_for_actor
    ):
        """If _create_interaction raises (e.g., OS API 500, DB
        unreachable), the frontdoor surfaces 500 with a request_id the
        operator can grep server logs for. The traceback is logged
        server-side — the client gets the id."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_interaction",
            new=AsyncMock(side_effect=RuntimeError("OS API 500")),
        ):
            response = _post_happy_path(client, deployment["_id"], token)

        assert response.status_code == 500
        body = response.json()
        assert body["error"] == "internal"
        assert body["request_id"]  # non-empty
        assert body["stage"] == "interaction"


class TestLiveKitFailure500:
    def test_livekit_dispatch_failure_returns_500_with_stage_livekit(
        self, client, jwt_for_actor
    ):
        """If LiveKit dispatch fails (network, AgentDispatch service
        error), the frontdoor surfaces 500 with stage=livekit so the
        operator can quickly classify the failure mode without grepping
        the traceback."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_lk_room_and_dispatch",
            new=AsyncMock(side_effect=RuntimeError("LiveKit timeout")),
        ):
            response = _post_happy_path(client, deployment["_id"], token)

        assert response.status_code == 500
        body = response.json()
        assert body["error"] == "internal"
        assert body["request_id"]
        assert body["stage"] == "livekit"

    def test_livekit_failure_marks_orphaned_interaction_failed(
        self, client, jwt_for_actor
    ):
        """Per §10.3.1: on LiveKit failure after Interaction creation
        succeeded, best-effort mark the Interaction as failed so we
        don't leak abandoned `active` Interactions whose room never
        spawned. The cleanup is best-effort — its own failure is logged
        but doesn't change the 500 response shape."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        # Track the cleanup call
        cleanup_calls = []

        async def _capture_cleanup(interaction_id, reason):
            cleanup_calls.append((interaction_id, reason))

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_lk_room_and_dispatch",
            new=AsyncMock(side_effect=RuntimeError("LiveKit timeout")),
        ), patch(
            "harness.sessions._mark_interaction_failed",
            new=_capture_cleanup,
        ):
            response = _post_happy_path(client, deployment["_id"], token)

        assert response.status_code == 500
        # cleanup fired with the Interaction's id
        assert len(cleanup_calls) == 1
        interaction_id, reason = cleanup_calls[0]
        assert interaction_id == "int_autouse"  # the autouse fixture's id
        assert "LiveKit" in reason or "timeout" in reason
