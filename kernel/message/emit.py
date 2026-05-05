"""Watch evaluation and message emission.

When an entity saves, this module evaluates all watches for that entity type
and creates messages for matching watches. Runs inside the save_tracked()
transaction.
"""

from kernel.message.mongodb_bus import MongoDBMessageBus
from kernel.message.schema import Message
from kernel.observability.tracing import create_span
from kernel.watch.cache import get_cached_watches
from kernel.watch.evaluator import evaluate_condition
from kernel.watch.scope import resolve_scope


async def evaluate_watches_and_emit(
    entity,
    event_type: str,
    event_metadata: dict,
    correlation_id: str,
    depth: int,
    parent_entity_type: str,
    causation_message_id: str = None,
    session=None,
) -> list[Message]:
    """Evaluate watches for this entity type and create messages for matches.
    Returns list of created Message objects (for optimistic dispatch in Phase 2)."""
    created_messages = []

    with create_span("watch.evaluate", entity_type=type(entity).__name__):
        org_id = str(entity.org_id)
        entity_type_name = type(entity).__name__
        entity_data = entity.model_dump(by_alias=True)

        watches = get_cached_watches(org_id, entity_type_name)

        for watch_info in watches:
            watch = watch_info["watch"]
            role_name = watch_info["role_name"]

            # Check event type match
            if not _event_matches(watch.event, event_type, event_metadata):
                continue

            # Check conditions (entity data + event metadata merged)
            if watch.conditions:
                eval_data = {**entity_data, **(event_metadata or {})}
                if not evaluate_condition(watch.conditions, eval_data):
                    continue

            # Resolve scope (if present)
            target_actor_id = None
            if watch.scope:
                resolved = await resolve_scope(watch.scope, entity, session)
                if resolved is None:
                    continue  # Scope couldn't resolve — skip this watch
                target_actor_id = resolved

            # Build context at configured depth
            context = await _build_context(entity, watch.context_depth, session)

            # Create message [OTEL span per vision § 14]
            with create_span(
                "message.create",
                entity_type=entity_type_name,
                target_role=role_name,
                event_type=event_type,
            ):
                pass  # span wraps message creation for tracing

            message = Message(
                org_id=entity.org_id,
                entity_type=entity_type_name,
                entity_id=entity.id,
                event_type=event_type,
                target_role=role_name,
                target_actor_id=target_actor_id,
                correlation_id=correlation_id,
                causation_id=causation_message_id,
                depth=depth + 1,
                event_metadata=event_metadata or {},
                context=context,
                summary=_build_summary(entity, event_type),
            )

            bus = MongoDBMessageBus()
            await bus.publish(message, session=session)
            created_messages.append(message)

    return created_messages


def _event_matches(watch_event: str, actual_event: str, metadata: dict) -> bool:
    """Check if the watch event matches the actual event."""
    if watch_event == actual_event:
        return True
    # "fields_changed" watches fire on method_invoked when fields changed
    if watch_event == "fields_changed" and actual_event == "method_invoked":
        return bool(metadata.get("fields_changed"))
    # "method_invoked" watches can match specific methods via "method:classify"
    if watch_event.startswith("method:") and actual_event == "method_invoked":
        return metadata.get("method") == watch_event.split(":", 1)[1]
    # "transitioned" watches can match specific target states via "transitioned:active"
    if watch_event.startswith("transitioned:") and actual_event == "transitioned":
        target = watch_event.split(":", 1)[1]
        return metadata.get("state_transition", {}).get("to") == target
    return False


async def _build_context(entity, depth: int, session) -> dict:
    """Build entity context at the specified depth.
    Depth 1: just the entity. Depth 2: + directly related entities."""
    context = {type(entity).__name__.lower(): _serialize_for_context(entity)}
    if depth <= 1:
        return context

    # Follow relationship fields to load related entities
    from kernel.db import ENTITY_REGISTRY
    from kernel.entity.definition import EntityDefinition

    entity_data = entity.model_dump(by_alias=True)
    defn = await EntityDefinition.find_one({"name": type(entity).__name__})
    if defn:
        for field_name, field_def in defn.fields.items():
            if field_def.is_relationship and field_def.relationship_target:
                related_id = entity_data.get(field_name)
                if related_id and field_def.relationship_target in ENTITY_REGISTRY:
                    related_cls = ENTITY_REGISTRY[field_def.relationship_target]
                    related = await related_cls.get(related_id)
                    if related:
                        key = field_def.relationship_target.lower()
                        context[key] = _serialize_for_context(related)
    return context


def _serialize_for_context(entity) -> dict:
    """Serialize entity for message context (exclude large fields)."""
    data = entity.model_dump(by_alias=True)
    # Exclude potentially large fields
    data.pop("data", None)  # Flexible data can be large
    data.pop("_loaded_state", None)
    return data


