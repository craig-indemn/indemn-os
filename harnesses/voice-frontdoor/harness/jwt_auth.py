"""JWT validation for the voice frontdoor (AI-407 §10.6 + §10.3.1 step 3).

User JWTs are minted by the indemn-os API (RS256, asymmetric) and carried
on POST /sessions as `Authorization: Bearer <token>`. The frontdoor
verifies the signature locally using the public key from AWS Secrets —
no API round-trip — so session-start stays fast.

Required claims per §10.6:
- sub (actor_id; surfaces as the authenticated_actor_id used by the
  acts_as gate in Task 2.31)
- org_id
- exp
- iss == "indemn-os"
- aud contains "runtime-voice-frontdoor"

Algorithm: RS256. Clock-skew tolerance: 60 seconds (per §10.3.1).

Public key lookup: AWS Secrets Manager via boto3, secret name from env
var `JWT_PUBLIC_KEY_SECRET_REF` (e.g., `indemn/dev/shared/jwt-public-key`).
Cached for the process lifetime via `lru_cache` — the key rotates rarely
and the cost of a Secrets call per /sessions would burn the latency budget.

Tests stub `_get_public_key` via an autouse fixture in
`tests/conftest.py` so they pair with the session-scoped RSA keypair
without hitting AWS.
"""

import logging
import os
from functools import lru_cache

import boto3
import jwt as pyjwt

log = logging.getLogger(__name__)


JWT_AUDIENCE = "runtime-voice-frontdoor"
JWT_ISSUER = "indemn-os"
JWT_LEEWAY_SECONDS = 60
JWT_ALGORITHM = "RS256"


@lru_cache(maxsize=1)
def _get_public_key() -> str:
    """Load JWT signing public key from AWS Secrets Manager.

    Cached for the lifetime of the process — the JWT public key rotates
    via deploy (new container picks up the new key on startup), not per
    request, so caching is correct.

    Raises KeyError if `JWT_PUBLIC_KEY_SECRET_REF` is not set. Raises
    botocore.exceptions.ClientError on Secrets Manager failures.
    """
    secret_name = os.environ["JWT_PUBLIC_KEY_SECRET_REF"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def verify_jwt(token: str) -> dict:
    """Verify a Bearer JWT and return its claims.

    Pyjwt raises:
    - ExpiredSignatureError on `exp` past (with leeway applied)
    - InvalidAudienceError on aud mismatch
    - InvalidIssuerError on iss mismatch
    - InvalidSignatureError on signature mismatch
    - DecodeError on malformed token
    - PyJWTError as the parent of all of the above

    Callers should catch ExpiredSignatureError specifically (→ 401
    reason=expired) and fall back to PyJWTError for everything else
    (→ 401 reason=invalid).
    """
    public_key = _get_public_key()
    return pyjwt.decode(
        token,
        public_key,
        algorithms=[JWT_ALGORITHM],
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
        leeway=JWT_LEEWAY_SECONDS,
    )
