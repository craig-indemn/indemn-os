"""Voice per-call correlation_id + effective_actor_id kwargs via indemn()
wrapper per acts_as (AI-407 §5.6 + §13.7).

Voice is single-session-per-process today (LiveKit Agents dispatches one
job per worker process). Process-env mutation is technically safe. BUT —
to keep the harness contract uniform across all 3 harnesses (async + chat +
voice) AND to insulate against any future LiveKit-Agents change to multi-
room-per-worker, voice uses the per-call kwargs pattern from Task 2.5's
updated `indemn()` wrapper.

acts_as resolution (§5.6):
- session_actor → effective_actor_id = dynamic_params["actor_id"] (user_actor
  validated upstream at /sessions; frontdoor already verified JWT.sub ==
  dynamic_params.actor_id when acts_as=session_actor)
- associate_self → effective_actor_id = associate._id (agent acts as itself)

Module path imports + heavy-dep stubs come from `tests/conftest.py`.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestEffectiveActorIdResolution:
    def test_session_actor_resolves_to_dynamic_params_actor_id(self):
        """acts_as=session_actor: effective_actor_id = dynamic_params.actor_id
        (the user driving the conversation — validated upstream by frontdoor)."""
        from session import VoiceSession

        eff = VoiceSession._resolve_effective_actor_id(
            deployment={"acts_as": "session_actor"},
            dynamic_params={"actor_id": "user_act_abc"},
            associate_id="assoc_def",
        )
        assert eff == "user_act_abc"

    def test_associate_self_resolves_to_associate_id(self):
        """acts_as=associate_self: effective_actor_id = associate._id."""
        from session import VoiceSession

        eff = VoiceSession._resolve_effective_actor_id(
            deployment={"acts_as": "associate_self"},
            dynamic_params={"actor_id": "user_act_abc"},  # ignored
            associate_id="assoc_def",
        )
        assert eff == "assoc_def"

    def test_session_actor_with_no_actor_id_falls_back_to_associate(self):
        """Defensive: if acts_as=session_actor but dynamic_params has no
        actor_id (shouldn't happen if frontdoor's parameter_schema validation
        worked, but be safe), fall back to associate_id with a warning."""
        from session import VoiceSession

        eff = VoiceSession._resolve_effective_actor_id(
            deployment={"acts_as": "session_actor"},
            dynamic_params={},  # missing actor_id
            associate_id="assoc_def",
        )
        # Falls back to associate (operator can detect via logs/metrics)
        assert eff == "assoc_def"

    def test_unknown_acts_as_defaults_to_associate(self):
        """Defensive: an unknown acts_as value (operator error or pre-AI-407
        Deployment without the field) defaults to associate_self semantics."""
        from session import VoiceSession

        eff = VoiceSession._resolve_effective_actor_id(
            deployment={},  # no acts_as field
            dynamic_params={"actor_id": "user_act_abc"},
            associate_id="assoc_def",
        )
        assert eff == "assoc_def"


class TestSessionIndemnWrapper:
    """_session_indemn wraps indemn() with per-call correlation_id +
    effective_actor_id kwargs — concurrency-safe vs os.environ mutation
    (matches the chat-deepagents pattern from Task 2.11)."""

    @patch("session.indemn")
    def test_session_indemn_passes_per_call_kwargs(self, mock_indemn):
        from session import VoiceSession

        session = VoiceSession(deployment_id="dep_abc")
        session.correlation_id = "cor_xyz"
        session.associate_id = "assoc_def"
        # Configure for session_actor (effective_actor = user)
        session._effective_actor_id = "user_act_abc"

        session._session_indemn("actor", "get", "some-id")

        mock_indemn.assert_called_once_with(
            "actor",
            "get",
            "some-id",
            correlation_id="cor_xyz",
            effective_actor_id="user_act_abc",
        )

    @patch("session.indemn")
    def test_session_indemn_handles_none_correlation_id(self, mock_indemn):
        """Early-lifecycle calls (before correlation_id arrives via metadata)
        pass None — the cli.indemn() wrapper's per-call kwargs are None-safe."""
        from session import VoiceSession

        session = VoiceSession(deployment_id="dep_abc")
        session.correlation_id = None  # not yet set
        session._effective_actor_id = "assoc_def"

        session._session_indemn("deployment", "get", "dep_abc")

        # Called with correlation_id=None — wrapper handles it
        call_kwargs = mock_indemn.call_args.kwargs
        assert call_kwargs.get("correlation_id") is None
        assert call_kwargs.get("effective_actor_id") == "assoc_def"
