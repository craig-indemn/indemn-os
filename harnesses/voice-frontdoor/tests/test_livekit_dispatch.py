"""POST /sessions LiveKit room creation + AgentDispatch + token mint
(AI-407 Task 2.33 / §10.3.1 step 10 + §10.6).

After all validation + Interaction creation pass, the frontdoor:
1. Creates a LiveKit room with deterministic name
   `dep-{deployment_id}-int-{interaction_id}`
2. Sets `room.metadata` to JSON of {deployment_id, interaction_id,
   dynamic_params, correlation_id} — **NO credentials** per §10.6
3. AgentDispatch to `voice-deepagents` worker pool
4. Mints a participant token with room_join + can_publish +
   can_subscribe grants

The room.metadata is the load-bearing handoff to the worker — the worker
reads `deployment_id` from there + loads the Deployment via OS API on
session start.

SDK shape gotchas (verified against installed livekit-api 1.x):
- `CreateRoomRequest` is in `livekit.protocol.room` (NOT `livekit.api`
  despite re-exports)
- `CreateAgentDispatchRequest` is in `livekit.protocol.agent_dispatch`
- Service methods: `room.create_room`, `agent_dispatch.create_dispatch`
  (NOT `create_agent_dispatch`)
- `CreateRoomRequest` has NO `max_duration` field — design's 4h cap
  enforced operationally (NOT at SDK level in this version)
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def client():
    from harness.app import app
    from starlette.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def _undo_create_lk_autouse(monkeypatch):
    """Opt out of the conftest autouse so a test can exercise the real
    `_create_lk_room_and_dispatch` helper directly (with LiveKitAPI
    mocked at the SDK level)."""
    import importlib

    from harness import sessions

    real = importlib.reload(sessions)._create_lk_room_and_dispatch
    monkeypatch.setattr(
        "harness.sessions._create_lk_room_and_dispatch", real
    )


@pytest.fixture
def _livekit_env(monkeypatch):
    """Set the LIVEKIT_* env vars the real helper requires. Values are
    obviously fake; tests should be patching LiveKitAPI to never make a
    real call."""
    monkeypatch.setenv("LIVEKIT_URL", "wss://livekit-test.example.com")
    monkeypatch.setenv("LIVEKIT_API_KEY", "lk_key_test")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "lk_secret_test")


def _stub_deployment_session_actor(deployment_id="dep_xyz"):
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


def _post_sessions(client, deployment_id, token):
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


class TestLiveKitDispatchSurface:
    """Black-box tests at the /sessions HTTP boundary — verify the
    response carries the LiveKit payload (room_name + livekit_url +
    livekit_token). Uses the conftest autouse stub for the helper."""

    def test_response_includes_room_name(self, client, jwt_for_actor):
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = _post_sessions(client, deployment["_id"], token)

        body = response.json()
        assert "room_name" in body
        assert body["room_name"]  # non-empty

    def test_response_includes_livekit_url(self, client, jwt_for_actor):
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = _post_sessions(client, deployment["_id"], token)

        body = response.json()
        assert "livekit_url" in body
        assert body["livekit_url"].startswith("wss://")

    def test_response_includes_livekit_token(self, client, jwt_for_actor):
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment_session_actor()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ):
            response = _post_sessions(client, deployment["_id"], token)

        body = response.json()
        assert "livekit_token" in body
        assert body["livekit_token"]  # non-empty


class TestLiveKitHelperContract:
    """Unit tests against the real `_create_lk_room_and_dispatch` helper
    with LiveKitAPI mocked at the SDK level. Pins the actual SDK calls
    + room.metadata shape."""

    @pytest.mark.asyncio
    async def test_room_metadata_carries_handoff_fields_no_credentials(
        self, _livekit_env, _undo_create_lk_autouse, monkeypatch
    ):
        """The load-bearing handoff to the worker. Verifies
        room.metadata JSON has {deployment_id, interaction_id,
        dynamic_params, correlation_id} AND NO auth tokens / service
        secrets (room metadata is visible to every participant per
        LiveKit protocol)."""
        captured = {"create_room_calls": []}

        class _FakeLiveKitAPI:
            def __init__(self, *args, **kwargs):
                self.room = MagicMock()
                self.room.create_room = AsyncMock(
                    side_effect=lambda req: captured["create_room_calls"].append(req)
                )
                self.agent_dispatch = MagicMock()
                self.agent_dispatch.create_dispatch = AsyncMock()

            async def aclose(self):
                pass

        monkeypatch.setattr("livekit.api.LiveKitAPI", _FakeLiveKitAPI)

        from harness.sessions import _create_lk_room_and_dispatch

        result = await _create_lk_room_and_dispatch(
            deployment_id="dep_xyz",
            interaction_id="int_abc",
            dynamic_params={"actor_id": "act_alice", "current_route": "/x"},
            correlation_id="cor_lineage",
        )

        # Room name format pinned (§10.3.1)
        assert result["room_name"] == "dep-dep_xyz-int-int_abc"

        # CreateRoomRequest metadata is JSON of the handoff fields
        assert len(captured["create_room_calls"]) == 1
        create_req = captured["create_room_calls"][0]
        meta = json.loads(create_req.metadata)
        assert meta["deployment_id"] == "dep_xyz"
        assert meta["interaction_id"] == "int_abc"
        assert meta["dynamic_params"] == {
            "actor_id": "act_alice",
            "current_route": "/x",
        }
        assert meta["correlation_id"] == "cor_lineage"

        # SECURITY: NO credentials in metadata. Visible to all
        # participants per LiveKit protocol (§10.6).
        meta_str = json.dumps(meta)
        assert "Bearer" not in meta_str
        assert "INDEMN_SERVICE_TOKEN" not in meta_str
        assert "auth_token" not in meta
        assert "api_secret" not in meta_str
        assert "lk_secret_test" not in meta_str

    @pytest.mark.asyncio
    async def test_agent_dispatch_targets_voice_deepagents_worker(
        self, _livekit_env, _undo_create_lk_autouse, monkeypatch
    ):
        """The AgentDispatch must target the `voice-deepagents` worker
        pool name (the agent_name registered by voice-deepagents/main.py
        per Task 2.16). Wrong agent_name = no worker picks up the room
        = silent timeout."""
        captured = {"dispatch_calls": []}

        class _FakeLiveKitAPI:
            def __init__(self, *args, **kwargs):
                self.room = MagicMock()
                self.room.create_room = AsyncMock()
                self.agent_dispatch = MagicMock()
                self.agent_dispatch.create_dispatch = AsyncMock(
                    side_effect=lambda req: captured["dispatch_calls"].append(req)
                )

            async def aclose(self):
                pass

        monkeypatch.setattr("livekit.api.LiveKitAPI", _FakeLiveKitAPI)

        from harness.sessions import _create_lk_room_and_dispatch

        await _create_lk_room_and_dispatch(
            deployment_id="dep_xyz",
            interaction_id="int_abc",
            dynamic_params={},
            correlation_id="cor_test",
        )

        assert len(captured["dispatch_calls"]) == 1
        dispatch_req = captured["dispatch_calls"][0]
        assert dispatch_req.agent_name == "voice-deepagents"
        assert dispatch_req.room == "dep-dep_xyz-int-int_abc"

    @pytest.mark.asyncio
    async def test_participant_token_minted_with_room_join_grant(
        self, _livekit_env, _undo_create_lk_autouse, monkeypatch
    ):
        """The participant token must include `room_join` so the user's
        LiveKit JS SDK can actually join the room. Missing grant =
        join attempt fails with 401 from LiveKit."""

        class _FakeLiveKitAPI:
            def __init__(self, *args, **kwargs):
                self.room = MagicMock()
                self.room.create_room = AsyncMock()
                self.agent_dispatch = MagicMock()
                self.agent_dispatch.create_dispatch = AsyncMock()

            async def aclose(self):
                pass

        monkeypatch.setattr("livekit.api.LiveKitAPI", _FakeLiveKitAPI)

        from harness.sessions import _create_lk_room_and_dispatch

        result = await _create_lk_room_and_dispatch(
            deployment_id="dep_xyz",
            interaction_id="int_abc",
            dynamic_params={},
            correlation_id="cor_test",
        )

        # Decode the JWT and inspect grants (LiveKit AccessToken =
        # standard JWT signed with API secret)
        import jwt as pyjwt

        claims = pyjwt.decode(
            result["livekit_token"],
            os.environ["LIVEKIT_API_SECRET"],
            algorithms=["HS256"],
            options={"verify_signature": True, "verify_aud": False},
        )
        # video grants live under the `video` key on LiveKit JWTs
        video = claims.get("video", {})
        assert video.get("roomJoin") is True
        # Camera + mic grants for an interactive voice session
        assert video.get("canPublish") is True
        assert video.get("canSubscribe") is True
        # Token is scoped to THIS room (defense against token reuse on
        # a different room)
        assert video.get("room") == "dep-dep_xyz-int-int_abc"
