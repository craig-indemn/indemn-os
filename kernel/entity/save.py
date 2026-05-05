"""save_tracked() — the critical transaction.

The ONLY save path for all entities. In one MongoDB transaction:
1. Optimistic concurrency check (version field)
2. Computed field evaluation
3. Flexible data validation
4. Entity write
5. Changes collection record (with hash chain)
6. Selective emission: watch evaluation → message creation

This is non-negotiable. ALL entity modifications go through this.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from bson import ObjectId

from kernel.changes.collection import write_change_record
from kernel.context import (
    current_causation_message_id,
    current_correlation_id,
    current_depth,
    current_effective_actor_id,
)
from kernel.entity.computed import evaluate_computed_fields
from kernel.entity.flexible import validate_flexible_data
from kernel.message.emit import evaluate_watches_and_emit
from kernel.message.event_metadata import build_event_metadata
from kernel.observability.tracing import create_span

MAX_CASCADE_DEPTH = 10

logger = logging.getLogger(__name__)


def _resolve_created_by(actor_id: str) -> str:
    """The identity to record on an entity's `created_by` at insert time.

    Bug #27: pre-fix the field was always None on every entity in dev
    because save_tracked never touched it. Auto-populate now reads:

      1. `current_effective_actor_id` — the associate the harness is
         running as (set by the X-Effective-Actor-Id header per Bug #22).
         Preferred because it matches the changes-collection's
         `effective_actor_id` field — same convention, same per-associate
         forensics granularity.
      2. `actor_id` — the authenticated session identity (e.g. the
         human user's actor_id, or the service-token actor for
         non-harness machine callers).

    Caller-set values are NOT overwritten (seed data / migrations may
    carry authoritative provenance like 'imported from Apollo').
    """
    return current_effective_actor_id.get() or actor_id


class VersionConflictError(Exception):
    """Raised when optimistic concurrency check fails."""

    pass


async def save_tracked_impl(entity, actor_id: str, **kwargs):
    """The ONLY save path. Returns list of created messages for optimistic dispatch."""
    method = kwargs.get("method")
    method_metadata = kwargs.get("method_metadata")
    correlation_id = kwargs.get("correlation_id") or current_correlation_id.get() or str(uuid4())
    depth = kwargs.get("depth", current_depth.get())

    with create_span(
        "entity.save_tracked",
        entity_type=type(entity).__name__,
        entity_id=str(entity.id) if entity.id else "new",
    ):
        # Detect heartbeat-only updates on Attention
        if _is_heartbeat_only(entity):
            await _save_heartbeat_only(entity)
            return []

        # Compute computed fields
        evaluate_computed_fields(entity)

        # Validate flexible data
        if hasattr(entity, "data") and entity.data:
            errors = await validate_flexible_data(entity, entity.data)
            if errors:
                raise ValueError(f"Flexible data validation failed: {errors}")

        # Compute field-level changes
        is_new = entity.id is None
        changes = _compute_changes(entity) if not is_new else []

        # Auto-populate created_by on insert (Bug #27).
        if is_new and hasattr(entity, "created_by") and entity.created_by is None:
            entity.created_by = _resolve_created_by(actor_id)

        # Update metadata
        entity.updated_at = datetime.now(timezone.utc)
        expected_version = entity.version
        entity.version += 1

        # Determine if this save should emit messages (selective emission)
        should_emit, event_type = _should_emit(entity, is_new, method, changes)

        # Build event metadata
        event_meta = build_event_metadata(entity, method, changes) if should_emit else None

        # Check cascade depth
        if depth > MAX_CASCADE_DEPTH:
            from kernel.message.schema import Message

            await Message(
                org_id=entity.org_id,
                entity_type=type(entity).__name__,
                entity_id=entity.id or ObjectId(),
                event_type="circuit_broken",
                target_role="__circuit_broken__",
                correlation_id=correlation_id,
                depth=depth,
                status="circuit_broken",
            ).insert()
            logger.warning("Cascade depth %d exceeded for %s", depth, type(entity).__name__)
            return []

        # Kernel entity cascade guard
        if entity._is_kernel_entity and depth > 0:
            parent_entity_type = kwargs.get("parent_entity_type")
            if parent_entity_type == type(entity).__name__:
                logger.warning(
                    "Blocked self-referencing cascade on kernel entity %s",
                    type(entity).__name__,
                )
                should_emit = False  # Save succeeds but no cascade

        # Start MongoDB transaction
        client = entity.get_motor_collection().database.client
        created_messages = []
        try:
            async with await client.start_session() as session:
                async with session.start_transaction():
                    if is_new:
                        # Insert
                        if not entity.id:
                            entity.id = ObjectId()
                        await entity.get_motor_collection().insert_one(
                            _serialize_entity(entity), session=session
                        )
                    else:
                        # Update with optimistic concurrency
                        result = await entity.get_motor_collection().update_one(
                            {"_id": entity.id, "version": expected_version},
                            {"$set": _serialize_entity(entity)},
                            session=session,
                        )
                        if result.modified_count == 0:
                            raise VersionConflictError(
                                f"{type(entity).__name__} {entity.id} was modified concurrently"
                            )

                    # Write changes record (INSIDE transaction for atomicity).
                    # effective_actor_id flows from the auth-middleware contextvar
                    # so we capture which associate (not just which token) did this.
                    await write_change_record(
                        entity=entity,
                        change_type="create" if is_new else "update",
                        actor_id=actor_id,
                        changes=changes,
                        method=method,
                        method_metadata=method_metadata,
                        correlation_id=correlation_id,
                        session=session,
                        effective_actor_id=current_effective_actor_id.get(),
                    )

                    # Evaluate watches and emit messages (INSIDE transaction)
                    if should_emit:
                        created_messages = await evaluate_watches_and_emit(
                            entity=entity,
                            event_type=event_type,
                            event_metadata=event_meta,
                            correlation_id=correlation_id,
                            depth=depth,
                            parent_entity_type=type(entity).__name__,
                            causation_message_id=current_causation_message_id.get(),
                            session=session,
                        )
        except Exception:
            # Restore entity version on failure so retries use correct expected_version
            entity.version = expected_version
            raise

        # Update loaded state for next change tracking
        entity._loaded_state = _serialize_entity(entity)

        # Invalidate watch cache when Role entities change
        if type(entity).__name__ == "Role":
            from kernel.watch.cache import invalidate_watch_cache

            await invalidate_watch_cache()

        # Return created messages for optimistic dispatch (Phase 2)
        return created_messages


async def bulk_save_tracked(
    entities: list,
    actor_id: str,
    method: str = None,
    correlation_id: str = None,
) -> dict:
    """Bulk insert path for new entities — same audit + watch contracts as save_tracked_impl.

    Optimized for creation-only (fetch_new ingestion). Replaces the sequential
    per-entity save_tracked loop with batched operations:
      1. Single Pydantic validation pass (construction)
      2. insert_many(ordered=False) for entities
      3. In-memory hash-chained change records via second insert_many
      4. Batched watch evaluation + grouped message insert_many
      5. Partial failure preserved — per-row error collection

    Returns: {"succeeded": int, "errored": int, "errors": list, "created_ids": list}
    """
    if not entities:
        return {"succeeded": 0, "errored": 0, "errors": [], "created_ids": []}

    import time

    from kernel.changes.collection import ChangeRecord, FieldChange
    from kernel.changes.hash_chain import compute_hash, get_previous_hash

    _correlation_id = correlation_id or current_correlation_id.get() or str(uuid4())
    effective_actor = current_effective_actor_id.get()
    causation_msg = current_causation_message_id.get()
    depth = current_depth.get()
    now = datetime.now(timezone.utc)

    with create_span(
        "entity.bulk_save_tracked",
        entity_type=type(entities[0]).__name__,
        batch_size=len(entities),
    ):
        t0 = time.monotonic()

        # --- Phase 1: Prepare entities for insert ---
        entity_type_name = type(entities[0]).__name__
        collection = entities[0].get_motor_collection()
        org_id = entities[0].org_id

        docs_to_insert = []
        for entity in entities:
            if not entity.id:
                entity.id = ObjectId()
            entity.version = 1
            entity.updated_at = now
            evaluate_computed_fields(entity)
            if hasattr(entity, "created_by") and entity.created_by is None:
                entity.created_by = _resolve_created_by(actor_id)
            docs_to_insert.append(_serialize_entity(entity))

        # --- Phase 2: Bulk insert entities (ordered=False for partial failure) ---
        succeeded_entities = []
        errors = []
        try:
            result = await collection.insert_many(docs_to_insert, ordered=False)
            succeeded_entities = list(entities)
        except Exception as bulk_err:
            from pymongo.errors import BulkWriteError

            if isinstance(bulk_err, BulkWriteError):
                failed_indices = {
                    e["index"] for e in bulk_err.details.get("writeErrors", [])
                }
                for i, entity in enumerate(entities):
                    if i in failed_indices:
                        err_detail = next(
                            (
                                e
                                for e in bulk_err.details["writeErrors"]
                                if e["index"] == i
                            ),
                            {},
                        )
                        err_msg = err_detail.get("errmsg", str(bulk_err))
                        if "E11000" in err_msg or "duplicate key" in err_msg:
                            pass  # silent skip for dedup — not an error
                        else:
                            errors.append(
                                {
                                    "entity_id": str(entity.id),
                                    "external_ref": getattr(entity, "external_ref", None),
                                    "error": err_msg,
                                }
                            )
                    else:
                        succeeded_entities.append(entity)
            else:
                raise

        if not succeeded_entities:
            duration_ms = (time.monotonic() - t0) * 1000
            return {
                "succeeded": 0,
                "errored": len(errors),
                "errors": errors,
                "created_ids": [],
                "duration_ms": round(duration_ms, 1),
            }

        # --- Phase 3: Build change records with in-memory hash chain ---
        prev_hash = await get_previous_hash(org_id)
        change_records = []
        for entity in succeeded_entities:
            record = ChangeRecord(
                id=ObjectId(),
                org_id=entity.org_id,
                entity_type=entity_type_name,
                entity_id=entity.id,
                change_type="create",
                actor_id=actor_id,
                effective_actor_id=effective_actor,
                correlation_id=_correlation_id,
                changes=[],
                method=method,
                timestamp=now,
            )
            record.previous_hash = prev_hash
            record.current_hash = compute_hash(record)
            prev_hash = record.current_hash
            change_records.append(record)

        changes_coll = ChangeRecord.get_motor_collection()
        change_docs = [r.model_dump(by_alias=True) for r in change_records]
        if change_docs:
            await changes_coll.insert_many(change_docs)

        # --- Phase 4: Batched watch evaluation + message emission ---
        all_messages = []
        for entity in succeeded_entities:
            event_meta = build_event_metadata(entity, method, [])
            messages = await evaluate_watches_and_emit(
                entity=entity,
                event_type="created",
                event_metadata=event_meta,
                correlation_id=_correlation_id,
                depth=depth,
                parent_entity_type=entity_type_name,
                causation_message_id=causation_msg,
                session=None,
            )
            all_messages.extend(messages)

        duration_ms = (time.monotonic() - t0) * 1000
        created_ids = [str(e.id) for e in succeeded_entities]

        return {
            "succeeded": len(succeeded_entities),
            "errored": len(errors),
            "errors": errors,
            "created_ids": created_ids,
            "duration_ms": round(duration_ms, 1),
        }


def _serialize_entity(entity) -> dict:
    """Serialize entity to dict. Works for kernel (Beanie) and domain (Pydantic)."""
    data = entity.model_dump(by_alias=True)
    # pymongo cannot encode Decimal — convert to float before writing.
    _convert_decimals(data)
    return data


def _convert_decimals(obj):
    """Recursively convert Decimal values to float for BSON serialization."""
    if isinstance(obj, dict):
        for key in obj:
            if isinstance(obj[key], Decimal):
                obj[key] = float(obj[key])
            elif isinstance(obj[key], (dict, list)):
                _convert_decimals(obj[key])
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, Decimal):
                obj[i] = float(item)
            elif isinstance(item, (dict, list)):
                _convert_decimals(item)


def _compute_changes(entity) -> list[dict]:
    """Compare current state against loaded state to find field-level changes."""
    current = _serialize_entity(entity)
    loaded = entity._loaded_state
    changes = []
    for key in set(list(current.keys()) + list(loaded.keys())):
        if key in ("_id", "id", "revision_id", "version", "updated_at"):
            continue
        old_val = loaded.get(key)
        new_val = current.get(key)
        if old_val != new_val:
            changes.append({"field": key, "old_value": old_val, "new_value": new_val})
    return changes


def _should_emit(entity, is_new: bool, method: str, changes: list) -> tuple[bool, str]:
    """Determine if this save should evaluate watches and create messages.

    Selective emission: only creation, deletion, state transitions,
    and @exposed method invocations.
    Priority: creation > transition > method > no emission.
    """
    if is_new:
        return True, "created"
    # Transition takes precedence over method
    if hasattr(entity, "_pending_transition") and entity._pending_transition:
        return True, "transitioned"
    if method:
        return True, "method_invoked"
    return False, ""


def _is_heartbeat_only(entity) -> bool:
    """Detect heartbeat-only updates on Attention and Runtime entities.

    Per white paper: 'Heartbeat updates bypass audit logging to avoid noise.'
    """
    entity_name = type(entity).__name__
    if entity_name not in ("Attention", "Runtime"):
        return False
    loaded = entity._loaded_state
    if not loaded:
        return False
    current = _serialize_entity(entity)
    changed_fields = {k for k in current if current.get(k) != loaded.get(k)}
    changed_fields -= {"version", "updated_at"}
    if entity_name == "Attention":
        return changed_fields <= {"last_heartbeat", "expires_at"}
    if entity_name == "Runtime":
        return changed_fields <= {"instances", "last_heartbeat"}
    return False


async def _save_heartbeat_only(entity):
    """Fast path for heartbeat updates — skip changes + watches."""
    update_fields: dict = {"updated_at": datetime.now(timezone.utc)}
    if hasattr(entity, "last_heartbeat"):
        update_fields["last_heartbeat"] = datetime.now(timezone.utc)
    if hasattr(entity, "expires_at") and entity.expires_at:
        update_fields["expires_at"] = entity.expires_at
    if hasattr(entity, "instances") and entity.instances is not None:
        update_fields["instances"] = [
            inst if isinstance(inst, dict) else inst.model_dump()
            for inst in entity.instances
        ]
    await entity.get_motor_collection().update_one(
        {"_id": entity.id},
        {"$set": update_fields},
    )


async def cascade_nullify_references(entity_type: str, entity_id, org_id) -> int:
    """Nullify relationship fields on other entities that reference a deleted entity.

    Scans all EntityDefinitions for fields where is_relationship=True and
    relationship_target matches the deleted entity's type. For each, runs
    update_many to set matching references to null.

    Returns total number of documents updated across all collections.
    """
    from kernel.db import ENTITY_REGISTRY
    from kernel.entity.definition import EntityDefinition

    definitions = await EntityDefinition.find({"org_id": org_id}).to_list()
    total_updated = 0

    for defn in definitions:
        for field_name, field_def in defn.fields.items():
            if not field_def.is_relationship:
                continue
            if field_def.relationship_target != entity_type:
                continue

            entity_cls = ENTITY_REGISTRY.get(defn.name)
            if entity_cls is None:
                continue

            collection = entity_cls.get_motor_collection()
            result = await collection.update_many(
                {"org_id": org_id, field_name: entity_id},
                {"$set": {field_name: None}},
            )
            if result.modified_count > 0:
                log.info(
                    "cascade_nullify: %s.%s — nullified %d refs to deleted %s %s",
                    defn.name,
                    field_name,
                    result.modified_count,
                    entity_type,
                    entity_id,
                )
                total_updated += result.modified_count

    return total_updated
