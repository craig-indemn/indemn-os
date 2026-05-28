"""Chat WebSocket close-frame on unhandled error (AI-409 smoke Bug A
secondary defect).

Pre-fix: when an unexpected exception (e.g., KeyError from a missing env
var like JWT_SIGNING_KEY) escaped the explicit handlers in
`websocket_handler`, the catch-all `except Exception` block logged the
error and let the connection drop. The transport closed with code 1006
"abnormal closure" — no close frame, no reason, no clean signal to the
SDK.

Post-fix: the catch-all sends a `{"type": "error", "code":
"internal_error"}` JSON frame (best-effort) followed by a proper
WebSocket close with code 1011 ("Server is terminating connection due
to an unexpected condition") per RFC 6455.

This regression test pins the close-frame behavior by simulating an
unexpected KeyError during the first receive_json call.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Unstub starlette so the real WebSocketDisconnect exception class is
# present (conftest stubs it with MagicMock at module load).
for _mod_name in list(sys.modules):
    if _mod_name == "starlette" or _mod_name.startswith("starlette."):
        del sys.modules[_mod_name]

# Unstub harness_common.cli so the real package loads (matches the
# pattern used by AI-408 chat tests).
if isinstance(sys.modules.get("harness_common.cli"), MagicMock):
    del sys.modules["harness_common.cli"]
import harness_common.cli  # noqa: E402,F401

# Stub harness.session (its full imports require checkpointer + langchain
# wiring we don't exercise here).
_harness_session_stub = MagicMock()
_harness_session_stub.ChatSession = MagicMock()
sys.modules["harness.session"] = _harness_session_stub

import main as harness_main  # noqa: E402


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
    ws.headers = {"origin": "http://localhost:5173"}
    return ws


def test_unhandled_keyerror_sends_1011_close_frame():
    """An unexpected KeyError (simulating missing env var) escapes into
    the catch-all `except Exception` block. The handler must send a
    proper WS close with code 1011 so the SDK reads a clean failure
    rather than a 1006 abnormal closure.
    """
    ws = _mock_websocket()
    # First receive_json raises KeyError — simulates an unexpected
    # internal error (e.g., os.environ['JWT_SIGNING_KEY'] when the var
    # is unset, which was the AI-409 smoke Bug A primary scenario).
    ws.receive_json = AsyncMock(
        side_effect=KeyError("JWT_SIGNING_KEY")
    )

    _run(harness_main.websocket_handler(ws))

    # The catch-all path must call ws.close with the canonical
    # "internal error" code per RFC 6455.
    ws.close.assert_called_with(code=1011, reason="internal_error")


def test_unhandled_error_emits_json_error_before_close():
    """Before closing the socket, the handler should best-effort send a
    JSON error frame so SDK consumers reading the WS stream can show a
    meaningful message in addition to the close code.
    """
    ws = _mock_websocket()
    ws.receive_json = AsyncMock(side_effect=RuntimeError("boom"))

    _run(harness_main.websocket_handler(ws))

    # send_json should have been called with the internal_error payload.
    payloads = [c.args[0] for c in ws.send_json.call_args_list]
    assert any(
        isinstance(p, dict)
        and p.get("type") == "error"
        and p.get("code") == "internal_error"
        for p in payloads
    ), f"Expected internal_error frame; got: {payloads}"


def test_close_swallows_secondary_failure():
    """If the websocket is already closed (or the transport is dead),
    the close call inside the catch-all must not propagate — otherwise
    the handler crashes the worker on top of the original error. The
    fix wraps the close call in try/except.

    The pre-fix code never reached close()/send_json() in the catch-all
    at all (it just logged), so this test must also assert close + send
    were actually attempted — otherwise it would pass equally under the
    pre-fix code (per code-reviewer S2).
    """
    ws = _mock_websocket()
    ws.receive_json = AsyncMock(side_effect=ValueError("first"))
    # close() raises — simulates the WS already being torn down.
    ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
    # send_json also raises — defense in depth.
    ws.send_json = AsyncMock(side_effect=RuntimeError("already closed"))

    # Should NOT raise — both inner try/except blocks swallow.
    _run(harness_main.websocket_handler(ws))

    # AND both close + send_json should have been attempted (otherwise
    # the pre-fix code's silent log-and-drop would also satisfy "did not
    # raise"). These assertions pin the regression: the fix attempts
    # both calls even when they're guaranteed to fail.
    assert ws.close.called, "close should have been attempted despite side_effect"
    assert ws.send_json.called, "send_json should have been attempted despite side_effect"
