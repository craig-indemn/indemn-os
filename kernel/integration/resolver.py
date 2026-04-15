"""Credential resolution — priority chain: actor → owner → org.

Resolves which Integration entity to use for a given system_type,
following the credential resolution priority from the spec.
"""

from bson import ObjectId

from kernel.context import current_actor_id, current_org_id
from kernel.integration.adapter import AdapterNotFoundError
from kernel_entities.actor import Actor
from kernel_entities.integration import Integration
from kernel_entities.role import Role


async def resolve_integration(
    system_type: str,
    actor_id: ObjectId = None,
    org_id: ObjectId = None,
    require_org_only: bool = False,
) -> Integration:
    """Resolve the Integration to use. Priority: actor → owner → org."""
    _actor_id = actor_id or ObjectId(current_actor_id.get())
    _org_id = org_id or current_org_id.get()

    # Step 1: Actor's own personal integration
    if not require_org_only:
        personal = await Integration.find_one({
            "owner_type": "actor",
            "owner_id": _actor_id,
            "system_type": system_type,
            "status": "active",
            "org_id": _org_id,
        })
        if personal:
            return personal

    # Step 2: Owner's personal integration (for owner-bound associates)
    actor = None
    if not require_org_only:
        actor = await Actor.get(_actor_id)
        if actor and actor.owner_actor_id:
            owner_integration = await Integration.find_one({
                "owner_type": "actor",
                "owner_id": actor.owner_actor_id,
                "system_type": system_type,
                "status": "active",
                "org_id": _org_id,
            })
            if owner_integration:
                return owner_integration

    # Step 3: Org-level with role-based access
    if actor is None:
        actor = await Actor.get(_actor_id)
    if actor:
        roles = await Role.find({"_id": {"$in": actor.role_ids}}).to_list()
        role_names = [r.name for r in roles]

        org_integration = await Integration.find_one({
            "owner_type": "org",
            "org_id": _org_id,
            "system_type": system_type,
            "status": "active",
            "access.roles": {"$in": role_names},
        })
        if org_integration:
            return org_integration

    raise AdapterNotFoundError(
        f"No {system_type} integration available. "
        f"Create one with: indemn integration create --system-type {system_type} ..."
    )
