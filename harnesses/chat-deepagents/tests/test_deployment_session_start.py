"""`_start_deployment_session` loads Deployment + status check (AI-408 Task 3.2).

This task adds the first stage of the validation chain:
- Load Deployment via authenticated CLI
- Status check (only `active` accepts sessions)
- Pass Deployment + dynamic_params + effective_actor_id into ChatSession

Subsequent tasks layer Origin (3.3), JWT (3.4), acts_as (3.5),
parameter_schema (3.6), and deployment_context SystemMessage (3.7) onto
this scaffold. Tests here pin the contract before those layers exist so
regressions during the additive layering will be caught immediately.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Unstub starlette per `test_connect_message_extension.py` rationale — main.py
# imports starlette.applications etc., which the conftest's flat MagicMock
# can't satisfy. Stub harness.session (also absent from conftest stub list).
for _mod_name in list(sys.modules):
    if _mod_name == "starlette" or _mod_name.startswith("starlette."):
        del sys.modules[_mod_name]
_harness_session_stub = MagicMock()
_harness_session_stub.ChatSession = MagicMock()
sys.modules["harness.session"] = _harness_session_stub

# The conftest stubs `harness_common.cli` as MagicMock to keep most chat tests
# lightweight — but main.py does `from harness_common.cli import CLIError` and
# uses CLIError in an `except` clause. A MagicMock isn't a class, so Python
# raises "catching classes that do not inherit from BaseException is not
# allowed" the moment the except clause is evaluated. Reload the real module
# (harnesses/_base/ is on sys.path via conftest) so CLIError is a real class
# AND indemn is real (we patch the latter per-test).
if isinstance(sys.modules.get("harness_common.cli"), MagicMock):
    del sys.modules["harness_common.cli"]
import harness_common.cli  # noqa: E402,F401  — real import

import main as harness_main  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_websocket(origin: str | None = "https://sales.indemn.ai"):
    """Default origin matches `_ACTIVE_DEPLOYMENT.allowed_origins` so Task 3.2
    tests pass through the Task 3.3 Origin check transparently. Tests that
    exercise Origin rejection explicitly pass a different origin (or None)."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.headers = {"origin": origin} if origin else {}
    return ws


def _send_payloads(ws):
    return [c.args[0] for c in ws.send_json.call_args_list]


# Sample Deployment shapes — minimal fields _start_deployment_session reads
_ACTIVE_DEPLOYMENT = {
    "_id": "dep_active",
    "status": "active",
    "associate_id": "act_associate",
    "name": "Sales Web Chat",
    "allowed_origins": ["https://sales.indemn.ai"],
    "acts_as": "associate_self",
    "static_parameters": {"role": "sales"},
}

_PAUSED_DEPLOYMENT = {
    **_ACTIVE_DEPLOYMENT,
    "_id": "dep_paused",
    "status": "paused",
}


class TestUnknownDeployment:
    def test_clierror_on_get_returns_404_close(self):
        """CLI fails (unknown deployment_id) → send not_found + WS close 4004."""
        ws = _mock_websocket()
        from harness_common.cli import CLIError

        # Use an explicit callable rather than patch.object(..., side_effect=X)
        # — that form's 3rd positional has been interpreted as `new` in some
        # patch.object call shapes, leaving side_effect unused. Function form
        # is unambiguous.
        def _raises_404(*args, **kwargs):
            raise CLIError("CLI failed (1): 404 Not Found")

        with patch.object(harness_main, "indemn", _raises_404):
            result = _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_nonexistent",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "not_found"
        assert "dep_nonexistent" in errors[0]["content"]
        ws.close.assert_called_once_with(code=4004)


class TestDeploymentStatusCheck:
    def test_paused_deployment_rejects_with_409(self):
        """Inactive Deployment (status=paused) → 4009 close + deployment_not_active code."""
        ws = _mock_websocket()

        with patch.object(harness_main, "indemn", return_value=_PAUSED_DEPLOYMENT):
            result = _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_paused",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "deployment_not_active"
        # The status field surfaces the actual status so the SDK can show a
        # specific message ("temporarily paused" vs generic "unavailable").
        assert errors[0]["status"] == "paused"
        ws.close.assert_called_once_with(code=4009)

    def test_configured_status_also_rejects(self):
        """`configured` is also not-yet-launchable per §5.7 — same 4009 close."""
        ws = _mock_websocket()
        deployment = {**_ACTIVE_DEPLOYMENT, "status": "configured"}

        with patch.object(harness_main, "indemn", return_value=deployment):
            result = _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_configured",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        assert result is None
        ws.close.assert_called_once_with(code=4009)


