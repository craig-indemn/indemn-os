"""Chat WebSocket Origin allowlist validation (AI-408 Task 3.3).

Origin is validated against `Deployment.allowed_origins` AFTER the Deployment
is loaded (need allowed_origins from it) but BEFORE the status check (so an
unauthorized origin can't probe Deployment activation state). WebSocket close
code 1008 ("Policy Violation" per RFC 6455) is the canonical WebSocket-side
analog of voice-frontdoor's HTTP 403.

Empty `allowed_origins` rejects ALL — embed snippets need to be explicit
about which surfaces they permit (Track 13f equivalent for chat).
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same setup as test_deployment_session_start.py — unstub starlette + reload
# real harness_common.cli + stub harness.session.
for _mod_name in list(sys.modules):
    if _mod_name == "starlette" or _mod_name.startswith("starlette."):
        del sys.modules[_mod_name]
_harness_session_stub = MagicMock()
_harness_session_stub.ChatSession = MagicMock()
sys.modules["harness.session"] = _harness_session_stub
if isinstance(sys.modules.get("harness_common.cli"), MagicMock):
    del sys.modules["harness_common.cli"]
import harness_common.cli  # noqa: E402,F401
import main as harness_main  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_verify_jwt(monkeypatch):
    """Task 3.4 added JWT validation in the chain. Origin tests want to
    exercise the Origin gate specifically — stub JWT validation so it
    doesn't reject test tokens before Origin failures surface (Origin runs
    BEFORE JWT in the validation chain, but the "matches → continues"
    happy-path test needs to reach status check + ChatSession construction
    which is after JWT)."""
    monkeypatch.setattr(
        harness_main,
        "_verify_jwt",
        lambda token: {"sub": "act_test", "actor_id": "act_test"},
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_websocket(origin: str | None):
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.headers = {"origin": origin} if origin else {}
    return ws


def _send_payloads(ws):
    return [c.args[0] for c in ws.send_json.call_args_list]


_DEPLOYMENT_WITH_ORIGINS = {
    "_id": "dep_with_origins",
    "status": "active",
    "associate_id": "act_associate",
    "allowed_origins": [
        "https://sales.indemn.ai",
        "https://app.indemn.ai",
    ],
    "acts_as": "associate_self",
}


_DEPLOYMENT_WITH_NO_ORIGINS = {
    **_DEPLOYMENT_WITH_ORIGINS,
    "_id": "dep_no_origins",
    "allowed_origins": [],
}


class TestIsOriginAllowed:
    """The pure helper — no WebSocket, no Deployment, just the predicate."""

    def test_allowed_origin_passes(self):
        assert harness_main._is_origin_allowed(
            "https://sales.indemn.ai", ["https://sales.indemn.ai"]
        )

    def test_unallowed_origin_rejects(self):
        assert not harness_main._is_origin_allowed(
            "https://malicious.example.com", ["https://sales.indemn.ai"]
        )

    def test_empty_allowlist_rejects_all(self):
        """Empty allowed_origins = reject all per §5.1 (Track 13f equivalent
        for chat). Even an "obviously safe" origin gets rejected — operators
        must opt-in surfaces explicitly."""
        assert not harness_main._is_origin_allowed(
            "https://sales.indemn.ai", []
        )

    def test_missing_origin_header_rejects(self):
        """RFC 6455 requires browsers to send Origin on WebSocket upgrade.
        Absence indicates a non-browser client we have no policy for."""
        assert not harness_main._is_origin_allowed(
            None, ["https://sales.indemn.ai"]
        )

    def test_empty_string_origin_rejects(self):
        assert not harness_main._is_origin_allowed(
            "", ["https://sales.indemn.ai"]
        )

    def test_match_is_case_sensitive(self):
        """RFC 6454: Origin headers are case-sensitive. Exact match only."""
        assert not harness_main._is_origin_allowed(
            "https://Sales.Indemn.Ai", ["https://sales.indemn.ai"]
        )

    def test_multiple_allowlist_entries(self):
        """Any entry in the allowlist matches → allowed."""
        allowed = ["https://sales.indemn.ai", "https://app.indemn.ai"]
        assert harness_main._is_origin_allowed("https://app.indemn.ai", allowed)
        assert harness_main._is_origin_allowed("https://sales.indemn.ai", allowed)
        assert not harness_main._is_origin_allowed(
            "https://other.indemn.ai", allowed
        )

    def test_no_wildcard_support_v1(self):
        """v1 is exact-match only; no `https://*.indemn.ai` wildcards. If
        operators need wildcards they enumerate explicitly."""
        assert not harness_main._is_origin_allowed(
            "https://x.indemn.ai", ["https://*.indemn.ai"]
        )


class TestStartDeploymentSessionOriginGate:
    """Integration: the Origin check runs in `_start_deployment_session`
    AFTER Deployment load + BEFORE status check."""

    def test_mismatched_origin_rejected_with_1008(self):
        """Origin not in allowed_origins → WS close 1008 + origin_not_allowed."""
        ws = _mock_websocket(origin="https://malicious.example.com")

        with patch.object(
            harness_main, "indemn", return_value=_DEPLOYMENT_WITH_ORIGINS
        ), patch.object(harness_main, "ChatSession") as mock_cls:
            result = _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_with_origins",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "origin_not_allowed"
        assert "malicious.example.com" in errors[0]["content"]
        ws.close.assert_called_once_with(code=1008)
        # ChatSession NOT constructed
        mock_cls.assert_not_called()

    def test_missing_origin_rejected(self):
        """No Origin header on the WS upgrade → reject (same close code as
        explicit mismatch). RFC 6455 mandates the header on browsers."""
        ws = _mock_websocket(origin=None)

        with patch.object(
            harness_main, "indemn", return_value=_DEPLOYMENT_WITH_ORIGINS
        ), patch.object(harness_main, "ChatSession") as mock_cls:
            result = _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_with_origins",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        assert result is None
        ws.close.assert_called_once_with(code=1008)
        mock_cls.assert_not_called()

    def test_empty_allowed_origins_rejects_all(self):
        """Deployment with allowed_origins=[] rejects every connection per
        Track 13f. Operators MUST opt-in explicitly."""
        ws = _mock_websocket(origin="https://sales.indemn.ai")

        with patch.object(
            harness_main, "indemn", return_value=_DEPLOYMENT_WITH_NO_ORIGINS
        ), patch.object(harness_main, "ChatSession") as mock_cls:
            result = _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_no_origins",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        assert result is None
        ws.close.assert_called_once_with(code=1008)
        mock_cls.assert_not_called()

    def test_matching_origin_proceeds_through_chain(self):
        """Matched origin → Origin check passes → continues to status check
        → (active Deployment) → ChatSession constructed."""
        ws = _mock_websocket(origin="https://sales.indemn.ai")
        chat_instance = MagicMock()
        chat_instance.start = AsyncMock()
        chat_instance.close = AsyncMock()
        chat_instance.interaction_id = "int_new"

        with patch.object(
            harness_main, "indemn", return_value=_DEPLOYMENT_WITH_ORIGINS
        ), patch.object(
            harness_main, "ChatSession", return_value=chat_instance
        ) as mock_cls:
            result = _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_with_origins",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        assert result is chat_instance
        # No errors sent on happy path
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        # ChatSession constructed + start() called
        mock_cls.assert_called_once()
        chat_instance.start.assert_called_once()

    def test_origin_check_runs_before_status_check(self):
        """Critical security property: an unauthorized origin must NOT learn
        whether a Deployment is active/paused. With Origin check BEFORE
        status check, a wrong-origin attacker on a paused Deployment gets
        the SAME 1008 origin_not_allowed they'd get on an active Deployment
        — no information leak."""
        paused_dep_with_origins = {
            **_DEPLOYMENT_WITH_ORIGINS,
            "_id": "dep_paused",
            "status": "paused",
        }
        ws = _mock_websocket(origin="https://malicious.example.com")

        with patch.object(
            harness_main, "indemn", return_value=paused_dep_with_origins
        ), patch.object(harness_main, "ChatSession"):
            _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_paused",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )

        # The attacker gets origin_not_allowed (1008), NOT deployment_not_active
        # (4009). Status of the Deployment isn't disclosed.
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "origin_not_allowed"
        ws.close.assert_called_once_with(code=1008)
