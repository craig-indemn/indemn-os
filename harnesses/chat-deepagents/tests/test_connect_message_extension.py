"""WebSocket connect accepts deployment_id + dynamic_params (AI-408 Task 3.1).

Backward compat with the current OS Base UI's connect-message shape
(`associate_id` only) is non-negotiable. The new Deployment-driven path
is exercised when `deployment_id` is set; otherwise the legacy
ChatSession-with-associate_id flow runs unchanged.

Tests drive `websocket_handler` end-to-end against a mocked WebSocket
that yields one connect message + then raises WebSocketDisconnect so the
message loop exits cleanly. Heavy deps (deepagents, harness.session,
starlette.websockets, etc.) are stubbed by conftest.py — `ChatSession`
and `_start_deployment_session` are patched per-test to verify dispatch
without exercising the deployment-load chain that Tasks 3.2-3.7 will add.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The shared conftest stubs `starlette` (top-level only) + `starlette.websockets`
# to keep most chat-harness tests lightweight. main.py also imports from
# starlette.applications / .responses / .routing, which aren't pre-stubbed —
# Python's import machinery can't pull submodules off a MagicMock parent
# ("'starlette' is not a package"). Drop the starlette stubs in this file so
# main.py imports against the real package (real dep, installed in the venv).
# Same treatment for `harness_common` submodules main.py touches.
for _mod_name in list(sys.modules):
    if _mod_name == "starlette" or _mod_name.startswith("starlette."):
        del sys.modules[_mod_name]

# harness.session isn't in the conftest stub list and main.py imports
# `from harness.session import ChatSession` — stub it now so the import
# resolves to a MagicMock we can patch in tests.
_harness_session_stub = MagicMock()
_harness_session_stub.ChatSession = MagicMock()
sys.modules["harness.session"] = _harness_session_stub

# main.py uses CLIError in an `except` clause (Task 3.2 onward). MagicMock
# can't be caught — replace the conftest's harness_common.cli stub with the
# real module so `except CLIError` resolves to a real class. test_deployment_
# session_start.py does the same — both must reload before main is imported.
if isinstance(sys.modules.get("harness_common.cli"), MagicMock):
    del sys.modules["harness_common.cli"]
import harness_common.cli  # noqa: E402,F401  — real import

import main as harness_main  # noqa: E402


def _run(coro):
    """Drive an async coro from a sync test (pytest-asyncio not in deps)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _build_mock_websocket(connect_msg: dict):
    """Mock WebSocket that yields connect_msg once, then disconnects."""
    from starlette.websockets import WebSocketDisconnect  # stubbed in conftest

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_json = AsyncMock(
        side_effect=[connect_msg, WebSocketDisconnect()]
    )
    ws.headers = {}
    return ws


def _send_payloads(ws):
    """Return list of dicts sent via ws.send_json over the test run."""
    return [c.args[0] for c in ws.send_json.call_args_list]