class TestActiveDeploymentHappyPath:
    def test_active_deployment_constructs_chatsession(self):
        """Active Deployment → ChatSession constructed with deployment +
        dynamic_params + effective_actor_id; start() called; session returned."""
        ws = _mock_websocket()
        dynamic_params = {"actor_id": "act_alice", "current_route": "/proposals"}

        chat_instance = MagicMock()
        chat_instance.start = AsyncMock()
        chat_instance.close = AsyncMock()
        chat_instance.interaction_id = "int_new"

        with patch.object(
            harness_main, "indemn", return_value=_ACTIVE_DEPLOYMENT
        ), patch.object(
            harness_main, "ChatSession", return_value=chat_instance
        ) as mock_cls:
            result = _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_active",
                    dynamic_params=dynamic_params,
                    auth_token="user-jwt",
                    connect_msg={"interaction_id": "int_resume"},
                )
            )

        assert result is chat_instance
        mock_cls.assert_called_once()
        kw = mock_cls.call_args.kwargs

        # Identity-flowed: Deployment's associate_id is both associate_id
        # AND effective_actor_id (pre-Task-3.5 default = associate_self
        # semantics). Task 3.5 will diverge these for session_actor mode.
        assert kw["associate_id"] == "act_associate"
        assert kw["effective_actor_id"] == "act_associate"

        # Deployment + dynamic_params flowed through
        assert kw["deployment"] is _ACTIVE_DEPLOYMENT
        assert kw["dynamic_params"] == dynamic_params

        # Other passed-through fields
        assert kw["auth_token"] == "user-jwt"
        assert kw["interaction_id"] == "int_resume"

        chat_instance.start.assert_called_once()

    def test_no_error_sent_on_happy_path(self):
        """Happy path sends no error messages — the websocket_handler
        wrapper is responsible for the 'connected' confirmation, not this
        helper."""
        ws = _mock_websocket()
        chat_instance = MagicMock()
        chat_instance.start = AsyncMock()
        chat_instance.close = AsyncMock()
        chat_instance.interaction_id = "int_new"

        with patch.object(
            harness_main, "indemn", return_value=_ACTIVE_DEPLOYMENT
        ), patch.object(
            harness_main, "ChatSession", return_value=chat_instance
        ):
            _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_active",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        ws.close.assert_not_called()

    def test_cli_call_uses_correct_deployment_id(self):
        """Verify indemn is invoked with `deployment get <deployment_id>`."""
        ws = _mock_websocket()
        chat_instance = MagicMock()
        chat_instance.start = AsyncMock()
        chat_instance.close = AsyncMock()
        chat_instance.interaction_id = "int_new"

        with patch.object(
            harness_main, "indemn", return_value=_ACTIVE_DEPLOYMENT
        ) as mock_indemn, patch.object(
            harness_main, "ChatSession", return_value=chat_instance
        ):
            _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_xyz",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        # asyncio.to_thread passes positional args through to indemn
        mock_indemn.assert_called_once_with("deployment", "get", "dep_xyz")


class TestChatSessionInitNewKwargs:
    """ChatSession.__init__ accepts deployment + dynamic_params +
    effective_actor_id (AI-408 additions) without breaking the legacy
    associate_id-only call shape used by the OS Base UI."""

    def test_legacy_init_still_works_without_new_kwargs(self):
        """Legacy callers (websocket_handler's `else:` branch) construct
        ChatSession with no deployment/dynamic_params/effective_actor_id.
        Result: deployment=None, dynamic_params={}, effective_actor_id
        defaulted to associate_id."""
        from session import ChatSession

        s = ChatSession(
            websocket=MagicMock(),
            associate_id="act_legacy",
            auth_token="tok",
        )
        assert s.deployment is None
        assert s.dynamic_params == {}
        assert s.effective_actor_id == "act_legacy"

    def test_init_with_new_kwargs_stores_them(self):
        from session import ChatSession

        deployment = {"_id": "dep_x", "status": "active"}
        s = ChatSession(
            websocket=MagicMock(),
            associate_id="act_associate",
            auth_token="tok",
            deployment=deployment,
            dynamic_params={"actor_id": "act_alice"},
            effective_actor_id="act_alice",
        )
        assert s.deployment is deployment
        assert s.dynamic_params == {"actor_id": "act_alice"}
        assert s.effective_actor_id == "act_alice"

    def test_effective_actor_id_defaults_to_associate_id(self):
        """When effective_actor_id is not supplied, default to associate_id —
        keeps the legacy + pre-Task-3.5 deployment paths producing identical
        forensics ('the chat associate acted')."""
        from session import ChatSession

        s = ChatSession(
            websocket=MagicMock(),
            associate_id="act_associate",
            auth_token="tok",
            deployment={"_id": "dep_x", "status": "active"},
            dynamic_params={},
        )
        assert s.effective_actor_id == "act_associate"

    def test_dynamic_params_none_normalizes_to_empty_dict(self):
        """An explicit dynamic_params=None (e.g., from a connect message with
        the field present but null) is normalized to {} so downstream code
        can assume a dict shape."""
        from session import ChatSession

        s = ChatSession(
            websocket=MagicMock(),
            associate_id="act_a",
            auth_token="tok",
            dynamic_params=None,
        )
        assert s.dynamic_params == {}


class TestSessionIndemnUsesEffectiveActorId:
    """`_session_indemn` reads `self.effective_actor_id` (AI-408 wire-up).
    The previous AI-407 implementation passed `self.associate_id` directly;
    Task 3.2 switches to the attribute so Task 3.5 can change the value
    without touching the call site."""

    @patch("session.indemn")
    def test_passes_effective_actor_id_kwarg(self, mock_indemn):
        from session import ChatSession

        mock_indemn.return_value = {}
        s = ChatSession(
            websocket=MagicMock(),
            associate_id="act_associate",
            auth_token="tok",
            effective_actor_id="act_alice",  # impersonating Alice
        )
        s.correlation_id = "cor_test"

        s._session_indemn("actor", "get", "act_x")

        kwargs = mock_indemn.call_args.kwargs
        assert kwargs["effective_actor_id"] == "act_alice"
        assert kwargs["correlation_id"] == "cor_test"

    @patch("session.indemn")
    def test_legacy_session_still_uses_associate_id(self, mock_indemn):
        """For legacy (no effective_actor_id arg) sessions, the default
        effective_actor_id = associate_id keeps backward compat: same
        forensics attribution as pre-AI-408."""
        from session import ChatSession

        mock_indemn.return_value = {}
        s = ChatSession(
            websocket=MagicMock(),
            associate_id="act_legacy",
            auth_token="tok",
        )

        s._session_indemn("actor", "get", "act_x")
        kwargs = mock_indemn.call_args.kwargs
        assert kwargs["effective_actor_id"] == "act_legacy"
