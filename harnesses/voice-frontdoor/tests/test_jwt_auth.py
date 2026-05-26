"""POST /sessions JWT validation (AI-407 Task 2.28; §10.3.1 step 3 + §10.6).

Authorization: Bearer <token> on every /sessions request. The frontdoor
verifies signature locally with the public key from AWS Secrets at
`indemn/dev/shared/jwt-public-key` (stubbed via conftest's autouse
`_stub_jwt_public_key` fixture so tests pair with the session-scoped RSA
keypair without hitting AWS).

JWT contract per §10.6:
- Algorithm: RS256
- Required claims: sub, org_id, exp, iss == "indemn-os",
  aud contains "runtime-voice-frontdoor"
- 60-second clock-skew tolerance

Error response shape per §10.3.1 table:
- 401 unauthorized → {"error": "unauthorized",
  "reason": "missing|invalid|expired|wrong_audience|wrong_issuer"}

Track 16 conftest deviation (resolved here): playbook tests reference a
module-level `_TEST_PRIVATE_KEY` constant; conftest exposes the
session-scoped `_test_private_key` fixture instead. Tests pull the key
via the fixture (same shape, just different access pattern).
"""

import time
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


def _stub_deployment(deployment_id="dep_valid"):
    """Minimal Deployment dict permitting Origin check to pass so the
    JWT validation step is reached. Subsequent tasks (2.29-2.31) add
    status / parameter_schema / acts_as / etc."""
    return {
        "_id": deployment_id,
        "name": "Test Deployment",
        "allowed_origins": ["https://sales.indemn.ai"],
        "status": "active",
    }


def _post_sessions(client, deployment_id, headers=None):
    """Helper: POST /sessions with the common body shape + given headers."""
    base = {"Origin": "https://sales.indemn.ai"}
    if headers:
        base.update(headers)
    return client.post(
        "/sessions",
        json={
            "deployment_id": deployment_id,
            "dynamic_params": {"actor_id": "act_abc"},
        },
        headers=base,
    )


class TestJWTAuth:
    def test_missing_authorization_returns_401(self, client, valid_deployment):
        """No Authorization header → 401 reason=missing.

        Pins the cheapest-to-detect case first: a request without any
        token at all. Distinguishes from invalid (malformed header)
        and expired (well-formed but past exp)."""
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response = _post_sessions(client, valid_deployment["_id"])

        assert response.status_code == 401
        body = response.json()
        assert body["error"] == "unauthorized"
        assert body["reason"] == "missing"

    def test_invalid_signature_returns_401(self, client, valid_deployment):
        """Malformed / unsigned token → 401 reason=invalid.

        `not.a.real.jwt` has 3 segments so pyjwt won't reject on shape;
        the signature check fails and falls through to the generic
        invalid branch."""
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": "Bearer not.a.real.jwt"},
            )

        assert response.status_code == 401
        body = response.json()
        assert body["error"] == "unauthorized"
        assert body["reason"] == "invalid"

    def test_expired_jwt_returns_401(
        self, client, valid_deployment, expired_jwt
    ):
        """Token past `exp` (outside 60s leeway) → 401 reason=expired.

        Uses the conftest `expired_jwt` factory which mints a token with
        exp 1h in the past — well past the 60s leeway window."""
        token = expired_jwt("act_test")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 401
        assert response.json()["reason"] == "expired"

    def test_wrong_audience_rejected(
        self, client, valid_deployment, _test_private_key
    ):
        """JWT with aud != 'runtime-voice-frontdoor' → 401.

        §10.6 requires the JWT's `aud` claim to include
        `runtime-voice-frontdoor`. A token minted for `runtime-chat` (or
        any other audience) MUST NOT be accepted by the voice frontdoor
        — otherwise a token leaked from a less-sensitive surface could
        grant voice-session creation. pyjwt raises InvalidAudienceError
        on `aud` mismatch; the reason may be the generic 'invalid' or
        the more specific 'wrong_audience' — accept either."""
        payload = {
            "sub": "act_test",
            "org_id": "org_test",
            "iss": "indemn-os",
            "aud": ["runtime-chat"],  # WRONG audience
            "exp": int(time.time()) + 60,
        }
        token = pyjwt.encode(payload, _test_private_key, algorithm="RS256")

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 401
        body = response.json()
        assert body["error"] == "unauthorized"
        assert body["reason"] in ("invalid", "wrong_audience")

    def test_wrong_issuer_rejected(
        self, client, valid_deployment, _test_private_key
    ):
        """JWT with iss != 'indemn-os' → 401.

        §10.6 requires the JWT's `iss` claim to equal `indemn-os`.
        Tokens minted by any other issuer (rogue service, forgotten old
        IdP) MUST NOT be accepted. Symmetric to wrong_audience but for
        issuer."""
        payload = {
            "sub": "act_test",
            "org_id": "org_test",
            "iss": "rogue-issuer",  # WRONG issuer
            "aud": ["runtime-voice-frontdoor"],
            "exp": int(time.time()) + 60,
        }
        token = pyjwt.encode(payload, _test_private_key, algorithm="RS256")

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 401
        body = response.json()
        assert body["error"] == "unauthorized"
        assert body["reason"] in ("invalid", "wrong_issuer")

    def test_jwt_within_60s_clock_skew_accepted(
        self, client, valid_deployment, _test_private_key
    ):
        """§10.6 specifies 60s clock-skew tolerance via pyjwt `leeway=60`.

        A token expired 30s ago is within tolerance — JWT validation
        should accept it (downstream checks may still fail, but NOT for
        reason=expired). A token expired 90s ago is outside tolerance —
        rejected with reason=expired. Pins the exact 60s contract from
        §10.3.1.
        """
        # Expired 30s ago — within 60s leeway
        payload_30s = {
            "sub": "act_test",
            "org_id": "org_test",
            "iss": "indemn-os",
            "aud": ["runtime-voice-frontdoor"],
            "exp": int(time.time()) - 30,
        }
        token_30s = pyjwt.encode(payload_30s, _test_private_key, algorithm="RS256")

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response_30s = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": f"Bearer {token_30s}"},
            )
        # NOT 401-with-reason=expired (within leeway)
        if response_30s.status_code == 401:
            assert response_30s.json().get("reason") != "expired"

        # Expired 90s ago — outside 60s leeway
        payload_90s = {**payload_30s, "exp": int(time.time()) - 90}
        token_90s = pyjwt.encode(payload_90s, _test_private_key, algorithm="RS256")

        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response_90s = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": f"Bearer {token_90s}"},
            )
        assert response_90s.status_code == 401
        assert response_90s.json()["reason"] == "expired"


