"""POST /sessions resume_interaction_id flow (AI-407 Task 2.35 /
§10.3.1 step 8 + §12.4 step 12).

Resume reconnects to an existing Interaction (network blip, browser
tab close + reopen, etc.) — same `interaction_id`, fresh LiveKit room,
new participant token. Per §12.4 + §10.7 resumption-hijacking row:

- Authenticated JWT.sub MUST equal Interaction.created_by (else 403
  actor_mismatch — same gate as fresh session, applied to the prior
  Interaction's owner)
- Interaction age MUST be within Deployment.resumption_config.ttl_seconds
  (else 410 resume_expired)
- Interaction.status MUST NOT be closed / archived (else 410)
- If `kill_on_resume=true` (default per §12.4), the prior LiveKit
  room's agent participant is disconnected before the new worker is
  dispatched — prevents two-workers-on-same-Interaction state races
- Same `interaction_id` is returned; the worker reads it from
  room.metadata + the checkpointer (keyed by interaction_id per §13)
  carries the conversation history forward

Error response shapes per §10.3.1:
- 404 → {"error": "not_found", "resource": "interaction"}
- 403 → {"error": "forbidden", "reason": "actor_mismatch"}
- 410 → {"error": "resume_expired"} (optionally "reason": "closed")
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def client():
    from harness.app import app
    from starlette.testclient import TestClient
    return TestClient(app)


def _post_resume(client, deployment_id, interaction_id, token, *, actor_id="act_test"):
    return client.post(
        "/sessions",
        json={
            "deployment_id": deployment_id,
            "dynamic_params": {"actor_id": actor_id},
            "resume_interaction_id": interaction_id,
        },
        headers={
            "Origin": "https://sales.indemn.ai",
            "Authorization": f"Bearer {token}",
        },
    )


class TestResumeHappyPath:
    def test_resume_with_matching_actor_returns_200(
        self, client, valid_deployment, existing_interaction, jwt_for_actor
    ):
        """JWT.sub == Interaction.created_by ('act_test' on both fixtures)
        → resume succeeds, same interaction_id returned."""
        token = jwt_for_actor("act_test")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ), patch(
            "harness.sessions._load_interaction",
            new=AsyncMock(return_value=existing_interaction),
        ):
            response = _post_resume(
                client,
                valid_deployment["_id"],
                existing_interaction["_id"],
                token,
            )

        assert response.status_code == 200
        body = response.json()
        # Same Interaction (no new one created on resume)
        assert body["interaction_id"] == existing_interaction["_id"]

    def test_resume_does_not_call_create_interaction(
        self, client, valid_deployment, existing_interaction, jwt_for_actor
    ):
        """Per §12.4: resume reuses the existing Interaction; NO new one
        gets created. Pins that the cron / queue / analytics don't see
        a duplicate Interaction for the same logical session."""
        token = jwt_for_actor("act_test")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ), patch(
            "harness.sessions._load_interaction",
            new=AsyncMock(return_value=existing_interaction),
        ), patch(
            "harness.sessions._create_interaction",
            new=AsyncMock(),
        ) as mock_create:
            _post_resume(
                client,
                valid_deployment["_id"],
                existing_interaction["_id"],
                token,
            )

        assert not mock_create.called


class TestResumeRejection:
    def test_resume_nonexistent_interaction_returns_404(
        self, client, valid_deployment, jwt_for_actor
    ):
        """If the Interaction isn't found, 404 with resource=interaction."""
        token = jwt_for_actor("act_test")

        async def _raise(_id):
            from harness.sessions import InteractionNotFound

            raise InteractionNotFound(_id)

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ), patch(
            "harness.sessions._load_interaction",
            new=_raise,
        ):
            response = _post_resume(
                client,
                valid_deployment["_id"],
                "int_missing",
                token,
            )

        assert response.status_code == 404
        assert response.json()["resource"] == "interaction"

    def test_resume_with_wrong_actor_returns_403(
        self, client, valid_deployment, existing_interaction, jwt_for_actor
    ):
        """Resumption hijacking prevention per §10.7: JWT.sub must
        match Interaction.created_by. existing_interaction.created_by =
        'act_test'; we use 'act_eve' as a different actor. The JWT IS
        source of truth — supplied actor_id in dynamic_params is
        irrelevant (acts_as gate already checked at step 7)."""
        # existing_interaction.created_by = "act_test"
        # JWT.sub = "act_eve" (different actor)
        token = jwt_for_actor("act_eve")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ), patch(
            "harness.sessions._load_interaction",
            new=AsyncMock(return_value=existing_interaction),
        ):
            response = _post_resume(
                client,
                valid_deployment["_id"],
                existing_interaction["_id"],
                token,
                actor_id="act_eve",
            )

        assert response.status_code == 403
        assert response.json()["reason"] == "actor_mismatch"

    def test_resume_past_ttl_returns_410(
        self, client, valid_deployment, expired_interaction, jwt_for_actor
    ):
        """Interaction.created_at ~48h ago, default ttl_seconds=86400
        (24h) → past TTL → 410 resume_expired."""
        token = jwt_for_actor("act_test")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ), patch(
            "harness.sessions._load_interaction",
            new=AsyncMock(return_value=expired_interaction),
        ):
            response = _post_resume(
                client,
                valid_deployment["_id"],
                expired_interaction["_id"],
                token,
            )

        assert response.status_code == 410
        assert response.json()["error"] == "resume_expired"

    def test_resume_closed_interaction_returns_410(
        self, client, valid_deployment, existing_interaction, jwt_for_actor
    ):
        """A closed (terminal) Interaction is not resumable — 410
        with reason=closed so the SDK can distinguish from TTL
        expiry and prompt the user to start fresh instead of
        retrying."""
        closed_interaction = {**existing_interaction, "status": "closed"}
        token = jwt_for_actor("act_test")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ), patch(
            "harness.sessions._load_interaction",
            new=AsyncMock(return_value=closed_interaction),
        ):
            response = _post_resume(
                client,
                valid_deployment["_id"],
                closed_interaction["_id"],
                token,
            )

        assert response.status_code == 410
        body = response.json()
        assert body["error"] == "resume_expired"
        assert body.get("reason") == "closed"


