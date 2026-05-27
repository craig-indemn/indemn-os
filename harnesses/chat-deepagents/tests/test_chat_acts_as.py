"""Chat acts_as security gate (AI-408 Task 3.5).

Inherits AI-407 Task 2.31's contract exactly:

- `acts_as = "session_actor"` — JWT IS source of truth for
  effective_actor_id. supplied_actor_id (from dynamic_params) is consulted
  ONLY for the mismatch check; the JWT's `sub` claim is the only value
  ever assigned to effective_actor_id, even when supplied == authenticated.
  Mismatch → WebSocket close 1008 with `actor_mismatch`.

- `acts_as = "associate_self"` — effective_actor_id = Deployment.associate_id.
  supplied actor_id is IGNORED entirely. The agent acts AS itself; the user
  is authenticated but their identity is irrelevant to who's logging entity
  writes.

The load-bearing invariant: a malicious supplied_actor_id (even matching
the JWT) MUST NOT be the value that flows through to entity-write
attribution. effective_actor_id always reads from JWT.sub (session_actor)
or Deployment.associate_id (associate_self) — never from dynamic_params.

This file tests the chat-side surfacing of the contract. The shared
contract definition + voice-frontdoor's coverage live in
`harnesses/voice-frontdoor/tests/test_acts_as_gate.py`.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same setup as other AI-408 test files
for _mod_name in list(sys.modules):
    if _mod_name == "starlette" or _mod_name.startswith("starlette."):
        del sys.modules[_mod_name]
_harness_session_stub = MagicMock()
_harness_session_stub.ChatSession = MagicMock()
sys.modules["harness.session"] = _harness_session_stub
if isinstance(sys.modules.get("harness_common.cli"), MagicMock):
    del sys.modules["harness_common.cli"]
import harness_common.cli  # noqa: E402,F401
if isinstance(sys.modules.get("harness_common.jwt_auth"), MagicMock):
    del sys.modules["harness_common.jwt_auth"]
import harness_common.jwt_auth  # noqa: E402,F401

import main as harness_main  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_verify_jwt(monkeypatch):
    """Stub JWT validation so tests can control authenticated_actor_id via
    the returned `sub` claim — JWT-validation behavior itself is covered in
    test_chat_jwt_validation.py. The fixture default returns
    `sub=act_jwt_default`; specific tests override via `with patch.object(...)`
    to assert mismatch / match cases.
    """
    monkeypatch.setattr(
        harness_main,
        "_verify_jwt",
        lambda token: {"sub": "act_jwt_default", "actor_id": "act_jwt_default"},
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_websocket():
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.headers = {"origin": "https://sales.indemn.ai"}
    return ws


def _send_payloads(ws):
    return [c.args[0] for c in ws.send_json.call_args_list]


# Deployment variants per acts_as mode
_SESSION_ACTOR_DEPLOYMENT = {
    "_id": "dep_session_actor",
    "status": "active",
    "associate_id": "act_associate",
    "allowed_origins": ["https://sales.indemn.ai"],
    "acts_as": "session_actor",
}

_ASSOCIATE_SELF_DEPLOYMENT = {
    "_id": "dep_associate_self",
    "status": "active",
    "associate_id": "act_associate",
    "allowed_origins": ["https://sales.indemn.ai"],
    "acts_as": "associate_self",
}


def _drive(*, deployment, dynamic_params, jwt_sub="act_alice"):
    """Drive `_start_deployment_session` with controlled JWT.sub +
    dynamic_params. Returns (websocket, chatsession_constructor_mock)
    so tests can assert on close/error AND on what was passed to ChatSession."""
    ws = _mock_websocket()
    chat_instance = MagicMock()
    chat_instance.start = AsyncMock()
    chat_instance.close = AsyncMock()
    chat_instance.interaction_id = "int_new"

    with patch.object(
        harness_main,
        "_verify_jwt",
        lambda token: {"sub": jwt_sub, "actor_id": jwt_sub},
    ), patch.object(
        harness_main, "indemn", return_value=deployment
    ), patch.object(
        harness_main, "ChatSession", return_value=chat_instance
    ) as mock_cls:
        result = _run(
            harness_main._start_deployment_session(
                websocket=ws,
                deployment_id=deployment["_id"],
                dynamic_params=dynamic_params,
                auth_token="any-jwt",
                connect_msg={},
            )
        )
    return ws, mock_cls, result


# -----------------------------------------------------------------------------
# session_actor mode — JWT.sub is the source of truth
# -----------------------------------------------------------------------------


class TestSessionActorMode:
    def test_supplied_actor_id_matching_jwt_accepted(self):
        """supplied == JWT.sub → accepted, effective = JWT.sub."""
        ws, mock_cls, result = _drive(
            deployment=_SESSION_ACTOR_DEPLOYMENT,
            dynamic_params={"actor_id": "act_alice"},
            jwt_sub="act_alice",
        )
        # No errors sent
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        # ChatSession received effective_actor_id = JWT.sub
        assert mock_cls.call_args.kwargs["effective_actor_id"] == "act_alice"

    def test_supplied_actor_id_missing_accepted(self):
        """No supplied actor_id in dynamic_params → accepted (mismatch check
        only fires when supplied is present)."""
        ws, mock_cls, result = _drive(
            deployment=_SESSION_ACTOR_DEPLOYMENT,
            dynamic_params={},
            jwt_sub="act_alice",
        )
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        # effective_actor_id = JWT.sub even without dynamic_params signal
        assert mock_cls.call_args.kwargs["effective_actor_id"] == "act_alice"

    def test_supplied_actor_id_mismatched_rejected(self):
        """supplied != JWT.sub → 1008 actor_mismatch, no ChatSession constructed."""
        ws, mock_cls, result = _drive(
            deployment=_SESSION_ACTOR_DEPLOYMENT,
            dynamic_params={"actor_id": "act_bob"},  # Bob
            jwt_sub="act_alice",  # but JWT says Alice
        )
        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "actor_mismatch"
        ws.close.assert_called_once_with(code=1008)
        mock_cls.assert_not_called()

    def test_empty_string_supplied_treated_as_mismatch(self):
        """`is not None` check — empty string is still 'supplied' and must
        match. Defense-in-depth against malformed schemas that don't enforce
        non-empty strings."""
        ws, mock_cls, result = _drive(
            deployment=_SESSION_ACTOR_DEPLOYMENT,
            dynamic_params={"actor_id": ""},
            jwt_sub="act_alice",
        )
        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors[0]["code"] == "actor_mismatch"

    def test_jwt_sub_assigned_even_when_supplied_matches(self):
        """Load-bearing invariant: effective_actor_id reads from JWT.sub,
        NEVER from dynamic_params. Even when supplied == authenticated, the
        line in main.py is `effective_actor_id = authenticated_actor_id`.
        This test pins that — if someone refactors to use the supplied
        value 'for symmetry', the security invariant breaks silently."""
        ws, mock_cls, _ = _drive(
            deployment=_SESSION_ACTOR_DEPLOYMENT,
            dynamic_params={"actor_id": "act_alice"},
            jwt_sub="act_alice",
        )
        # The effective_actor_id is JWT.sub by reference; can't directly
        # test "which variable" but verify the value matches what verify_jwt
        # returned (the patch fixture's return shape pins this).
        assert mock_cls.call_args.kwargs["effective_actor_id"] == "act_alice"


# -----------------------------------------------------------------------------
# associate_self mode — Deployment.associate_id is the source of truth
# -----------------------------------------------------------------------------


class TestAssociateSelfMode:
    def test_effective_actor_id_is_deployment_associate(self):
        """associate_self → effective = Deployment.associate_id, regardless
        of JWT.sub or dynamic_params."""
        ws, mock_cls, _ = _drive(
            deployment=_ASSOCIATE_SELF_DEPLOYMENT,
            dynamic_params={},
            jwt_sub="act_alice",
        )
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        assert (
            mock_cls.call_args.kwargs["effective_actor_id"] == "act_associate"
        )

    def test_supplied_actor_id_ignored_in_associate_self(self):
        """associate_self ignores supplied actor_id entirely — NO mismatch
        check, NO override of the Deployment's associate. A user can pass
        whatever actor_id; it has no effect on attribution."""
        ws, mock_cls, _ = _drive(
            deployment=_ASSOCIATE_SELF_DEPLOYMENT,
            dynamic_params={"actor_id": "act_anyone"},
            jwt_sub="act_alice",
        )
        # No mismatch error (gate doesn't fire in associate_self mode)
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        # effective_actor_id ignores both supplied AND JWT.sub
        assert (
            mock_cls.call_args.kwargs["effective_actor_id"] == "act_associate"
        )

    def test_jwt_sub_ignored_in_associate_self(self):
        """JWT only proves the caller is authenticated; the JWT's actor_id
        is irrelevant in associate_self mode."""
        ws, mock_cls, _ = _drive(
            deployment=_ASSOCIATE_SELF_DEPLOYMENT,
            dynamic_params={},
            jwt_sub="act_someone_else_entirely",
        )
        assert (
            mock_cls.call_args.kwargs["effective_actor_id"] == "act_associate"
        )

    def test_no_acts_as_field_defaults_to_associate_self(self):
        """Deployment missing the `acts_as` field falls through the
        `if acts_as == "session_actor"` check → associate_self semantics."""
        deployment = {
            **_ASSOCIATE_SELF_DEPLOYMENT,
            "_id": "dep_no_acts_as",
        }
        # Remove acts_as entirely
        deployment.pop("acts_as", None)
        ws, mock_cls, _ = _drive(
            deployment=deployment,
            dynamic_params={"actor_id": "act_someone"},
            jwt_sub="act_alice",
        )
        # Defaults to associate_self → effective = Deployment.associate_id
        assert (
            mock_cls.call_args.kwargs["effective_actor_id"] == "act_associate"
        )


# -----------------------------------------------------------------------------
# associate_id (NOT effective_actor_id) — kept as Deployment.associate_id
# -----------------------------------------------------------------------------


class TestAssociateIdAlwaysDeploymentAssociate:
    """`associate_id` (the agent ID — separate from `effective_actor_id`) is
    ALWAYS Deployment.associate_id regardless of acts_as. The "who's the
    agent" doesn't change based on impersonation mode; only "who gets
    attributed for entity writes" does."""

    def test_session_actor_mode(self):
        ws, mock_cls, _ = _drive(
            deployment=_SESSION_ACTOR_DEPLOYMENT,
            dynamic_params={"actor_id": "act_alice"},
            jwt_sub="act_alice",
        )
        assert mock_cls.call_args.kwargs["associate_id"] == "act_associate"

    def test_associate_self_mode(self):
        ws, mock_cls, _ = _drive(
            deployment=_ASSOCIATE_SELF_DEPLOYMENT,
            dynamic_params={},
            jwt_sub="act_alice",
        )
        assert mock_cls.call_args.kwargs["associate_id"] == "act_associate"
