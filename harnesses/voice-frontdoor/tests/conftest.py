"""Pytest config + shared fixtures for voice-frontdoor tests.

Covers:
1. sys.path: `harness/` is a real package locally (matches Dockerfile COPY);
   add the voice-frontdoor dir + harnesses/_base to sys.path.
2. RSA-2048 keypair (session-scoped) — pairs with stubbed _get_public_key
   so the frontdoor's JWT validator (Task 2.28) verifies test tokens
   against a known public key.
3. JWT factories — valid_jwt + jwt_for_actor + expired_jwt for Tasks 2.28
   + 2.32.5 + 2.34 + 2.35.
4. Deployment fixtures — valid_deployment (session_actor), variants for
   Origin tests.
5. Interaction fixtures — existing_interaction + expired_interaction for
   resume-flow tests (Task 2.35).
6. LiveKit mocks — mock_livekit (Path B mock fallback per Track 9);
   livekit_test_instance (Path A real instance, auto-skipped if
   LIVEKIT_URL absent).
7. JWT TestClient (client_with_jwt) — for chat WS tests (Task 3.4).
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

VOICE_FRONTDOOR_DIR = Path(__file__).resolve().parents[1]
HARNESSES_BASE_DIR = VOICE_FRONTDOOR_DIR.parent / "_base"

if str(VOICE_FRONTDOOR_DIR) not in sys.path:
    sys.path.insert(0, str(VOICE_FRONTDOOR_DIR))
if str(HARNESSES_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESSES_BASE_DIR))


# ----------------------------------------------------------------------------
# RSA keypair (session-scoped; generated once per pytest run)
# ----------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _test_rsa_keypair():
    """Generate a fresh RSA-2048 keypair for the test session.
    Production frontdoor reads private from a secret signer + public from
    AWS Secrets `indemn/dev/shared/jwt-public-key`; tests use this ephemeral
    pair paired with the autouse stub below."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


@pytest.fixture(scope="session")
def _test_private_key(_test_rsa_keypair):
    """Test RSA private PEM (PKCS8). Used by JWT factories to sign tokens."""
    return _test_rsa_keypair[0]


@pytest.fixture(scope="session")
def _test_public_key(_test_rsa_keypair):
    """Test RSA public PEM. Stubs the value the frontdoor's _get_public_key
    would return from AWS Secrets in production."""
    return _test_rsa_keypair[1]


@pytest.fixture(autouse=True)
def _stub_jwt_public_key(_test_public_key, monkeypatch):
    """Autouse: replace `harness.jwt_auth._get_public_key` with a function
    returning the session-scoped test public key.

    Avoids the AWS Secrets Manager call during tests + pairs the verifier
    with the test private key so JWTs minted by `valid_jwt` / `expired_jwt`
    factories validate correctly. lru_cache on _get_public_key is bypassed
    because monkeypatch replaces the bound function entirely.

    Idempotent if harness.jwt_auth hasn't been imported yet — try/except
    keeps the fixture safe in the brief window before Task 2.28 lands.
    """
    try:
        monkeypatch.setattr(
            "harness.jwt_auth._get_public_key",
            lambda: _test_public_key,
        )
    except (ModuleNotFoundError, AttributeError):
        # jwt_auth module / symbol not present — tests that don't need it
        # still run; tests that do will fail loudly when verify_jwt is hit.
        pass


# ----------------------------------------------------------------------------
# JWT factories
# ----------------------------------------------------------------------------


@pytest.fixture
def valid_jwt(_test_private_key):
    """Factory: valid_jwt(actor_id, **claim_overrides) → signed JWT string.

    Per §10.3.1 JWT specifics: RS256; required claims sub + org_id + exp +
    iss + aud. Defaults to claims a real frontdoor would accept.
    """
    import jwt as pyjwt

    def _mint(actor_id: str = "act_test", **overrides) -> str:
        now = int(time.time())
        claims = {
            "sub": actor_id,
            "org_id": overrides.pop("org_id", "org_test"),
            "iss": overrides.pop("iss", "indemn-os"),
            "aud": overrides.pop("aud", ["runtime-voice-frontdoor"]),
            "iat": overrides.pop("iat", now),
            "exp": overrides.pop("exp", now + 3600),  # 1h expiry
        }
        claims.update(overrides)
        return pyjwt.encode(claims, _test_private_key, algorithm="RS256")

    return _mint


@pytest.fixture
def jwt_for_actor(valid_jwt):
    """Alias of valid_jwt — used in Tasks 2.28 + 2.31 tests where the
    explicit "for actor X" framing reads better than "valid JWT"."""
    return valid_jwt


@pytest.fixture
def expired_jwt(_test_private_key):
    """Factory: expired_jwt(actor_id) → expired JWT string (exp in past).

    Used by Task 2.28 to verify JWT validation rejects expired tokens.
    """
    import jwt as pyjwt

    def _mint(actor_id: str = "act_test") -> str:
        now = int(time.time())
        claims = {
            "sub": actor_id,
            "org_id": "org_test",
            "iss": "indemn-os",
            "aud": ["runtime-voice-frontdoor"],
            "iat": now - 7200,
            "exp": now - 3600,  # expired 1h ago
        }
        return pyjwt.encode(claims, _test_private_key, algorithm="RS256")

    return _mint


