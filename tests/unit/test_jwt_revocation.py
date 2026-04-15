"""Unit tests for JWT revocation cache and partial tokens."""

import time

import pytest

from kernel.auth.jwt import (
    _revocation_cache,
    create_access_token,
    create_partial_token,
    generate_magic_link_token,
    revoke_in_cache,
    verify_access_token,
    verify_magic_link_token,
    verify_partial_token,
)
from kernel.config import settings

# Ensure a signing key is available for tests
settings.jwt_signing_key = "test-secret-key-for-unit-tests"


class TestRevocationCache:
    def setup_method(self):
        _revocation_cache.clear()

    def test_revoke_in_cache_blocks_token(self):
        token, jti = create_access_token("actor1", "org1", ["admin"])
        # Token should verify before revocation
        payload = verify_access_token(token)
        assert payload["actor_id"] == "actor1"

        # Revoke
        revoke_in_cache(jti)

        # Token should now fail
        with pytest.raises(Exception):
            verify_access_token(token)

    def test_cache_eviction(self):
        # Add an entry with old timestamp
        _revocation_cache["old-jti"] = time.time() - 10000
        # Verifying any token triggers eviction
        token, _ = create_access_token("actor1", "org1", ["admin"])
        verify_access_token(token)
        assert "old-jti" not in _revocation_cache


class TestPartialToken:
    def test_create_and_verify(self):
        from types import SimpleNamespace

        actor = SimpleNamespace(id="actor123")
        session = SimpleNamespace(id="session456")
        token = create_partial_token(actor, session)
        payload = verify_partial_token(token)
        assert payload["actor_id"] == "actor123"
        assert payload["session_id"] == "session456"
        assert payload["purpose"] == "mfa_challenge"

    def test_reject_non_partial_token(self):
        token, _ = create_access_token("actor1", "org1", ["admin"])
        with pytest.raises(Exception, match="Not a partial token"):
            verify_partial_token(token)


class TestMagicLinkToken:
    def test_create_and_verify(self):
        from types import SimpleNamespace

        actor = SimpleNamespace(id="actor789")
        token = generate_magic_link_token(actor, purpose="password_reset")
        payload = verify_magic_link_token(token, purpose="password_reset")
        assert payload["actor_id"] == "actor789"
        assert payload["purpose"] == "password_reset"

    def test_wrong_purpose_rejected(self):
        from types import SimpleNamespace

        actor = SimpleNamespace(id="actor789")
        token = generate_magic_link_token(actor, purpose="password_reset")
        with pytest.raises(Exception, match="purpose mismatch"):
            verify_magic_link_token(token, purpose="email_verify")
