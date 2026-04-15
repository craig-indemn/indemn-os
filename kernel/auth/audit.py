"""Auth audit events — written to the changes collection.

Every authentication action is recorded: logins, MFA, password changes,
role grants/revocations, platform admin access, brute force lockouts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from bson import ObjectId

if TYPE_CHECKING:
    from kernel_entities.actor import Actor

AUTH_EVENT_TYPES = [
    "auth.login_attempt",
    "auth.login_success",
    "auth.login_failure",
    "auth.session_created",
    "auth.session_refreshed",
    "auth.session_revoked",
    "auth.mfa_enrolled",
    "auth.mfa_challenged",
    "auth.mfa_verified",
    "auth.mfa_reset",
    "auth.password_changed",
    "auth.method_added",
    "auth.method_removed",
    "auth.role_granted",
    "auth.role_revoked",
    "auth.lifecycle_transitioned",
    "auth.platform_admin_access",
    "auth.brute_force_lockout",
]


async def write_auth_event(
    actor: Actor,
    event_type: str,
    metadata: Optional[dict] = None,
) -> None:
    """Write an auth event to the changes collection."""
    from kernel.changes.collection import ChangeRecord
    from kernel.changes.hash_chain import compute_hash, get_previous_hash

    record = ChangeRecord(
        org_id=actor.org_id,
        entity_type="Actor",
        entity_id=actor.id,
        change_type=event_type,
        actor_id=str(actor.id),
        method=event_type,
        method_metadata=metadata or {},
    )
    record.previous_hash = await get_previous_hash(actor.org_id, None)
    record.current_hash = compute_hash(record)
    await record.insert()


async def write_auth_event_in_org(
    org_id: ObjectId,
    actor: Actor,
    event_type: str,
    metadata: Optional[dict] = None,
) -> None:
    """Write an auth event scoped to a specific org (e.g., platform admin access)."""
    from kernel.changes.collection import ChangeRecord
    from kernel.changes.hash_chain import compute_hash, get_previous_hash

    record = ChangeRecord(
        org_id=org_id,
        entity_type="Actor",
        entity_id=actor.id,
        change_type=event_type,
        actor_id=str(actor.id),
        method=event_type,
        method_metadata=metadata or {},
    )
    record.previous_hash = await get_previous_hash(org_id, None)
    record.current_hash = compute_hash(record)
    await record.insert()


async def write_auth_event_by_email(
    email: str,
    org_id: ObjectId,
    event_type: str,
    metadata: Optional[dict] = None,
) -> None:
    """Write an auth event when we only have an email (pre-auth, e.g., brute force)."""
    from kernel.changes.collection import ChangeRecord
    from kernel.changes.hash_chain import compute_hash, get_previous_hash

    record = ChangeRecord(
        org_id=org_id,
        entity_type="Actor",
        entity_id=ObjectId(),  # Placeholder — actor not yet resolved
        change_type=event_type,
        actor_id=f"email:{email}",
        method=event_type,
        method_metadata=metadata or {},
    )
    record.previous_hash = await get_previous_hash(org_id, None)
    record.current_hash = compute_hash(record)
    await record.insert()
