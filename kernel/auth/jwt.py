"""JWT token creation and verification.

Phase 1: basic JWT with jti for future revocation.
Phase 4: revocation cache with Change Stream invalidation + partial tokens for MFA.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from kernel.config import settings

logger = logging.getLogger(__name__)

# --- In-memory revocation cache [G-42] ---

_revocation_cache: dict[str, float] = {}  # jti -> revoked_at timestamp
_CACHE_TTL = 900  # 15 minutes (matches max access token lifetime)


def create_access_token(actor_id: str, org_id: str, roles: list[str]) -> tuple[str, str]:
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
    """Verify and decode a JWT access token with revocation check."""
    payload = jwt.decode(token, settings.jwt_signing_key, algorithms=[settings.jwt_algorithm])

    # Check revocation cache [G-42]
    jti = payload.get("jti")
    if jti and jti in _revocation_cache:
        raise jwt.InvalidTokenError("Token has been revoked")

    # Evict expired cache entries
    _evict_expired_cache()

    return payload


def create_partial_token(actor, session) -> str:
    """Create a short-lived token for MFA challenge. [G-36]

    Can only be used for MFA verification — not API access.
    """
    return jwt.encode(
        {
            "actor_id": str(actor.id),
            "session_id": str(session.id),
            "purpose": "mfa_challenge",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        settings.jwt_signing_key,
        algorithm=settings.jwt_algorithm,
    )


def verify_partial_token(token: str) -> dict:
    """Verify a partial token (MFA challenge only)."""
    payload = jwt.decode(token, settings.jwt_signing_key, algorithms=[settings.jwt_algorithm])
    if payload.get("purpose") != "mfa_challenge":
        raise jwt.InvalidTokenError("Not a partial token")
    return payload


def generate_magic_link_token(actor, purpose: str, expires_hours: int = 4) -> str:
    """Generate a magic link token for password reset or email verification. [G-38]"""
    return jwt.encode(
        {
            "actor_id": str(actor.id),
            "purpose": purpose,
            "exp": datetime.now(timezone.utc) + timedelta(hours=expires_hours),
        },
        settings.jwt_signing_key,
        algorithm=settings.jwt_algorithm,
    )


def verify_magic_link_token(token: str, purpose: str) -> dict:
    """Verify a magic link token matches the expected purpose."""
    payload = jwt.decode(token, settings.jwt_signing_key, algorithms=[settings.jwt_algorithm])
    if payload.get("purpose") != purpose:
        raise jwt.InvalidTokenError(f"Token purpose mismatch: expected {purpose}")
    return payload


# --- Revocation cache management [G-42] ---


async def bootstrap_revocation_cache() -> None:
    """On API startup: load recently revoked sessions into the in-memory cache."""
    from kernel_entities.session import Session

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_CACHE_TTL)
    revoked = await Session.find(
        {
            "status": "revoked",
            "updated_at": {"$gte": cutoff},
        }
    ).to_list()

    for session in revoked:
        if session.access_token_jti:
            _revocation_cache[session.access_token_jti] = time.time()

    logger.info("Revocation cache bootstrapped with %d entries", len(_revocation_cache))


async def watch_revocations() -> None:
    """Watch for Session revocations via Change Stream. [G-42]

    Run as a background task on API startup. Updates the in-memory
    revocation cache when sessions are revoked.
    """
    from kernel.db import get_database

    db = get_database()
    pipeline = [
        {
            "$match": {
                "fullDocument.status": "revoked",
                "operationType": "update",
            },
        },
    ]
    try:
        async with db["sessions"].watch(pipeline, full_document="updateLookup") as stream:
            async for change in stream:
                doc = change.get("fullDocument", {})
                jti = doc.get("access_token_jti")
                if jti:
                    _revocation_cache[jti] = time.time()
                    logger.debug("Revocation cache updated: %s", jti)
    except Exception:
        logger.exception("Revocation watcher stopped")


def revoke_in_cache(jti: str) -> None:
    """Directly add a JTI to the revocation cache (used during session revocation)."""
    _revocation_cache[jti] = time.time()


def _evict_expired_cache() -> None:
    """Remove expired entries from the revocation cache."""
    now = time.time()
    expired = [k for k, v in _revocation_cache.items() if now - v > _CACHE_TTL]
    for k in expired:
        del _revocation_cache[k]