class TestKillPriorRoom:
    def test_kill_on_resume_true_fires_kill_helper(
        self, client, valid_deployment, existing_interaction, jwt_for_actor
    ):
        """Default kill_on_resume=true → _kill_prior_room is called
        with the existing Interaction before the new dispatch.

        Without this, two voice-deepagents workers could be alive on
        the same Interaction post-resume — checkpointer-write race,
        double LLM costs, user hears two voices.
        """
        token = jwt_for_actor("act_test")
        kill_calls = []

        async def _capture_kill(interaction):
            kill_calls.append(interaction)

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ), patch(
            "harness.sessions._load_interaction",
            new=AsyncMock(return_value=existing_interaction),
        ), patch(
            "harness.sessions._kill_prior_room",
            new=_capture_kill,
        ):
            _post_resume(
                client,
                valid_deployment["_id"],
                existing_interaction["_id"],
                token,
            )

        assert len(kill_calls) == 1
        assert kill_calls[0] is existing_interaction

    def test_kill_on_resume_false_skips_kill_helper(
        self,
        client,
        valid_deployment,
        existing_interaction,
        jwt_for_actor,
    ):
        """resumption_config.kill_on_resume=false → _kill_prior_room
        NOT called. Operator's choice to accept race risk in exchange
        for non-disruptive resume (rare; not v1 default)."""
        token = jwt_for_actor("act_test")
        # Override the resumption_config on the deployment
        deployment = {
            **valid_deployment,
            "resumption_config": {"ttl_seconds": 86400, "kill_on_resume": False},
        }
        kill_calls = []

        async def _capture_kill(interaction):
            kill_calls.append(interaction)

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._load_interaction",
            new=AsyncMock(return_value=existing_interaction),
        ), patch(
            "harness.sessions._kill_prior_room",
            new=_capture_kill,
        ):
            _post_resume(
                client,
                deployment["_id"],
                existing_interaction["_id"],
                token,
            )

        assert len(kill_calls) == 0

    def test_kill_helper_failure_does_not_block_resume(
        self, client, valid_deployment, existing_interaction, jwt_for_actor
    ):
        """_kill_prior_room is best-effort per §12.4 — a LiveKit error
        during kill must NOT block the resume. The new worker still
        gets dispatched; the prior worker's eventual exit (Attention
        TTL, network drop) cleans up the abandoned room."""
        token = jwt_for_actor("act_test")

        async def _raise(interaction):
            raise RuntimeError("LiveKit list_participants timed out")

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=valid_deployment),
        ), patch(
            "harness.sessions._load_interaction",
            new=AsyncMock(return_value=existing_interaction),
        ), patch(
            "harness.sessions._kill_prior_room",
            new=_raise,
        ):
            response = _post_resume(
                client,
                valid_deployment["_id"],
                existing_interaction["_id"],
                token,
            )

        # Resume still succeeded despite kill failure
        assert response.status_code == 200
