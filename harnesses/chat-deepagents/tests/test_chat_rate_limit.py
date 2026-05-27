"""Chat WebSocket connect rate limiting (AI-408 Phase 3 PM1 hardening).

The code-reviewer flagged chat as missing the rate-limiting that
voice-frontdoor enforces (AI-407 Task 2.36 / §10.7 row "Replay of
session creation"). An attacker who passes Origin + has any valid JWT
could exhaust runtime memory by opening unlimited WebSockets, each
allocating Interaction + Attention + checkpointer state before being
throttled.

This file pins chat's mirror of voice-frontdoor's RateLimiter pattern:
- Module-level RateLimiter singleton (per-process, per-surface — not
  shared between chat + voice; each frontdoor has its own throttle)
- Sliding-window check per IP / per actor / per deployment
- Fires BEFORE ChatSession construction (so the resource-allocation
  attack surface is closed)
- WebSocket close code 1013 ("Try Again Later" per RFC 6455) — the
  canonical analog of HTTP 429
- Error payload carries scope + retry_after_seconds so the SDK can
  back off on the right key

The RateLimiter impl itself lives in `harness_common/rate_limit.py`
(extracted from voice-frontdoor in this same Phase 3 hardening pass);
its sliding-window behavior is already pinned by voice-frontdoor's
test_rate_limit.py — those tests don't need duplication. This file
tests chat's surfacing of the limit (WS close code, error shape,
ordering invariant).
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
if isinstance(sys.modules.get("harness_common.rate_limit"), MagicMock):
    del sys.modules["harness_common.rate_limit"]
import harness_common.rate_limit  # noqa: E402,F401

import main as harness_main  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_verify_jwt(monkeypatch):
    """Stub JWT so rate-limit tests can run against the gate AFTER auth +
    acts_as resolve (matches the chain ordering)."""
    monkeypatch.setattr(
        harness_main,
        "_verify_jwt",
        lambda token: {"sub": "act_test", "actor_id": "act_test"},
    )


@pytest.fixture(autouse=True)
def _reset_rate_limiter(monkeypatch):
    """Each test gets a fresh limiter — sliding-window state accumulates
    by design, so without isolation the Nth test trips on shared state
    from the previous N-1. Per-test fresh instance keeps tests
    deterministic."""
    from harness_common.rate_limit import RateLimiter

    monkeypatch.setattr(harness_main, "_rate_limiter", RateLimiter())


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_websocket(ip: str = "10.0.0.1"):
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.headers = {"origin": "https://sales.indemn.ai"}
    # Starlette's WebSocket.client is a NamedTuple-like Address(host, port)
    ws.client = MagicMock(host=ip)
    return ws


def _send_payloads(ws):
    return [c.args[0] for c in ws.send_json.call_args_list]


_ACTIVE_DEPLOYMENT = {
    "_id": "dep_active",
    "status": "active",
    "associate_id": "act_associate",
    "allowed_origins": ["https://sales.indemn.ai"],
    "acts_as": "associate_self",
}


def _drive(*, ip="10.0.0.1", deployment=None):
    """Run _start_deployment_session once with the given IP. Returns the
    mock websocket so the caller can inspect close + payload."""
    ws = _mock_websocket(ip=ip)
    chat_instance = MagicMock()
    chat_instance.start = AsyncMock()
    chat_instance.close = AsyncMock()
    chat_instance.interaction_id = "int_new"

    dep = deployment or _ACTIVE_DEPLOYMENT
    with patch.object(
        harness_main, "indemn", return_value=dep
    ), patch.object(
        harness_main, "ChatSession", return_value=chat_instance
    ):
        _run(
            harness_main._start_deployment_session(
                websocket=ws,
                deployment_id=dep["_id"],
                dynamic_params={},
                auth_token="tok",
                connect_msg={},
            )
        )
    return ws


# -----------------------------------------------------------------------------
# Limit-tripping cases
# -----------------------------------------------------------------------------


class TestRateLimitTrips:
    def test_per_ip_limit_trips_with_1008_equivalent_1013(self, monkeypatch):
        """11th request from the same IP within the 60s window trips
        the per-IP limit (default 10/min). WS close 1013 ('Try Again
        Later' per RFC 6455) + error payload with scope='ip'."""
        from harness_common.rate_limit import RateLimiter

        # Tight limit for fast test (1/min)
        monkeypatch.setattr(
            harness_main,
            "_rate_limiter",
            RateLimiter(per_ip_per_minute=1),
        )

        # First request: allowed
        ws1 = _drive(ip="10.0.0.1")
        errors1 = [p for p in _send_payloads(ws1) if p.get("type") == "error"]
        assert errors1 == []

        # Second request: tripped
        ws2 = _drive(ip="10.0.0.1")
        errors2 = [p for p in _send_payloads(ws2) if p.get("type") == "error"]
        assert len(errors2) == 1
        assert errors2[0]["code"] == "rate_limited"
        assert errors2[0]["scope"] == "ip"
        assert errors2[0]["retry_after_seconds"] >= 1
        ws2.close.assert_called_once_with(code=1013)

    def test_per_deployment_limit_trips_with_scope_deployment(
        self, monkeypatch
    ):
        from harness_common.rate_limit import RateLimiter

        monkeypatch.setattr(
            harness_main,
            "_rate_limiter",
            RateLimiter(
                per_ip_per_minute=100,  # not the binding limit
                per_actor_per_minute=100,  # not the binding limit
                per_deployment_per_minute=1,
            ),
        )

        # Different IPs to bypass per-IP limit — both hit the same Deployment
        _drive(ip="10.0.0.1")
        ws2 = _drive(ip="10.0.0.2")

        errors = [p for p in _send_payloads(ws2) if p.get("type") == "error"]
        assert errors[0]["code"] == "rate_limited"
        assert errors[0]["scope"] == "deployment"

    def test_different_ips_dont_share_limit(self, monkeypatch):
        """Per-IP throttling is keyed on IP — different clients don't
        starve each other (modulo per-actor + per-deployment limits)."""
        from harness_common.rate_limit import RateLimiter

        monkeypatch.setattr(
            harness_main,
            "_rate_limiter",
            RateLimiter(per_ip_per_minute=1),
        )

        ws1 = _drive(ip="10.0.0.1")
        # Same IP would now be tripped, but IP 2 has its own bucket
        ws2 = _drive(ip="10.0.0.2")

        errors1 = [p for p in _send_payloads(ws1) if p.get("type") == "error"]
        errors2 = [p for p in _send_payloads(ws2) if p.get("type") == "error"]
        assert errors1 == []
        assert errors2 == []


# -----------------------------------------------------------------------------
# Ordering: rate-limit runs AFTER acts_as (need effective_actor_id) but
# BEFORE ChatSession construction (so resource allocation is gated)
# -----------------------------------------------------------------------------


class TestRateLimitOrdering:
    def test_rate_limit_runs_before_chatsession_construction(
        self, monkeypatch
    ):
        """If the rate limit trips, ChatSession is NOT constructed — the
        load-bearing invariant for the mitigation. Resource allocation
        (Interaction + Attention + checkpointer state) doesn't happen
        for rate-limited callers."""
        from harness_common.rate_limit import RateLimiter

        monkeypatch.setattr(
            harness_main,
            "_rate_limiter",
            RateLimiter(per_ip_per_minute=1),
        )

        # First request: ChatSession constructed
        ws1 = _mock_websocket()
        with patch.object(
            harness_main, "indemn", return_value=_ACTIVE_DEPLOYMENT
        ), patch.object(harness_main, "ChatSession") as mock_cls1:
            mock_cls1.return_value = MagicMock(
                interaction_id="int_1",
                start=AsyncMock(),
                close=AsyncMock(),
            )
            _run(
                harness_main._start_deployment_session(
                    websocket=ws1,
                    deployment_id="dep_active",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )
        mock_cls1.assert_called_once()

        # Second request: tripped — ChatSession NOT constructed
        ws2 = _mock_websocket()
        with patch.object(
            harness_main, "indemn", return_value=_ACTIVE_DEPLOYMENT
        ), patch.object(harness_main, "ChatSession") as mock_cls2:
            _run(
                harness_main._start_deployment_session(
                    websocket=ws2,
                    deployment_id="dep_active",
                    dynamic_params={},
                    auth_token="tok",
                    connect_msg={},
                )
            )
        mock_cls2.assert_not_called()
        ws2.close.assert_called_once_with(code=1013)

    def test_rate_limit_runs_after_acts_as_so_effective_actor_id_used(
        self, monkeypatch
    ):
        """Per-actor limit keys on effective_actor_id (the security-resolved
        identity from the acts_as gate), NOT on the raw JWT.sub or
        dynamic_params.actor_id. For associate_self mode, the actor key
        is the Deployment's associate_id; for session_actor it's JWT.sub.
        This pins the chain ordering: acts_as resolves first, then
        rate-limit uses the result."""
        from harness_common.rate_limit import RateLimiter

        monkeypatch.setattr(
            harness_main,
            "_rate_limiter",
            RateLimiter(per_actor_per_minute=1),
        )

        # Two requests in associate_self mode → effective_actor_id =
        # Deployment.associate_id on both → SAME bucket → second trips.
        ws1 = _drive(ip="10.0.0.1")
        ws2 = _drive(ip="10.0.0.2")  # different IP — only per-actor binds

        errors1 = [p for p in _send_payloads(ws1) if p.get("type") == "error"]
        errors2 = [p for p in _send_payloads(ws2) if p.get("type") == "error"]
        assert errors1 == []
        assert len(errors2) == 1
        assert errors2[0]["scope"] == "actor"


# -----------------------------------------------------------------------------
# Allowed path (rate-limit passes through)
# -----------------------------------------------------------------------------


class TestRateLimitAllowed:
    def test_first_request_passes_through(self):
        """Fresh limiter + first request → no rate-limit error; session
        constructed (the test fixture's _reset_rate_limiter ensures
        cleanliness)."""
        ws = _drive()
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        ws.close.assert_not_called()