class TestConnectMessageExtension:
    def test_legacy_associate_id_only_routes_to_chatsession(self):
        """Current OS Base UI flow: connect with associate_id only still
        works — ChatSession constructed via legacy path, no deployment call."""
        connect_msg = {
            "type": "connect",
            "associate_id": "act_legacy",
            "auth_token": "test-token",
        }
        ws = _build_mock_websocket(connect_msg)

        chat_instance = MagicMock()
        chat_instance.start = AsyncMock()
        chat_instance.close = AsyncMock()
        chat_instance.interaction_id = "int_legacy"

        with patch.object(
            harness_main, "ChatSession", return_value=chat_instance
        ) as mock_cls, patch.object(
            harness_main, "_start_deployment_session", new_callable=AsyncMock
        ) as mock_dep:
            _run(harness_main.websocket_handler(ws))

        # Legacy path: ChatSession constructed with associate_id
        mock_cls.assert_called_once()
        kw = mock_cls.call_args.kwargs
        assert kw["associate_id"] == "act_legacy"
        assert kw["auth_token"] == "test-token"

        # Deployment-driven path NOT taken
        mock_dep.assert_not_called()

        # session.start() invoked, "connected" sent with interaction_id
        chat_instance.start.assert_called_once()
        connected = [p for p in _send_payloads(ws) if p.get("type") == "connected"]
        assert len(connected) == 1
        assert connected[0]["interaction_id"] == "int_legacy"

    def test_deployment_id_routes_to_deployment_session(self):
        """New connect with deployment_id calls _start_deployment_session
        with deployment_id + dynamic_params extracted from the connect msg."""
        connect_msg = {
            "type": "connect",
            "deployment_id": "dep_test",
            "dynamic_params": {
                "actor_id": "act_alice",
                "current_route": "/proposals",
            },
            "auth_token": "user-jwt",
        }
        ws = _build_mock_websocket(connect_msg)

        dep_session = MagicMock(interaction_id="int_dep")
        dep_session.close = AsyncMock()

        with patch.object(
            harness_main,
            "_start_deployment_session",
            new_callable=AsyncMock,
            return_value=dep_session,
        ) as mock_dep, patch.object(
            harness_main, "ChatSession"
        ) as mock_cls:
            _run(harness_main.websocket_handler(ws))

        # Deployment path taken with full kwarg set
        mock_dep.assert_called_once()
        kw = mock_dep.call_args.kwargs
        assert kw["deployment_id"] == "dep_test"
        assert kw["dynamic_params"] == {
            "actor_id": "act_alice",
            "current_route": "/proposals",
        }
        assert kw["auth_token"] == "user-jwt"
        assert kw["connect_msg"] is connect_msg

        # Legacy ChatSession NOT constructed
        mock_cls.assert_not_called()

        # "connected" message sent with the deployment session's interaction_id
        connected = [p for p in _send_payloads(ws) if p.get("type") == "connected"]
        assert len(connected) == 1
        assert connected[0]["interaction_id"] == "int_dep"

    def test_deployment_id_wins_when_both_present(self):
        """When both deployment_id and associate_id present, deployment_id
        takes precedence — the Deployment-driven path runs."""
        connect_msg = {
            "type": "connect",
            "deployment_id": "dep_test",
            "associate_id": "act_legacy",  # ignored when deployment_id set
            "auth_token": "tok",
        }
        ws = _build_mock_websocket(connect_msg)

        dep_session = MagicMock(interaction_id="int_dep")
        dep_session.close = AsyncMock()
        with patch.object(
            harness_main,
            "_start_deployment_session",
            new_callable=AsyncMock,
            return_value=dep_session,
        ) as mock_dep, patch.object(
            harness_main, "ChatSession"
        ) as mock_cls:
            _run(harness_main.websocket_handler(ws))

        mock_dep.assert_called_once()
        mock_cls.assert_not_called()

    def test_neither_field_returns_error_and_closes(self):
        """Missing both deployment_id and associate_id → error message
        naming both fields + WebSocket close."""
        connect_msg = {"type": "connect", "auth_token": "tok"}
        ws = _build_mock_websocket(connect_msg)

        with patch.object(
            harness_main, "_start_deployment_session", new_callable=AsyncMock
        ) as mock_dep, patch.object(harness_main, "ChatSession") as mock_cls:
            _run(harness_main.websocket_handler(ws))

        mock_dep.assert_not_called()
        mock_cls.assert_not_called()

        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        content = errors[0]["content"]
        # Error message references both field names so the caller knows
        # they can supply either one.
        assert "deployment_id" in content
        assert "associate_id" in content
        ws.close.assert_called()

    def test_dynamic_params_must_be_dict(self):
        """dynamic_params present but not a JSON object → error + close
        BEFORE dispatching to either path."""
        connect_msg = {
            "type": "connect",
            "deployment_id": "dep_test",
            "dynamic_params": "not-a-dict",
            "auth_token": "tok",
        }
        ws = _build_mock_websocket(connect_msg)

        with patch.object(
            harness_main, "_start_deployment_session", new_callable=AsyncMock
        ) as mock_dep, patch.object(harness_main, "ChatSession") as mock_cls:
            _run(harness_main.websocket_handler(ws))

        mock_dep.assert_not_called()
        mock_cls.assert_not_called()
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert "dynamic_params" in errors[0]["content"]
        ws.close.assert_called()

    def test_dynamic_params_defaults_to_empty_dict(self):
        """dynamic_params omitted → defaults to {} forwarded to the
        deployment-session call (not None)."""
        connect_msg = {
            "type": "connect",
            "deployment_id": "dep_test",
            "auth_token": "tok",
            # no dynamic_params field at all
        }
        ws = _build_mock_websocket(connect_msg)

        dep_session = MagicMock(interaction_id="int_dep")
        dep_session.close = AsyncMock()
        with patch.object(
            harness_main,
            "_start_deployment_session",
            new_callable=AsyncMock,
            return_value=dep_session,
        ) as mock_dep, patch.object(harness_main, "ChatSession"):
            _run(harness_main.websocket_handler(ws))

        mock_dep.assert_called_once()
        assert mock_dep.call_args.kwargs["dynamic_params"] == {}

    def test_dynamic_params_null_defaults_to_empty_dict(self):
        """dynamic_params=None in the JSON also defaults to {} (handled by
        `connect_msg.get('dynamic_params') or {}` in main.py)."""
        connect_msg = {
            "type": "connect",
            "deployment_id": "dep_test",
            "dynamic_params": None,
            "auth_token": "tok",
        }
        ws = _build_mock_websocket(connect_msg)

        dep_session = MagicMock(interaction_id="int_dep")
        dep_session.close = AsyncMock()
        with patch.object(
            harness_main,
            "_start_deployment_session",
            new_callable=AsyncMock,
            return_value=dep_session,
        ) as mock_dep, patch.object(harness_main, "ChatSession"):
            _run(harness_main.websocket_handler(ws))

        mock_dep.assert_called_once()
        assert mock_dep.call_args.kwargs["dynamic_params"] == {}


# NOTE: The Task 3.1 commit shipped `_start_deployment_session` as a stub
# that emitted `code: "not_implemented"` + close 4501. Task 3.2 (next commit
# in this branch) replaces the stub with the real Deployment-load flow —
# end-to-end coverage of that behavior moves to test_deployment_session_start.py.
