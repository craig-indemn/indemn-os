"""Chat WebSocket JWT validation (AI-408 Task 3.4).

Inherits HS256 dual-mode + purpose-claim enforcement from AI-407 via the
shared `harness_common.jwt_auth` module (extracted from voice-frontdoor in
this same task). Chat pins audience="runtime-chat" (RS256 only — HS256
doesn't check aud per OS reality).

WebSocket close code 1008 ("Policy Violation" per RFC 6455) is the
canonical analog of HTTP 401 — applied uniformly to missing / expired /
invalid / wrong-purpose JWTs.

The headline cross-runtime token-reuse defense:
- HS256 path: rejection via purpose-claim (kernel-issued mfa_challenge /
  password_reset / magic_link tokens cannot be replayed against /connect).
- RS256 path: rejection via audience pinning — a JWT minted with
  aud="runtime-voice-frontdoor" MUST NOT validate against chat (and vice
  versa). The shared `test_jwt_auth.py` already pins this; chat-side
  integration here verifies it surfaces as a 1008 close.
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same setup as other AI-408 tests — unstub starlette + reload real
# harness_common.cli + stub harness.session.
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

import main as harness_main  # noqa: E402


# -----------------------------------------------------------------------------
# Test helpers
# -----------------------------------------------------------------------------


_HS256_KEY = "test-secret-key-32-bytes-or-more-long"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_websocket(origin: str = "https://sales.indemn.ai"):
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.headers = {"origin": origin}
    return ws


def _send_payloads(ws):
    return [c.args[0] for c in ws.send_json.call_args_list]


def _hs256_token(**overrides) -> str:
    """Mint an HS256 token using the OS-shape claims (actor_id, org_id, etc.)."""
    now = int(time.time())
    claims = {
        "actor_id": "act_test",
        "org_id": "org_test",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return pyjwt.encode(claims, _HS256_KEY, algorithm="HS256")


_ACTIVE_DEPLOYMENT = {
    "_id": "dep_chat",
    "status": "active",
    "associate_id": "act_associate",
    "allowed_origins": ["https://sales.indemn.ai"],
    "acts_as": "associate_self",
}


def _start_with_token(monkeypatch, *, auth_token: str, deployment=None):
    """Helper: drive `_start_deployment_session` with the given JWT under
    HS256 mode. Returns the mock websocket so callers can inspect the
    payloads sent + close-codes."""
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_SIGNING_KEY", _HS256_KEY)

    ws = _mock_websocket()
    chat_instance = MagicMock()
    chat_instance.start = AsyncMock()
    chat_instance.close = AsyncMock()
    chat_instance.interaction_id = "int_new"

    dep = deployment or _ACTIVE_DEPLOYMENT

    with patch.object(harness_main, "indemn", return_value=dep), patch.object(
        harness_main, "ChatSession", return_value=chat_instance
    ):
        _run(
            harness_main._start_deployment_session(
                websocket=ws,
                deployment_id=dep["_id"],
                dynamic_params={},
                auth_token=auth_token,
                connect_msg={},
            )
        )
    return ws


# -----------------------------------------------------------------------------
# JWT validation — happy path + rejection cases (HS256, OS-current mode)
# -----------------------------------------------------------------------------


class TestChatJWTHS256Mode:
    def test_valid_token_passes_through_chain(self, monkeypatch):
        """Valid HS256 token → JWT validates → session continues to status
        check → ChatSession constructed (no error sent)."""
        ws = _start_with_token(
            monkeypatch, auth_token=_hs256_token(actor_id="act_alice")
        )
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        ws.close.assert_not_called()

    def test_missing_auth_token_rejected_with_1008(self, monkeypatch):
        """Empty auth_token → 1008 close with reason=missing."""
        ws = _start_with_token(monkeypatch, auth_token="")
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "unauthorized"
        assert errors[0]["reason"] == "missing"
        ws.close.assert_called_once_with(code=1008)

    def test_expired_token_rejected_with_1008(self, monkeypatch):
        """Expired token → distinct reason=expired so SDK can show specific
        message ('please re-authenticate' vs generic 'invalid token')."""
        ws = _start_with_token(
            monkeypatch,
            auth_token=_hs256_token(exp=int(time.time()) - 3600),
        )
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "unauthorized"
        assert errors[0]["reason"] == "expired"
        ws.close.assert_called_once_with(code=1008)

    def test_bad_signature_rejected(self, monkeypatch):
        """JWT signed with a different key → reason=invalid."""
        bad_token = pyjwt.encode(
            {
                "actor_id": "act_test",
                "exp": int(time.time()) + 3600,
            },
            "DIFFERENT-secret-key-than-prod",
            algorithm="HS256",
        )
        ws = _start_with_token(monkeypatch, auth_token=bad_token)
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "unauthorized"
        assert errors[0]["reason"] == "invalid"
        ws.close.assert_called_once_with(code=1008)

    def test_malformed_token_rejected(self, monkeypatch):
        """Non-JWT garbage → reason=invalid."""
        ws = _start_with_token(monkeypatch, auth_token="not-a-jwt-at-all")
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors[0]["code"] == "unauthorized"
        assert errors[0]["reason"] == "invalid"
        ws.close.assert_called_once_with(code=1008)


class TestChatJWTHS256PurposeClaim:
    """AI-407 pre-merge security fix inherited verbatim. The OS kernel signs
    multiple token kinds with the same JWT_SIGNING_KEY — chat /connect MUST
    reject tokens minted for other purposes (mfa_challenge, password_reset,
    magic_link). Symmetric with voice-frontdoor's `TestJWTHS256PurposeClaim`.
    """

    def test_no_purpose_claim_accepted(self, monkeypatch):
        """Canonical OS access token (no purpose claim) → accepted."""
        ws = _start_with_token(monkeypatch, auth_token=_hs256_token())
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []

    def test_purpose_session_accepted(self, monkeypatch):
        ws = _start_with_token(
            monkeypatch, auth_token=_hs256_token(purpose="session")
        )
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []

    def test_purpose_access_accepted(self, monkeypatch):
        ws = _start_with_token(
            monkeypatch, auth_token=_hs256_token(purpose="access")
        )
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []

    def test_mfa_challenge_purpose_rejected(self, monkeypatch):
        """A leaked partial-MFA token MUST NOT open a chat session."""
        ws = _start_with_token(
            monkeypatch, auth_token=_hs256_token(purpose="mfa_challenge")
        )
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "invalid"
        ws.close.assert_called_once_with(code=1008)

    def test_password_reset_purpose_rejected(self, monkeypatch):
        ws = _start_with_token(
            monkeypatch, auth_token=_hs256_token(purpose="password_reset")
        )
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors[0]["reason"] == "invalid"
        ws.close.assert_called_once_with(code=1008)

    def test_magic_link_purpose_rejected(self, monkeypatch):
        ws = _start_with_token(
            monkeypatch, auth_token=_hs256_token(purpose="magic_link")
        )
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors[0]["reason"] == "invalid"
        ws.close.assert_called_once_with(code=1008)


# -----------------------------------------------------------------------------
# Validation order — JWT comes BEFORE status check
# -----------------------------------------------------------------------------


class TestJWTValidationOrder:
    def test_jwt_check_runs_before_status_check(self, monkeypatch):
        """Critical security property: an unauthenticated caller must NOT
        learn whether a Deployment is active/paused. With JWT BEFORE status,
        a wrong-token attacker on a paused Deployment gets `unauthorized`,
        NOT `deployment_not_active` — Deployment lifecycle stays private."""
        paused_dep = {**_ACTIVE_DEPLOYMENT, "status": "paused"}
        ws = _start_with_token(monkeypatch, auth_token="garbage", deployment=paused_dep)

        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "unauthorized"  # NOT deployment_not_active
        ws.close.assert_called_once_with(code=1008)  # NOT 4009

    def test_jwt_check_runs_after_origin_check(self, monkeypatch):
        """Symmetric: an attacker from the wrong Origin should NOT be able
        to probe JWT validity (which could be used as an auth oracle).
        Origin check fires first → 1008 origin_not_allowed, JWT never
        evaluated."""
        monkeypatch.setenv("JWT_ALGORITHM", "HS256")
        monkeypatch.setenv("JWT_SIGNING_KEY", _HS256_KEY)
        ws = _mock_websocket(origin="https://malicious.example.com")
        chat_instance = MagicMock()
        chat_instance.start = AsyncMock()
        chat_instance.close = AsyncMock()

        # Even with a perfectly valid JWT, the wrong-origin call rejects first.
        with patch.object(
            harness_main, "indemn", return_value=_ACTIVE_DEPLOYMENT
        ), patch.object(
            harness_main, "ChatSession", return_value=chat_instance
        ):
            _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_chat",
                    dynamic_params={},
                    auth_token=_hs256_token(),
                    connect_msg={},
                )
            )

        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        # Origin_not_allowed — not unauthorized
        assert errors[0]["code"] == "origin_not_allowed"


# -----------------------------------------------------------------------------
# RS256 audience pinning — cross-runtime defense surfaces as 1008 invalid
# -----------------------------------------------------------------------------


class TestChatJWTRS256AudiencePinning:
    """The shared `test_jwt_auth.py` proves audience pinning at the pyjwt
    layer. This integration test verifies the chat handler surfaces the
    pyjwt.InvalidAudienceError as a 1008 close with reason=invalid."""

    def test_voice_audience_token_rejected_on_chat(
        self, monkeypatch, _rsa_keypair
    ):
        """Cross-runtime token reuse — voice-minted JWT replayed on chat
        gets reason=invalid (audience mismatch caught by shared verify)."""
        private_pem, public_pem = _rsa_keypair
        monkeypatch.setenv("JWT_ALGORITHM", "RS256")
        monkeypatch.setattr(
            "harness_common.jwt_auth._get_public_key",
            lambda: public_pem,
        )

        # Mint a token for the VOICE audience
        voice_token = pyjwt.encode(
            {
                "sub": "act_test",
                "org_id": "org_test",
                "iss": "indemn-os",
                "aud": ["runtime-voice-frontdoor"],
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            private_pem,
            algorithm="RS256",
        )

        ws = _mock_websocket()
        chat_instance = MagicMock()
        chat_instance.start = AsyncMock()
        chat_instance.close = AsyncMock()

        with patch.object(
            harness_main, "indemn", return_value=_ACTIVE_DEPLOYMENT
        ), patch.object(
            harness_main, "ChatSession", return_value=chat_instance
        ):
            _run(
                harness_main._start_deployment_session(
                    websocket=ws,
                    deployment_id="dep_chat",
                    dynamic_params={},
                    auth_token=voice_token,
                    connect_msg={},
                )
            )

        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "unauthorized"
        assert errors[0]["reason"] == "invalid"
        ws.close.assert_called_once_with(code=1008)


# -----------------------------------------------------------------------------
# Module surface — pin the public constants + the chat wrapper
# -----------------------------------------------------------------------------


class TestModuleSurface:
    def test_jwt_audience_pinned_to_runtime_chat(self):
        """Per the playbook §10.6 — chat audience MUST be `runtime-chat`."""
        assert harness_main.JWT_AUDIENCE == "runtime-chat"

    def test_verify_jwt_wrapper_delegates_to_shared(self, monkeypatch):
        """The chat `_verify_jwt` wrapper forwards the token to the shared
        impl with audience="runtime-chat" baked in."""
        captured = {}

        def _capture(token, *, audience):
            captured["token"] = token
            captured["audience"] = audience
            return {"sub": "act_test"}

        monkeypatch.setattr(harness_main, "_verify_jwt_shared", _capture)
        result = harness_main._verify_jwt("test-token")
        assert captured["token"] == "test-token"
        assert captured["audience"] == "runtime-chat"
        assert result == {"sub": "act_test"}


# -----------------------------------------------------------------------------
# Shared RSA keypair fixture (session-scoped to amortize key-gen cost)
# -----------------------------------------------------------------------------


import pytest  # noqa: E402  (imported here for fixture scope)
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402


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
