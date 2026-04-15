"""Integration tests for Phase 4 auth flows."""

import pytest

from bson import ObjectId

from kernel.auth.audit import write_auth_event
from kernel.auth.jwt import (
    create_access_token,
    revoke_in_cache,
    verify_access_token,
)
from kernel.auth.password import hash_password, verify_password
from kernel.auth.rate_limit import (
    RATE_LIMIT_COLLECTION,
    check_rate_limit,
    record_failed_attempt,
)
from kernel.auth.session_manager import create_session, revoke_all_sessions
from kernel.changes.collection import ChangeRecord
from kernel.config import settings
from kernel_entities.actor import Actor
from kernel_entities.session import Session

settings.jwt_signing_key = "test-secret-key-for-integration-tests"


class TestAuthAuditIntegration:
    """Test auth audit events are written to changes collection."""

    async def test_write_auth_event(self, db, org_id, actor):
        await write_auth_event(actor, "auth.login_success", {"ip": "127.0.0.1"})

        records = await ChangeRecord.find({
            "org_id": org_id,
            "change_type": "auth.login_success",
        }).to_list()
        assert len(records) == 1
        assert records[0].method == "auth.login_success"
        assert records[0].method_metadata["ip"] == "127.0.0.1"
        assert records[0].current_hash is not None

    async def test_multiple_events_chain(self, db, org_id, actor):
        await write_auth_event(actor, "auth.login_attempt", {})
        await write_auth_event(actor, "auth.login_success", {})

        records = await ChangeRecord.find({
            "org_id": org_id,
            "change_type": {"$regex": "^auth\\."},
        }).sort("timestamp").to_list()
        assert len(records) == 2
        # Second record should reference first's hash
        assert records[1].previous_hash is not None


class TestRateLimitIntegration:
    """Test rate limiting with real MongoDB."""

    async def test_no_limit_initially(self, db, org_id):
        # Clean collection first to ensure no stale data
        await db[RATE_LIMIT_COLLECTION].delete_many({})
        result = await check_rate_limit("10.0.0.1", "fresh@test.com", org_id)
        assert result is False

    async def test_limit_after_failures(self, db, org_id):
        await db[RATE_LIMIT_COLLECTION].delete_many({})
        # Record 5 failures
        for _ in range(5):
            await record_failed_attempt("10.0.0.2", "limit@test.com")

        # Should be rate limited
        result = await check_rate_limit("10.0.0.2", "limit@test.com", org_id)
        assert result is True

    async def test_different_ip_not_limited(self, db, org_id):
        await db[RATE_LIMIT_COLLECTION].delete_many({})
        for _ in range(5):
            await record_failed_attempt("10.0.0.3", "multi@test.com")

        # Different IP should not be limited
        result = await check_rate_limit("10.0.0.4", "multi@test.com", org_id)
        assert result is False


class TestSessionLifecycle:
    """Test session creation and revocation."""

    async def test_create_session(self, db, org_id, actor):
        session, token = await create_session(actor, "password")
        assert session.status == "active"
        assert session.auth_method_used == "password"
        assert token is not None

        # Token should be valid
        payload = verify_access_token(token)
        assert payload["actor_id"] == str(actor.id)

    async def test_revoke_all_sessions(self, db, org_id, actor):
        s1, _ = await create_session(actor, "password")
        s2, _ = await create_session(actor, "sso:google")

        await revoke_all_sessions(actor.id)

        s1_reloaded = await Session.get(s1.id)
        s2_reloaded = await Session.get(s2.id)
        assert s1_reloaded.status == "revoked"
        assert s2_reloaded.status == "revoked"


class TestRevocationCache:
    """Test revocation cache with real JWTs."""

    async def test_revoked_token_rejected(self, db, org_id, actor):
        session, token = await create_session(actor, "password")
        payload = verify_access_token(token)
        assert payload is not None

        # Revoke in cache
        revoke_in_cache(session.access_token_jti)

        with pytest.raises(Exception):
            verify_access_token(token)