# ----------------------------------------------------------------------------
# Deployment fixtures
# ----------------------------------------------------------------------------


@pytest.fixture
def valid_deployment():
    """Deployment with acts_as=session_actor + parameter_schema requiring
    actor_id. Exercises the JWT-validation path (Task 2.28 + 2.31).
    Includes allowed_origins so Origin check passes."""
    return {
        "_id": "dep_valid",
        "name": "Valid Test Deployment",
        "associate_id": "act_associate",
        "runtime_id": "rt_voice",
        "acts_as": "session_actor",
        "status": "active",
        "allowed_origins": ["https://sales.indemn.ai"],
        "parameter_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["actor_id"],
            "properties": {
                "actor_id": {
                    "type": "string",
                    "pattern": "^[0-9a-zA-Z_]+$",
                },
                "current_route": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "static_parameters": {"role": "sales", "tenant": "indemn-internal"},
        "greeting": "Hi, this is your test assistant.",
        "resumption_config": {"ttl_seconds": 86400, "kill_on_resume": True},
    }


@pytest.fixture
def deployment_with_allowed_origins(valid_deployment):
    """Alias — a Deployment with a non-empty allowed_origins list. Used by
    test_origin_validation.py tests that need Origin check to pass for one
    specific origin."""
    return valid_deployment


@pytest.fixture
def deployment_with_no_origins(valid_deployment):
    """Track 13f — a Deployment with allowed_origins=[] (the spec says
    'empty list = reject all'). Same as valid_deployment but with empty
    allowed_origins."""
    return {**valid_deployment, "allowed_origins": []}


@pytest.fixture
def paused_deployment(valid_deployment):
    """Deployment in `paused` status — used by Task 2.29 to verify 409
    deployment_not_active."""
    return {**valid_deployment, "status": "paused"}


# ----------------------------------------------------------------------------
# Interaction fixtures (for resume-flow tests — Task 2.35)
# ----------------------------------------------------------------------------


@pytest.fixture
def existing_interaction(valid_deployment):
    """A fresh Interaction (created ~minutes ago) — eligible for resume
    per Deployment.resumption_config.ttl_seconds."""
    now = time.time()
    return {
        "_id": "int_existing",
        "deployment_id": valid_deployment["_id"],
        "channel_type": "voice",
        "created_by": "act_test",
        "handling_actor_id": valid_deployment["associate_id"],
        "correlation_id": "cor_existing",
        "status": "active",
        "created_at": now - 60,  # 1 minute ago
    }


@pytest.fixture
def expired_interaction(valid_deployment):
    """An Interaction with created_at older than ttl_seconds — Task 2.35
    surfaces as 410 resume_expired."""
    now = time.time()
    return {
        "_id": "int_expired",
        "deployment_id": valid_deployment["_id"],
        "channel_type": "voice",
        "created_by": "act_test",
        "handling_actor_id": valid_deployment["associate_id"],
        "correlation_id": "cor_expired",
        "status": "active",
        "created_at": now - (48 * 3600),  # 48h ago — past default 24h TTL
    }


# ----------------------------------------------------------------------------
# LiveKit mocks (Path B — preferred for fast unit tests)
# ----------------------------------------------------------------------------


@pytest.fixture
def mock_livekit():
    """MagicMock(s) for LiveKit API surfaces — Task 2.33 will use this to
    capture CreateRoomRequest calls + AgentDispatch calls.

    Returns an object with `room` + `agent_dispatch` attributes for tests
    to assert against.
    """
    livekit = MagicMock()
    livekit.room = MagicMock()
    livekit.agent_dispatch = MagicMock()
    return livekit


@pytest.fixture
def livekit_test_instance():
    """Path A real LiveKit instance — auto-skipped if LIVEKIT_URL absent
    (Tracks 9 + 13b). Used by Task 2.32.5 + Task 2.38 for E2E tests
    against the actual self-hosted LiveKit on AWS GPU."""
    import os

    if not os.environ.get("LIVEKIT_URL"):
        pytest.skip("LIVEKIT_URL not set — Path A real-LiveKit fixtures skipped")
    # Returns config the test can use to construct a real client
    return {
        "url": os.environ["LIVEKIT_URL"],
        "api_key": os.environ.get("LIVEKIT_API_KEY", ""),
        "api_secret": os.environ.get("LIVEKIT_API_SECRET", ""),
    }


# ----------------------------------------------------------------------------
# JWT TestClient (Task 3.4 chat-deepagents tests will use this)
# ----------------------------------------------------------------------------


@pytest.fixture
def client_with_jwt(valid_jwt):
    """TestClient + a default Authorization header with a valid JWT for
    act_test. Tests can override by passing headers={"Authorization": ...}
    on individual requests."""
    from starlette.testclient import TestClient
    from harness.app import app

    client = TestClient(app, headers={"Authorization": f"Bearer {valid_jwt('act_test')}"})
    return client