class TestJWTHS256PurposeClaim:
    """AI-407 pre-merge security: HS256 path must reject non-session tokens.

    The OS kernel (kernel/auth/jwt.py) signs THREE distinct token kinds
    with the SAME `JWT_SIGNING_KEY`:
    - access tokens (create_access_token) — NO `purpose` claim
    - partial MFA-challenge tokens (create_partial_token) — `purpose="mfa_challenge"`
    - magic-link tokens (generate_magic_link_token) — `purpose=<varies>`

    Without a purpose gate at the frontdoor's HS256 path, a leaked
    magic-link or MFA-challenge token would be accepted by /sessions
    as a valid LiveKit session credential. Realistic exploit is narrow
    (those tokens are short-lived and narrowly scoped) but the
    cross-token-kind reuse is a real surface — block it explicitly.

    Allowed: no purpose claim (canonical access token), purpose="session",
    purpose="access" (forward-compatible if the OS adds explicit claims later).

    Rejected: every other purpose value.
    """

    def _hs256_token(self, *, purpose=None, key="test-shared-secret"):
        """Mint an HS256 token with the OS claim shape; optional purpose."""
        payload = {
            "actor_id": "act_test",
            "org_id": "org_test",
            "roles": ["platform_admin"],
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "jti": f"test-{int(time.time())}",
        }
        if purpose is not None:
            payload["purpose"] = purpose
        return pyjwt.encode(payload, key, algorithm="HS256")

    @pytest.fixture
    def hs256_env(self, monkeypatch):
        """Flip frontdoor JWT_ALGORITHM to HS256 + set the shared secret."""
        monkeypatch.setenv("JWT_ALGORITHM", "HS256")
        monkeypatch.setenv("JWT_SIGNING_KEY", "test-shared-secret")

    def test_access_token_no_purpose_accepted(
        self, client, valid_deployment, hs256_env
    ):
        """Canonical OS access token (no purpose claim) → JWT passes."""
        token = self._hs256_token(purpose=None)
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": f"Bearer {token}"},
            )
        # NOT 401 with reason invalid/wrong_purpose — downstream checks
        # may fail (acts_as etc.) but JWT verification itself passed.
        if response.status_code == 401:
            assert response.json().get("reason") != "invalid"

    def test_session_purpose_accepted(
        self, client, valid_deployment, hs256_env
    ):
        """Explicit purpose='session' accepted (forward-compat)."""
        token = self._hs256_token(purpose="session")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code == 401:
            assert response.json().get("reason") != "invalid"

    def test_access_purpose_accepted(
        self, client, valid_deployment, hs256_env
    ):
        """Explicit purpose='access' accepted (forward-compat)."""
        token = self._hs256_token(purpose="access")
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code == 401:
            assert response.json().get("reason") != "invalid"

    @pytest.mark.parametrize(
        "rejected_purpose",
        ["mfa_challenge", "password_reset", "email_verify", "magic_link"],
    )
    def test_non_session_purpose_rejected(
        self, client, valid_deployment, hs256_env, rejected_purpose
    ):
        """MFA-challenge, magic-link, and any other non-session purpose
        must be rejected at /sessions even if signed correctly.

        Parametrized across the known non-session purposes the OS issues
        + a couple of speculative magic-link purpose values. Each maps to
        401 reason=invalid (purpose mismatch folds into the generic
        invalid bucket; the SDK's only actionable response is 're-mint
        a token' either way per the existing wrong_audience/issuer
        precedent)."""
        token = self._hs256_token(purpose=rejected_purpose)
        with patch(
            "harness.sessions._load_deployment",
            new=AsyncMock(return_value=_stub_deployment(valid_deployment["_id"])),
        ):
            response = _post_sessions(
                client,
                valid_deployment["_id"],
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 401
        assert response.json().get("reason") == "invalid"