async def _build_related_entities(entity, depth: int) -> list[dict]:
    """Build the list of entities related to `entity` for the API
    `?include_related=true` response.

    Walks BOTH directions:
      * Forward — fields on `entity`'s own EntityDefinition where
        `is_relationship=true`. Loads the target entity by id.
      * Reverse — fields on OTHER EntityDefinitions whose
        `relationship_target == type(entity).__name__`. Queries each source
        collection for entities pointing at this one.

    Different shape from `_build_context` (which the watch-emit path uses to
    enrich messages and keys by lowercase target-entity-name). The API needs
    a flat list because (a) reverse refs can produce many entities of the
    same type for one source entity (a Company has many Touchpoints), and
    (b) consumers need to know HOW each entity is related — both the
    direction and the field that declared the relationship — to navigate
    the constellation.

    Each returned dict carries the entity's serialized fields plus three
    metadata keys leading-underscored to avoid collision with entity fields:
      * `_entity_type`            — name of the related entity's type
      * `_relationship_direction` — "forward" or "reverse"
      * `_via_field`              — the field name on whichever entity
                                    declares the relationship (always lives
                                    on the SOURCE side of the ref)

    Self-relationships (e.g. `Proposal.supersedes -> Proposal`) appear as
    BOTH a forward ref (the entity I supersede) and a reverse ref (the
    entity that supersedes me) without duplicating the current entity into
    its own related list.

    Limitation (MVP): domain entities pointing at a kernel entity DO
    appear; kernel entities pointing at this entity do NOT (no
    EntityDefinition row to walk). Adding Pydantic-class introspection to
    cover that path is a follow-on if it surfaces in practice.

    Depth contract matches `_build_context`: depth <= 1 returns []
    (entity-only); depth >= 2 returns directly-related entities. Higher
    depths are not yet implemented; capping at 2 matches the
    `--include-related` design intent in the Phase 0+1 spec.
    """
    if depth <= 1:
        return []

    from kernel.api.serialize import to_dict
    from kernel.db import ENTITY_REGISTRY
    from kernel.entity.definition import EntityDefinition

    related: list[dict] = []
    entity_type_name = type(entity).__name__

    # ---- Forward refs: walk this entity's own definition ----
    own_defn = await EntityDefinition.find_one({"name": entity_type_name})
    if own_defn:
        entity_data = entity.model_dump(by_alias=True)
        for field_name, field_def in own_defn.fields.items():
            # Standard fixed-target relationship
            if field_def.is_relationship and field_def.relationship_target:
                target_name = field_def.relationship_target
                related_id = entity_data.get(field_name)
                if not related_id:
                    continue
                target_cls = ENTITY_REGISTRY.get(target_name)
                if target_cls is None:
                    continue
                target_entity = await target_cls.get(related_id)
                if target_entity is None:
                    continue
                d = to_dict(target_entity)
                d["_entity_type"] = target_name
                d["_relationship_direction"] = "forward"
                d["_via_field"] = field_name
                related.append(d)
            # Polymorphic relationship — target type resolved at runtime
            elif (
                getattr(field_def, "is_polymorphic_relationship", False)
                and getattr(field_def, "target_type_field", None)
            ):
                related_id = entity_data.get(field_name)
                if not related_id:
                    continue
                target_name = entity_data.get(field_def.target_type_field)
                if not target_name:
                    continue
                target_cls = ENTITY_REGISTRY.get(target_name)
                if target_cls is None:
                    continue
                target_entity = await target_cls.get(related_id)
                if target_entity is None:
                    continue
                d = to_dict(target_entity)
                d["_entity_type"] = target_name
                d["_relationship_direction"] = "forward"
                d["_via_field"] = field_name
                d["_polymorphic"] = True
                related.append(d)

    # ---- Reverse refs: walk every other EntityDefinition for fields pointing here ----
    all_defns = await EntityDefinition.find_all().to_list()
    for source_defn in all_defns:
        source_cls = ENTITY_REGISTRY.get(source_defn.name)
        if source_cls is None:
            continue
        for field_name, field_def in source_defn.fields.items():
            if not (field_def.is_relationship and field_def.relationship_target):
                continue
            if field_def.relationship_target != entity_type_name:
                continue
            # Self-relationship — exclude the entity itself from its own
            # reverse-ref list (the forward walk already surfaced what it
            # supersedes; only OTHER instances of this type that point
            # at us are reverse refs).
            query = {field_name: entity.id}
            if source_defn.name == entity_type_name:
                query["_id"] = {"$ne": entity.id}
            inbound = await source_cls.find_scoped(query).to_list()
            for inbound_entity in inbound:
                d = to_dict(inbound_entity)
                d["_entity_type"] = source_defn.name
                d["_relationship_direction"] = "reverse"
                d["_via_field"] = field_name
                related.append(d)

    return related


def _build_summary(entity, event_type: str) -> dict:
    """Build a minimal summary for queue display."""
    return {
        "entity_type": type(entity).__name__,
        "event_type": event_type,
        "display": f"{type(entity).__name__} {getattr(entity, 'name', str(entity.id))}",
    }
