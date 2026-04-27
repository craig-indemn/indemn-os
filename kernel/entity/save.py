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
