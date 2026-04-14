"""JWT token creation and verification.

Phase 1: basic JWT with jti for future revocation.
Phase 4 adds: revocation cache with Change Stream invalidation.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from kernel.config import settings


def create_access_token(
    actor_id: str, org_id: str, roles: list[str]
) -> tuple[str, str]:
    """Create a JWT access token. Returns (token, jti)."""
    jti = str(uuid4())
    payload = {
        "actor_id": str(actor_id),
        "org_id": str(org_id),
        "roles": roles,
        "jti": jti,
        "exp": datetime.now(timezone.utc)
        + timedelta(minutes=settings.jwt_access_token_expire_minutes),
        "iat": datetime.now(timezone.utc),
    }
    token = jwt.encode(payload, settings.jwt_signing_key, algorithm=settings.jwt_algorithm)
    return token, jti


def verify_access_token(token: str) -> dict:
    """Verify and decode a JWT access token."""
    payload = jwt.decode(
        token, settings.jwt_signing_key, algorithms=[settings.jwt_algorithm]
    )
    # Phase 4 adds: check jti against revocation cache
    return payload
