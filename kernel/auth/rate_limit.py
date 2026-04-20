"""Pre-auth rate limiting — MongoDB sliding window.

Phase 1: interface definition only.
Phase 4: full implementation with sliding window counters.

Tracks failed login attempts by hashed IP+email. After 5 failures in 10 minutes,
locks the account for 30 minutes. All lockouts are audited.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

RATE_LIMIT_COLLECTION = "auth_rate_limits"

# Thresholds
MAX_FAILURES = 5
WINDOW_MINUTES = 10
LOCKOUT_MINUTES = 30


async def check_rate_limit(ip_address: str, email: str, org_id) -> bool:
    """Check if login attempts are rate-limited.

    Returns True if the attempt should be BLOCKED.
    """
    from kernel.db import get_database

    db = get_database()
    collection = db[RATE_LIMIT_COLLECTION]

    key = _make_key(ip_address, email)
    window = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)

    doc = await collection.find_one({"_id": key})
    if not doc:
        return False

    # Check active lockout
    locked_until = doc.get("locked_until")
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    window_naive = window.replace(tzinfo=None)
    if locked_until and locked_until > now_naive:
        return True

    # Count recent failures (MongoDB stores naive UTC datetimes)
    recent_failures = [t for t in doc.get("failures", []) if t > window_naive]
    if len(recent_failures) >= MAX_FAILURES:
        # Apply new lockout
        await collection.update_one(
            {"_id": key},
            {
                "$set": {
                    "locked_until": datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES),
                }
            },
            upsert=True,
        )
        # Audit the lockout
        from kernel.auth.audit import write_auth_event_by_email

        await write_auth_event_by_email(
            email,
            org_id,
            "auth.brute_force_lockout",
            {"ip_address": ip_address, "failures": len(recent_failures)},
        )
        return True

    return False


async def record_failed_attempt(ip_address: str, email: str) -> None:
    """Record a failed login attempt."""
    from kernel.db import get_database

    db = get_database()
    key = _make_key(ip_address, email)
    await db[RATE_LIMIT_COLLECTION].update_one(
        {"_id": key},
        {
            "$push": {"failures": datetime.now(timezone.utc)},
            "$set": {"last_attempt": datetime.now(timezone.utc)},
        },
        upsert=True,
    )


def _make_key(ip_address: str, email: str) -> str:
    """Hash IP + email to create a rate limit key. Don't store raw email."""
    return hashlib.sha256(f"{ip_address}:{email}".encode()).hexdigest()
