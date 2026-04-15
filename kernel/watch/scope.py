"""Scope resolution for watches.

Two resolution types:
- field_path: traverses entity relationships to resolve to an actor_id
- active_context: looks up Attention records for real-time event delivery [G-48]

Scope is resolved at emit time. The kernel writes target_actor_id on the message.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId


async def resolve_scope(
    scope: dict, entity, session
) -> ObjectId | list[ObjectId] | None:
    """Resolve a watch scope to target actor(s).

    Returns actor ObjectId, list of ObjectIds, or None (message not created).
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
    entity.organization_id -> Organization -> primary_owner_id.
    """
    path = scope["path"]
    parts = path.split(".")
    current_data = entity.model_dump(by_alias=True)

    for part in parts[:-1]:
        # This part is a relationship field — load the related entity.
        # Support dot-notation shorthand: "organization" resolves to
        # field "organization_id" (the _id suffix is implicit).
        related_id = current_data.get(part)
        field_name = part
        if related_id is None:
            # Try with _id suffix (spec shorthand)
            related_id = current_data.get(part + "_id")
            if related_id is not None:
                field_name = part + "_id"
        if not related_id:
            return None
        entity_cls = _resolve_entity_type_for_field(
            type(entity).__name__, field_name,
        )
        if not entity_cls:
            return None
        related = await entity_cls.get(related_id)
        if not related:
            return None
        current_data = related.model_dump(by_alias=True)

    # Last part is the actor_id field
    actor_id = current_data.get(parts[-1])
    return ObjectId(actor_id) if actor_id else None


async def _resolve_active_context(
    scope: dict, entity, session
) -> ObjectId | list[ObjectId] | None:
    """Find actors with Attention records covering this entity. [G-48]

    Looks up active Attention records where the target_entity or
    related_entities includes this entity (or a related entity via traversal).
    """
    from kernel_entities.attention import Attention

    traverses = scope.get("traverses")
    if traverses:
        related_id = getattr(entity, traverses, None)
    else:
        related_id = entity.id

    if not related_id:
        return None

    # Query Attention for active records matching this entity
    now = datetime.now(timezone.utc)
    attentions = await Attention.find({
        "status": "active",
        "expires_at": {"$gt": now},
        "$or": [
            {"target_entity.id": related_id},
            {"related_entities.id": related_id},
        ],
    }).to_list()

    if not attentions:
        return None

    # Return all matching actors (emit.py creates one message per actor)
    actor_ids = list({a.actor_id for a in attentions})
    if len(actor_ids) == 1:
        return actor_ids[0]
    return actor_ids


def _resolve_entity_type_for_field(entity_type_name: str, field_name: str):
    """Look up what entity type a relationship field points to.

    For kernel entities: infer from field name conventions (e.g., org_id -> Organization).
    For domain entities: use the EntityDefinition's relationship_target.
    """
    from kernel.db import ENTITY_REGISTRY

    cls = ENTITY_REGISTRY.get(entity_type_name)
    if not cls:
        return None

    # Kernel entity — use naming convention
    if getattr(cls, "_is_kernel_entity", False):
        return _infer_entity_from_field_name(field_name)

    # Domain entity — check EntityDefinition for relationship_target
    # This requires async lookup, so we use a sync heuristic first
    return _infer_entity_from_field_name(field_name)


def _infer_entity_from_field_name(field_name: str):
    """Infer entity type from field name convention.

    E.g., 'organization_id' -> Organization, 'actor_id' -> Actor.
    """
    from kernel.db import ENTITY_REGISTRY

    # Strip common suffixes
    base = field_name
    for suffix in ("_id", "_ids"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break

    # Convert snake_case to PascalCase
    pascal = "".join(part.capitalize() for part in base.split("_"))

    return ENTITY_REGISTRY.get(pascal)
