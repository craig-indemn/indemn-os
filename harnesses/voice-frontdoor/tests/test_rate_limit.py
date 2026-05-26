"""Rate limiter tests (AI-407 Task 2.36 / §10.7).

Two layers:
- Helper-level unit tests against RateLimiter directly
- Integration tests at the /sessions HTTP boundary verifying 429 surfaces
  with retry_after_seconds + scope per §10.3.1 status table
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestRateLimiterUnit:
    def test_per_ip_under_limit_allowed(self):
        from harness.rate_limit import RateLimiter

        rl = RateLimiter(per_ip_per_minute=10)
        for _ in range(10):
            assert rl.check_ip("1.2.3.4") is True

    def test_per_ip_over_limit_blocked(self):
        from harness.rate_limit import RateLimiter

        rl = RateLimiter(per_ip_per_minute=10)
        for _ in range(10):
            rl.check_ip("1.2.3.4")
        # 11th call rejected
        assert rl.check_ip("1.2.3.4") is False

    def test_per_actor_over_limit_blocked(self):
        from harness.rate_limit import RateLimiter

        rl = RateLimiter(per_actor_per_minute=30)
        for _ in range(30):
            rl.check_actor("act_abc")
        assert rl.check_actor("act_abc") is False

    def test_per_deployment_over_limit_blocked(self):
        from harness.rate_limit import RateLimiter

        rl = RateLimiter(per_deployment_per_minute=100)
        for _ in range(100):
            rl.check_deployment("dep_xyz")
        assert rl.check_deployment("dep_xyz") is False

    def test_per_ip_isolated_across_ips(self):
        """A noisy IP must not affect a quiet IP — separate buckets."""
        from harness.rate_limit import RateLimiter

        rl = RateLimiter(per_ip_per_minute=5)
        for _ in range(5):
            rl.check_ip("1.2.3.4")
        # noisy IP blocked
        assert rl.check_ip("1.2.3.4") is False
        # quiet IP still allowed
        assert rl.check_ip("9.9.9.9") is True

    def test_check_with_retry_returns_scope_on_block(self):
        """When a check trips, the response identifies which scope —
        SDK can back off the right key."""
        from harness.rate_limit import RateLimiter

        rl = RateLimiter(per_ip_per_minute=2)
        rl.check_ip("1.2.3.4")
        rl.check_ip("1.2.3.4")
        result = rl.check_with_retry(
            "1.2.3.4", actor="act_x", deployment="dep_x"
        )
        assert result["allowed"] is False
        assert result["scope"] == "ip"
        assert result["retry_after_seconds"] > 0
        assert result["retry_after_seconds"] <= 60

    def test_check_with_retry_allowed_records_all_counters(self):
        """An allowed call records the hit on ALL three counters so
        subsequent requests can trip any of the limits."""
        from harness.rate_limit import RateLimiter

        rl = RateLimiter(
            per_ip_per_minute=100,
            per_actor_per_minute=2,
            per_deployment_per_minute=100,
        )
        # 2 allowed (per_actor=2)
        r1 = rl.check_with_retry("1.1.1.1", actor="act_x", deployment="dep_x")
        r2 = rl.check_with_retry("1.1.1.1", actor="act_x", deployment="dep_x")
        assert r1["allowed"] is True
        assert r2["allowed"] is True
        # 3rd trips the actor limit even though IP + deployment have room
        r3 = rl.check_with_retry("1.1.1.1", actor="act_x", deployment="dep_x")
        assert r3["allowed"] is False
        assert r3["scope"] == "actor"


# ----------------------------------------------------------------------------
# Integration — /sessions returns 429 with retry_after_seconds per §10.3.1
# ----------------------------------------------------------------------------


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


def _stub_deployment(deployment_id="dep_test"):
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


def _post(client, deployment_id, token):
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


class TestRateLimitIntegration:
    def test_429_returned_after_per_ip_limit(self, client, jwt_for_actor):
        """After the per-IP limit is exceeded, /sessions returns 429
        with the §10.3.1 shape: error=rate_limited, retry_after_seconds,
        scope."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment()
        # Replace the module-level limiter with a tight one so the test
        # doesn't burn the prod default of 10 req/min
        from harness.rate_limit import RateLimiter

        tight_limiter = RateLimiter(per_ip_per_minute=2)
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch("harness.sessions._rate_limiter", tight_limiter):
            r1 = _post(client, deployment["_id"], token)
            r2 = _post(client, deployment["_id"], token)
            r3 = _post(client, deployment["_id"], token)

        # First two succeed (under limit)
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Third 429
        assert r3.status_code == 429
        body = r3.json()
        assert body["error"] == "rate_limited"
        assert body["retry_after_seconds"] > 0
        assert body.get("scope") == "ip"
        # Retry-After header per HTTP spec — SDK + browsers both honor
        assert r3.headers.get("Retry-After") == str(body["retry_after_seconds"])

    def test_rate_limit_blocks_before_livekit_dispatch(
        self, client, jwt_for_actor
    ):
        """LOAD-BEARING ORDERING per §10.7: rate-limit must fire BEFORE
        _create_lk_room_and_dispatch is called. Otherwise an attacker
        exhausts LiveKit room slots before being throttled."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment()

        from harness.rate_limit import RateLimiter

        tight_limiter = RateLimiter(per_ip_per_minute=0)  # block ALL requests
        dispatch_mock = AsyncMock()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._rate_limiter",
            tight_limiter,
        ), patch(
            "harness.sessions._create_lk_room_and_dispatch",
            new=dispatch_mock,
        ):
            response = _post(client, deployment["_id"], token)

        assert response.status_code == 429
        # The LiveKit dispatch was NEVER called — rate-limit fired first
        assert not dispatch_mock.called

    def test_rate_limit_blocks_before_interaction_creation(
        self, client, jwt_for_actor
    ):
        """Same ordering invariant — Interaction creation must NOT fire
        when rate-limited; otherwise audit-trail records leak before
        the attacker is throttled."""
        token = jwt_for_actor("act_alice")
        deployment = _stub_deployment()

        from harness.rate_limit import RateLimiter

        tight_limiter = RateLimiter(per_ip_per_minute=0)
        create_mock = AsyncMock()
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=deployment),
        ), patch(
            "harness.sessions._rate_limiter",
            tight_limiter,
        ), patch(
            "harness.sessions._create_interaction",
            new=create_mock,
        ):
            response = _post(client, deployment["_id"], token)

        assert response.status_code == 429
        assert not create_mock.called
