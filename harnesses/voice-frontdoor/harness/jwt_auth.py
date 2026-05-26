"""JWT validation for the voice frontdoor (AI-407 §10.6 + §10.3.1 step 3).

Two algorithms supported, selected by `JWT_ALGORITHM` env var:

- **HS256 (default + OS-current)** — symmetric HMAC with shared secret
  `JWT_SIGNING_KEY`. Matches the current indemn-os API's auth
  infrastructure (kernel/auth/jwt.py uses `settings.jwt_signing_key` +
  `settings.jwt_algorithm` defaulted to HS256). Production deploy of
  the frontdoor uses this so real OS-issued user JWTs validate.

  OS claim shape: `actor_id` (not `sub`), `org_id`, `exp`, `iat`, `jti`,
  `roles`. The frontdoor normalizes by surfacing `sub = actor_id` so
  downstream code (the acts_as gate in Task 2.31, the Interaction's
  created_by, etc.) reads `claims["sub"]` uniformly. No `iss` / `aud`
  requirement in HS256 mode — the OS doesn't set them today.

- **RS256 (forward design / tests)** — asymmetric RSA. Per §10.6
  forward design: signing private key in the API server, public key in
  AWS Secrets at `indemn/dev/shared/jwt-public-key`. Required claims:
  `sub`, `org_id`, `exp`, `iss == "indemn-os"`,
  `aud contains "runtime-voice-frontdoor"`. Used by the unit test suite
  + future production when OS-side RS256 lands.

  Tests stub `_get_public_key` via an autouse fixture in
  `tests/conftest.py` so they pair with the session-scoped RSA keypair
  without hitting AWS.

Clock-skew tolerance: 60 seconds (per §10.3.1).

**Deviation from playbook spec recorded in os-learnings.md:** the
playbook called for RS256-only; OS hasn't shipped RS256-issuing yet.
HS256 path bridges to current OS reality. When OS-side RS256 ships,
flip `JWT_ALGORITHM=RS256` + populate `JWT_PUBLIC_KEY_SECRET_REF` and
the iss/aud checks turn back on.
"""

import logging
import os
from functools import lru_cache

import jwt as pyjwt

log = logging.getLogger(__name__)


JWT_AUDIENCE = "runtime-voice-frontdoor"
JWT_ISSUER = "indemn-os"
JWT_LEEWAY_SECONDS = 60


@lru_cache(maxsize=1)
def _get_public_key() -> str:
    """RS256 path: load JWT signing public key from AWS Secrets Manager.

    Cached for the lifetime of the process. Raises KeyError if
    `JWT_PUBLIC_KEY_SECRET_REF` is not set; raises
    botocore.exceptions.ClientError on Secrets Manager failures.

    Tests stub this function via the autouse `_stub_jwt_public_key`
    fixture in tests/conftest.py.
    """
    # Lazy import — boto3 is heavy; only loaded on RS256 path
    import boto3

    secret_name = os.environ["JWT_PUBLIC_KEY_SECRET_REF"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def verify_jwt(token: str) -> dict:
    """Verify a Bearer JWT and return its claims.

    Algorithm + key resolution:
    - JWT_ALGORITHM=HS256 (default): key = `JWT_SIGNING_KEY` env var
      (the OS's shared HMAC secret). No iss/aud check. Normalizes
      `actor_id` → `sub` so downstream reads `claims["sub"]`.
    - JWT_ALGORITHM=RS256: key = `_get_public_key()` (AWS Secrets, or
      test fixture). iss + aud required per §10.3.1.

    Pyjwt raises:
    - ExpiredSignatureError on `exp` past (with leeway applied)
    - InvalidAudienceError on aud mismatch (RS256 only)
    - InvalidIssuerError on iss mismatch (RS256 only)
    - InvalidSignatureError on signature mismatch
    - DecodeError on malformed token
    - PyJWTError as the parent of all of the above

    Callers should catch ExpiredSignatureError specifically (→ 401
    reason=expired) and fall back to PyJWTError for everything else
    (→ 401 reason=invalid).
    """
    algorithm = os.environ.get("JWT_ALGORITHM", "RS256")
    if algorithm == "HS256":
        # OS-current path. Symmetric HMAC; no iss/aud (OS doesn't set them).
        key = os.environ["JWT_SIGNING_KEY"]
        claims = pyjwt.decode(
            token,
            key,
            algorithms=["HS256"],
            leeway=JWT_LEEWAY_SECONDS,
        )
        # AI-407 pre-merge security fix: enforce `purpose` claim.
        # The OS kernel issues MULTIPLE token kinds signed with the same
        # JWT_SIGNING_KEY (kernel/auth/jwt.py): access tokens (NO purpose
        # claim), partial MFA-challenge tokens (purpose="mfa_challenge",
        # 5-min lifetime), magic-link tokens (purpose=<caller-supplied>,
        # 4-hr lifetime). Without a purpose gate, /sessions would accept
        # a leaked magic-link token as a LiveKit session credential —
        # narrow exploit but real surface. Accept only:
        #   - None (canonical access tokens, the OS-current shape)
        #   - "session" / "access" (forward-compatible if OS adds explicit
        #      purpose claims to access tokens later)
        # Reject every other purpose explicitly.
        purpose = claims.get("purpose")
        if purpose is not None and purpose not in ("session", "access"):
            raise pyjwt.InvalidTokenError(
                f"Token purpose '{purpose}' not valid for /sessions"
            )
        # Normalize: OS uses `actor_id`; design uses `sub`. Surface both.
        if "actor_id" in claims and "sub" not in claims:
            claims["sub"] = claims["actor_id"]
        return claims

    # RS256 path — design forward / test path.
    public_key = _get_public_key()
    return pyjwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
        leeway=JWT_LEEWAY_SECONDS,
    )
