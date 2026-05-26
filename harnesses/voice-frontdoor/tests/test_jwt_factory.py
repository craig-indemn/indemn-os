"""Voice-frontdoor fixture smoke (AI-407 §10.6 + Phase 2B test dependencies).

Task 2.27.5 — verify the conftest fixtures Tasks 2.28 / 2.32.5 / 2.34 / 2.35
will consume work correctly.

Fixtures pinned:
- _test_private_key / _test_public_key: session-scoped RSA-2048 keypair
- valid_jwt: factory that mints a valid JWT for any actor_id
- jwt_for_actor: alias of valid_jwt with explicit sub claim
- expired_jwt: factory minting an expired token
- mock_livekit: MagicMock(s) for LiveKit API interactions
- valid_deployment: Deployment fixture (acts_as=session_actor, requires
  actor_id in parameter_schema)
"""

import pytest


def test_test_private_key_is_rsa(_test_private_key):
    """The test private key is a valid RSA PEM."""
    assert (
        "BEGIN RSA PRIVATE KEY" in _test_private_key
        or "BEGIN PRIVATE KEY" in _test_private_key
    )


def test_test_public_key_is_rsa(_test_public_key):
    """The test public key is a valid RSA PEM."""
    assert (
        "BEGIN PUBLIC KEY" in _test_public_key
        or "BEGIN RSA PUBLIC KEY" in _test_public_key
    )


def test_valid_jwt_returns_string(valid_jwt):
    """The valid_jwt factory mints a JWT string for an arbitrary actor."""
    token = valid_jwt("act_test")
    assert isinstance(token, str)
    assert len(token.split(".")) == 3  # JWT has 3 segments


def test_jwt_for_actor_pins_sub(jwt_for_actor):
    """jwt_for_actor mints a token with sub = the given actor_id."""
    import jwt as pyjwt

    token = jwt_for_actor("act_alice")
    # Decode WITHOUT verification just to check the claims
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == "act_alice"


def test_jwt_includes_required_claims(valid_jwt):
    """Per §10.3.1 JWT specifics: required claims are sub, org_id, exp,
    iss, aud. Verify the factory includes them all."""
    import jwt as pyjwt

    token = valid_jwt("act_test")
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == "act_test"
    assert "org_id" in claims
    assert "exp" in claims
    assert claims.get("iss") == "indemn-os"
    assert "runtime-voice-frontdoor" in (claims.get("aud") or [])


def test_expired_jwt_is_expired(expired_jwt):
    """expired_jwt has an exp claim in the past."""
    import time

    import jwt as pyjwt

    token = expired_jwt("act_test")
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] < time.time()


def test_mock_livekit_provides_room_and_dispatch(mock_livekit):
    """mock_livekit exposes room + agent_dispatch attributes for tests
    to assert against."""
    assert hasattr(mock_livekit, "room")
    assert hasattr(mock_livekit, "agent_dispatch")


def test_valid_deployment_has_session_actor(valid_deployment):
    """valid_deployment is a Deployment with acts_as=session_actor +
    parameter_schema requiring actor_id (exercises the JWT-validation
    path that Tasks 2.28 + 2.31 will fill)."""
    assert valid_deployment["acts_as"] == "session_actor"
    parameter_schema = valid_deployment["parameter_schema"]
    assert "actor_id" in parameter_schema.get("required", [])


def test_deployment_with_allowed_origins_default(deployment_with_allowed_origins):
    """deployment_with_allowed_origins is a Deployment with a non-empty
    allowed_origins list — for tests that need Origin check to pass."""
    assert deployment_with_allowed_origins["allowed_origins"]


def test_deployment_with_no_origins_is_empty(deployment_with_no_origins):
    """deployment_with_no_origins is a Deployment with allowed_origins=[]
    — for Track 13f test that empty list rejects all."""
    assert deployment_with_no_origins["allowed_origins"] == []


def test_existing_interaction_has_required_fields(existing_interaction):
    """existing_interaction is a fresh Interaction for resume-flow tests
    (Task 2.35) — must have _id, deployment_id, created_by, correlation_id,
    status, created_at."""
    for key in (
        "_id",
        "deployment_id",
        "created_by",
        "correlation_id",
        "status",
        "created_at",
    ):
        assert key in existing_interaction, f"missing {key}"


def test_expired_interaction_is_past_ttl(expired_interaction):
    """expired_interaction has a created_at older than the default
    resumption_config.ttl_seconds — Task 2.35 will surface as 410
    resume_expired."""
    import time

    age_seconds = time.time() - expired_interaction["created_at"]
    # Default ttl_seconds=86400 (24h); fixture creates expired_interaction
    # with created_at ~48h ago for safety margin
    assert age_seconds > 86400
