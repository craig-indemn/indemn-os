"""Shared jwt_auth: HS256 dual-mode + purpose-claim enforcement (AI-408
Phase 3 extraction).

The module lives at `harnesses/_base/harness_common/jwt_auth.py`. Tests
here cover both algorithms + the AI-407 purpose-claim hardening. Per-
frontdoor wrappers (`harnesses/voice-frontdoor/harness/jwt_auth.py`,
`harnesses/chat-deepagents/jwt_auth.py`) pin their audience constants and
re-export — their tests sit in the respective harness's `tests/` dir.
"""

import os
import time
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from harness_common.jwt_auth import (
    JWT_ISSUER,
    JWT_LEEWAY_SECONDS,
    verify_jwt,
)


# -----------------------------------------------------------------------------
# Fixtures (HS256 + RS256 key material)
# -----------------------------------------------------------------------------


@pytest.fixture
def hs256_env(monkeypatch):
    """Configure HS256 mode with a known signing secret."""
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_SIGNING_KEY", "test-secret-key-32-bytes-or-more-long")


@pytest.fixture(scope="session")
def _rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def rs256_env(monkeypatch, _rsa_keypair):
    """Configure RS256 mode + stub _get_public_key to return the test key."""
    monkeypatch.setenv("JWT_ALGORITHM", "RS256")
    public_pem = _rsa_keypair[1]
    monkeypatch.setattr(
        "harness_common.jwt_auth._get_public_key",
        lambda: public_pem,
    )


