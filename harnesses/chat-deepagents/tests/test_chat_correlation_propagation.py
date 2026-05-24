"""Tests for chat per-call indemn() kwargs (AI-407 Task 2.11).

Chat is multi-session-per-process (one WebSocket process serves many concurrent
sessions in the same event loop). Mutating os.environ to set
INDEMN_CORRELATION_ID at session start races with concurrent sessions and
contaminates cross-session lineage attribution.

Task 2.5 added `correlation_id` + `effective_actor_id` kwargs to the
harness_common.cli.indemn() wrapper. Task 2.11 wires every indemn() call in
ChatSession to pass session-local values via those kwargs (no os.environ
mutation).

The wiring is via a small `_session_indemn(*args)` helper on ChatSession that
captures the session's correlation_id + associate_id and passes them through.
"""

from unittest.mock import MagicMock, patch

from session import ChatSession


class TestSessionIndemnHelper:
    @patch("session.indemn")
    def test_session_indemn_passes_correlation_id_and_actor(self, mock_indemn):
        """_session_indemn forwards self.correlation_id + self.associate_id
        as kwargs to the harness_common.cli.indemn() wrapper."""
        mock_indemn.return_value = {}

        session = ChatSession(
            websocket=MagicMock(),
            associate_id="act_alice",
            auth_token="tok",
        )
        session.correlation_id = "cor_alice"

        session._session_indemn("actor", "get", "act_alice")

        mock_indemn.assert_called_once()
        kwargs = mock_indemn.call_args.kwargs
        assert kwargs["correlation_id"] == "cor_alice"
        assert kwargs["effective_actor_id"] == "act_alice"

    @patch("session.indemn")
    def test_session_indemn_handles_unset_correlation_id(self, mock_indemn):
        """Early-lifecycle calls (before self.correlation_id is set) are safe
        — correlation_id=None is a no-op per Task 2.5's wrapper (skips the
        env-setting branch)."""
        mock_indemn.return_value = {}

        session = ChatSession(
            websocket=MagicMock(),
            associate_id="act_alice",
            auth_token="tok",
        )
        # correlation_id NOT yet set (still None from __init__)
        assert session.correlation_id is None

        session._session_indemn("actor", "get", "act_alice")

        kwargs = mock_indemn.call_args.kwargs
        assert kwargs["correlation_id"] is None
        assert kwargs["effective_actor_id"] == "act_alice"

    @patch("session.indemn")
    def test_session_indemn_forwards_positional_args(self, mock_indemn):
        """Positional CLI args pass through untouched."""
        mock_indemn.return_value = {}

        session = ChatSession(
            websocket=MagicMock(),
            associate_id="act_alice",
            auth_token="tok",
        )

        session._session_indemn("interaction", "update", "int_xyz", "--data", '{"x": 1}')

        positional = mock_indemn.call_args.args
        assert positional == ("interaction", "update", "int_xyz", "--data", '{"x": 1}')

    @patch("session.indemn")
    def test_two_sessions_pass_different_correlation_ids(self, mock_indemn):
        """Concurrency-safety smoke: two ChatSession instances with different
        correlation_ids pass their OWN correlation_id when _session_indemn
        is called — no shared os.environ mutation, no cross-session clobber.

        (Per-process mutation race is what Task 2.5's wrapper kwargs solve;
        this test verifies session.py actually uses them.)
        """
        mock_indemn.return_value = {}
        envs_seen = []
        mock_indemn.side_effect = lambda *a, **kw: envs_seen.append(kw) or {}

        session_a = ChatSession(
            websocket=MagicMock(), associate_id="act_a", auth_token="tok"
        )
        session_a.correlation_id = "cor_alice"

        session_b = ChatSession(
            websocket=MagicMock(), associate_id="act_b", auth_token="tok"
        )
        session_b.correlation_id = "cor_bob"

        # Interleaved calls — would race if implementation used os.environ
        session_a._session_indemn("actor", "get", "act_a")
        session_b._session_indemn("actor", "get", "act_b")
        session_a._session_indemn("interaction", "get", "int_a")

        correlation_ids = [kw["correlation_id"] for kw in envs_seen]
        assert correlation_ids == ["cor_alice", "cor_bob", "cor_alice"]

        actor_ids = [kw["effective_actor_id"] for kw in envs_seen]
        assert actor_ids == ["act_a", "act_b", "act_a"]
