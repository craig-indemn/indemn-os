"""Session lifecycle management.

Creates, validates, and revokes Session entities.
Phase 1: basic session creation for password and token auth.
Phase 4 adds: SSO, MFA, platform admin sessions, revocation cache.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from bson import ObjectId

from kernel.auth.jwt import create_access_token
from kernel_entities.session import Session


async def create_session(
    actor,
    auth_method: str,
    session_type: str = "user_interactive",
    ip_address: str = None,
    user_agent: str = None,
    expire_minutes: int = None,
) -> tuple[Session, str]:
    """Create a new Session entity and issue an access token.
    Returns (session, access_token)."""
    from kernel.config import settings

    expire_mins = expire_minutes or settings.jwt_access_token_expire_minutes
    # Get role names for JWT claims
    from kernel_entities.role import Role
    roles = await Role.find({"_id": {"$in": actor.role_ids}}).to_list()
    role_names = [r.name for r in roles]

    token, jti = create_access_token(str(actor.id), str(actor.org_id), role_names)

    session = Session(
        org_id=actor.org_id,
        actor_id=actor.id,
        type=session_type,
        auth_method_used=auth_method,
        ip_address=ip_address,
        user_agent=user_agent,
        status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=expire_mins),
        access_token_jti=jti,
    )
    await session.insert()

    return session, token


async def revoke_session(session_id: ObjectId):
    """Revoke a session by transitioning to 'revoked' status."""
    session = await Session.get(session_id)
    if session and session.status == "active":
        session.transition_to("revoked")
        await session.save_tracked(actor_id="system:revocation", method="revoke")


async def revoke_all_sessions(actor_id: ObjectId):
    """Revoke all active sessions for an actor (e.g., on password reset)."""
    sessions = await Session.find(
        {"actor_id": actor_id, "status": "active"}
    ).to_list()
    for session in sessions:
        session.transition_to("revoked")
        await session.save_tracked(actor_id="system:revocation", method="revoke_all")
