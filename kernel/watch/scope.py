"""Scope resolution for watches.

Two resolution types:
- field_path: traverses entity relationships to resolve to an actor_id
- active_context: looks up Attention records for real-time event delivery

Scope is resolved at emit time. The kernel writes target_actor_id on the message.
Full implementation activated in Phase 5. Phase 1 provides the stub.
"""

from bson import ObjectId


async def resolve_scope(scope: dict, entity, session) -> ObjectId | None:
    """Resolve a watch scope to a target actor.

    Returns actor ObjectId, or None if scope can't be resolved (message not created).
    """
    scope_type = scope.get("type")

    if scope_type == "field_path":
        return await _resolve_field_path(scope, entity, session)
    elif scope_type == "active_context":
        return await _resolve_active_context(scope, entity, session)
    else:
        return None


async def _resolve_field_path(scope: dict, entity, session) -> ObjectId | None:
    """Traverse entity relationships to resolve an actor_id.

    E.g., path="organization.primary_owner_id" traverses
    entity.organization_id → Organization → primary_owner_id.
    """
    from kernel.db import ENTITY_REGISTRY

    path = scope["path"]
    parts = path.split(".")
    current_data = entity.model_dump(by_alias=True)

    for part in parts[:-1]:
        # This part is a relationship field — load the related entity
        related_id = current_data.get(part)
        if not related_id:
            return None
        # Determine the entity type from the field definition
        entity_cls = _resolve_entity_type_for_field(type(entity).__name__, part)
        if not entity_cls:
            return None
        related = await entity_cls.get(related_id)
        if not related:
            return None
        current_data = related.model_dump(by_alias=True)

    # Last part is the actor_id field
    actor_id = current_data.get(parts[-1])
    return ObjectId(actor_id) if actor_id else None


async def _resolve_active_context(scope: dict, entity, session) -> ObjectId | None:
    """Find actors with Attention records covering this entity.

    Full implementation in Phase 5. Returns None for now.
    """
    # Phase 5 activates this with Attention lookup
    return None


def _resolve_entity_type_for_field(entity_type_name: str, field_name: str):
    """Look up what entity type a relationship field points to."""
    from kernel.db import ENTITY_REGISTRY
    from kernel.entity.definition import EntityDefinition

    # For kernel entities, check model field annotations
    cls = ENTITY_REGISTRY.get(entity_type_name)
    if cls and getattr(cls, "_is_kernel_entity", False):
        # Kernel entities don't have EntityDefinitions
        # For now, return None — relationship resolution for kernel entities
        # uses explicit code in the callers
        return None

    # For domain entities, we'd need the EntityDefinition
    # This is async but we need it sync here — defer to caller
    return None