def _hs256_token(**overrides) -> str:
    """Mint an HS256-signed test token with the given claim overrides."""
    now = int(time.time())
    claims = {
        "actor_id": "act_test",
        "org_id": "org_test",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return pyjwt.encode(
        claims,
        "test-secret-key-32-bytes-or-more-long",
        algorithm="HS256",
    )


def _rs256_token(private_pem: str, **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "act_test",
        "org_id": "org_test",
        "iss": "indemn-os",
        "aud": ["runtime-voice-frontdoor"],
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return pyjwt.encode(claims, private_pem, algorithm="RS256")


# -----------------------------------------------------------------------------
# HS256 — OS-current path
# -----------------------------------------------------------------------------


class TestHS256BasicVerify:
    def test_valid_token_decodes_with_actor_id(self, hs256_env):
        """OS-shape token (actor_id + org_id + iat + exp) verifies. Audience
        parameter is ignored in HS256 mode (OS doesn't set aud)."""
        token = _hs256_token(actor_id="act_alice")
        claims = verify_jwt(token, audience="runtime-chat")
        assert claims["actor_id"] == "act_alice"
        assert claims["org_id"] == "org_test"

    def test_actor_id_normalized_to_sub(self, hs256_env):
        """Frontdoors read `claims['sub']` uniformly — actor_id is mirrored
        onto sub when sub is absent (the OS-current claim shape)."""
        token = _hs256_token(actor_id="act_alice")
        claims = verify_jwt(token, audience="ignored")
        assert claims["sub"] == "act_alice"

    def test_explicit_sub_preserved(self, hs256_env):
        """When both actor_id AND sub are present, sub wins (no overwrite)."""
        token = _hs256_token(actor_id="act_alice", sub="act_explicit")
        claims = verify_jwt(token, audience="ignored")
        assert claims["sub"] == "act_explicit"

    def test_expired_raises_ExpiredSignatureError(self, hs256_env):
        token = _hs256_token(exp=int(time.time()) - 3600)
        with pytest.raises(pyjwt.ExpiredSignatureError):
            verify_jwt(token, audience="ignored")

    def test_bad_signature_raises(self, hs256_env, monkeypatch):
        token = _hs256_token()
        # Verifier uses a different signing key than the minter
        monkeypatch.setenv("JWT_SIGNING_KEY", "different-key-than-mint")
        with pytest.raises(pyjwt.InvalidSignatureError):
            verify_jwt(token, audience="ignored")

    def test_clock_skew_leeway(self, hs256_env):
        """60s leeway covers clock-skew between OS API + frontdoor."""
        # Just-expired token (within leeway) still validates
        token = _hs256_token(exp=int(time.time()) - 30)
        claims = verify_jwt(token, audience="ignored")
        assert claims["actor_id"] == "act_test"


class TestHS256PurposeClaim:
    """AI-407 pre-merge security fix: enforce the `purpose` claim. The OS
    kernel signs MULTIPLE token kinds with the same JWT_SIGNING_KEY
    (access tokens have no purpose; mfa_challenge / password_reset /
    magic_link tokens have specific purposes). Without a purpose gate the
    frontdoor would accept any of them as a session credential."""

    def test_no_purpose_claim_accepted(self, hs256_env):
        """Canonical OS access token (no purpose claim) → accepted."""
        token = _hs256_token()  # no purpose
        claims = verify_jwt(token, audience="ignored")
        assert "purpose" not in claims

    def test_purpose_session_accepted(self, hs256_env):
        """Forward-compat: if OS adds purpose='session' to access tokens
        later, they still validate."""
        token = _hs256_token(purpose="session")
        verify_jwt(token, audience="ignored")  # no raise

    def test_purpose_access_accepted(self, hs256_env):
        """Forward-compat: purpose='access' synonym is also accepted."""
        token = _hs256_token(purpose="access")
        verify_jwt(token, audience="ignored")  # no raise

    @pytest.mark.parametrize(
        "purpose",
        ["mfa_challenge", "password_reset", "email_verify", "magic_link"],
    )
    def test_non_session_purpose_rejected(self, hs256_env, purpose):
        """Any other purpose value → reject with InvalidTokenError. Even
        if the signature + expiry are valid, the token wasn't minted for
        a session."""
        token = _hs256_token(purpose=purpose)
        with pytest.raises(pyjwt.InvalidTokenError) as exc:
            verify_jwt(token, audience="ignored")
        assert purpose in str(exc.value)


# -----------------------------------------------------------------------------
# RS256 — forward design path
# -----------------------------------------------------------------------------


class TestRS256AudiencePinning:
    """RS256 mode enforces iss + aud. Audience is caller-supplied so each
    frontdoor pins its own surface — a JWT for runtime-chat MUST NOT
    validate against runtime-voice-frontdoor and vice versa."""

    def test_valid_token_with_matching_audience(self, rs256_env, _rsa_keypair):
        token = _rs256_token(_rsa_keypair[0], aud=["runtime-chat"])
        claims = verify_jwt(token, audience="runtime-chat")
        assert claims["sub"] == "act_test"

    def test_wrong_audience_rejected(self, rs256_env, _rsa_keypair):
        """The headline cross-runtime-token-reuse defense — a JWT minted
        for voice MUST NOT validate against chat."""
        token = _rs256_token(_rsa_keypair[0], aud=["runtime-voice-frontdoor"])
        with pytest.raises(pyjwt.InvalidAudienceError):
            verify_jwt(token, audience="runtime-chat")

    def test_wrong_issuer_rejected(self, rs256_env, _rsa_keypair):
        token = _rs256_token(
            _rsa_keypair[0],
            iss="rogue-issuer",
            aud=["runtime-chat"],
        )
        with pytest.raises(pyjwt.InvalidIssuerError):
            verify_jwt(token, audience="runtime-chat")

    def test_expired_rejected(self, rs256_env, _rsa_keypair):
        token = _rs256_token(
            _rsa_keypair[0],
            aud=["runtime-chat"],
            exp=int(time.time()) - 3600,
        )
        with pytest.raises(pyjwt.ExpiredSignatureError):
            verify_jwt(token, audience="runtime-chat")

    def test_audience_list_member_accepted(self, rs256_env, _rsa_keypair):
        """JWTs may have multiple audiences; matching any one is sufficient."""
        token = _rs256_token(
            _rsa_keypair[0],
            aud=["runtime-chat", "runtime-voice-frontdoor"],
        )
        verify_jwt(token, audience="runtime-chat")  # no raise
        verify_jwt(token, audience="runtime-voice-frontdoor")  # no raise


class TestAlgorithmDefault:
    def test_unset_jwt_algorithm_defaults_to_rs256(
        self, monkeypatch, _rsa_keypair
    ):
        """Per the design forward path — no env = RS256."""
        monkeypatch.delenv("JWT_ALGORITHM", raising=False)
        monkeypatch.setattr(
            "harness_common.jwt_auth._get_public_key",
            lambda: _rsa_keypair[1],
        )
        token = _rs256_token(_rsa_keypair[0], aud=["runtime-chat"])
        claims = verify_jwt(token, audience="runtime-chat")
        assert claims["sub"] == "act_test"


# -----------------------------------------------------------------------------
# Module constants — pin to lock the public surface
# -----------------------------------------------------------------------------


class TestModuleConstants:
    def test_issuer_pinned(self):
        assert JWT_ISSUER == "indemn-os"

    def test_leeway_60s(self):
        assert JWT_LEEWAY_SECONDS == 60
