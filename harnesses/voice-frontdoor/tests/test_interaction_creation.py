"""POST /sessions Interaction creation server-side (AI-407 Task 2.32 /
§10.3.1 step 10 + §10.6).

After all validation passes, the frontdoor creates an Interaction record
via the OS API (POST /api/interactions/) using its own
INDEMN_SERVICE_TOKEN. The Interaction is:
- channel_type: "voice"
- deployment_id: from the validated Deployment
- correlation_id: fresh UUID4 (the lineage tracker per §13)
- created_by: effective_actor_id (JWT.sub for session_actor; associate_id
  for associate_self — per Task 2.31's gate)
- status: "active"
- dynamic_params: RAW (per §10.7 — sanitize applies only to the
  <deployment_context> SystemMessage path; the Interaction record is
  for audit + forensics)

interaction_id + correlation_id surface in the response so the SDK +
worker can both reference the same Interaction.

The 501 placeholder response now carries interaction_id +
correlation_id; Task 2.34 flips the status code to 200 with the full
success shape (room_name + livekit_url + livekit_token added by
Task 2.33's LiveKit dispatch).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


def _stub_deployment_session_actor(deployment_id="dep_test"):
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
                "actor_id": {"type": "string", "pattern": "^[0-9a-zA-Z_]+$"},
                "role": {"type": "string"},
                "tenant": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "parameter_schema_validation_mode": "strict",
        "static_parameters": {"role": "sales", "tenant": "indemn-internal"},
    }


def _interaction_response(interaction_id="int_test", correlation_id="cor_test"):
    """Shape of what POST /api/interactions/ returns after save_tracked."""
    return {
        "_id": interaction_id,
        "channel_type": "voice",
        "correlation_id": correlation_id,
        "deployment_id": "dep_test",
        "created_by": "act_alice",
        "status": "active",
    }


class TestInteractionCreation:
    def test_interaction_created_via_os_api(
        self, client, jwt_for_actor
    ):
        """After acts_as gate passes, _create_interaction is called with
        the merged context. Verifies the integration point exists +
        produces an Interaction record."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_interaction",
            new=AsyncMock(return_value=_interaction_response()),
        ) as mock_create:
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

        # 501 (placeholder) until Task 2.34 wires success — but the
        # Interaction itself MUST be created.
        assert mock_create.called
        # The call args carry the right (deployment, effective_actor_id,
        # dynamic_params) tuple
        call = mock_create.call_args
        all_args = list(call.args) + list(call.kwargs.values())
        # deployment passed (positional or as kwarg)
        assert deployment in all_args
        # effective_actor_id should be the JWT.sub (Task 2.31 invariant)
        assert "act_alice" in all_args

    def test_response_surfaces_interaction_id(
        self, client, jwt_for_actor
    ):
        """The 501 placeholder (Task 2.34 will flip to 200) MUST carry
        interaction_id so the SDK + worker can both reference the same
        Interaction record."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        interaction = _interaction_response(
            interaction_id="int_specific_xyz",
            correlation_id="cor_specific_abc",
        )

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_interaction",
            new=AsyncMock(return_value=interaction),
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

        body = response.json()
        assert body.get("interaction_id") == "int_specific_xyz"
        assert body.get("correlation_id") == "cor_specific_abc"

    def test_associate_self_interaction_created_by_is_associate_id(
        self, client, jwt_for_actor
    ):
        """For acts_as=associate_self, effective_actor_id is the
        Associate's id (Task 2.31); Interaction.created_by reflects
        that. The JWT.sub of the anonymous visitor is NOT recorded as
        the creator of the Interaction (per §10.6 — created_by is the
        actor whose actions are recorded, not the session initiator)."""
        token = jwt_for_actor("anon_visitor")
        deployment = {
            "_id": "dep_public",
            "name": "Public Deployment",
            "allowed_origins": ["https://sales.indemn.ai"],
            "status": "active",
            "acts_as": "associate_self",
            "associate_id": "act_public_assistant",
            "parameter_schema": None,
            "static_parameters": {},
        }
        captured_actor_id = []

        async def _capture_actor(deployment, effective_actor_id, dynamic_params):
            captured_actor_id.append(effective_actor_id)
            return _interaction_response()

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._create_interaction",
            new=_capture_actor,
        ):
            client.post(
                "/sessions",
                json={
                    "deployment_id": deployment["_id"],
                    "dynamic_params": {},
                },
                headers={
                    "Origin": "https://sales.indemn.ai",
                    "Authorization": f"Bearer {token}",
                },
            )

        # effective_actor_id was the associate_id, not the JWT.sub
        assert captured_actor_id == ["act_public_assistant"]


@pytest.fixture
def _undo_create_interaction_autouse(monkeypatch):
    """Opt out of the conftest autouse `_stub_create_interaction` so this
    test class can exercise the REAL _create_interaction helper directly.
    Importing the unpatched function back into the module restores the
    original behavior for this test's scope."""
    import importlib

    from harness import sessions

    real_create = importlib.reload(sessions)._create_interaction
    monkeypatch.setattr("harness.sessions._create_interaction", real_create)


class TestCreateInteractionHelper:
    """Unit-test the _create_interaction helper directly (without the
    Starlette route). Pins the HTTP shape sent to /api/interactions/."""

    @pytest.mark.asyncio
    async def test_posts_to_os_api_with_service_token(
        self, monkeypatch, _undo_create_interaction_autouse
    ):
        """_create_interaction must POST to {INDEMN_API_URL}/api/interactions/
        with the Authorization: Bearer {INDEMN_SERVICE_TOKEN} header and
        a payload matching the §10.3.1 contract."""
        from harness import sessions

        monkeypatch.setenv("INDEMN_API_URL", "http://test-api")
        monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "svc_token_abc")

        captured = {}

        class _StubAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, **kwargs):
                captured["url"] = url
                captured["kwargs"] = kwargs
                resp = MagicMock()
                resp.status_code = 201
                resp.json.return_value = _interaction_response()
                resp.raise_for_status = MagicMock()
                return resp

        monkeypatch.setattr("harness.sessions.httpx.AsyncClient", _StubAsyncClient)

        deployment = {
            "_id": "dep_test",
            "acts_as": "session_actor",
        }
        result = await sessions._create_interaction(
            deployment=deployment,
            effective_actor_id="act_alice",
            dynamic_params={"actor_id": "act_alice", "current_route": "/x"},
        )

        assert captured["url"].endswith("/api/interactions/")
        assert "test-api" in captured["url"]
        # Authorization header carries the service token
        headers = captured["kwargs"].get("headers", {})
        assert headers.get("Authorization") == "Bearer svc_token_abc"
        # Payload carries the contract fields
        payload = captured["kwargs"].get("json", {})
        assert payload["channel_type"] == "voice"
        assert payload["deployment_id"] == "dep_test"
        assert payload["created_by"] == "act_alice"
        assert payload["status"] == "active"
        assert payload["dynamic_params"] == {
            "actor_id": "act_alice",
            "current_route": "/x",
        }
        # correlation_id is a fresh UUID4 — should be a non-empty string
        assert isinstance(payload["correlation_id"], str)
        assert len(payload["correlation_id"]) > 0
        # Helper returns the API response
        assert result["_id"] == "int_test"
