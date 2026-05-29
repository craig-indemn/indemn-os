"""Temporal activities — kernel-side functions workflows call.

Agent execution (process_with_associate) moved to harness images (G2.3).
Kernel activities: message lifecycle, human decisions, bulk operations.
"""

import logging

from bson import ObjectId
from temporalio import activity

from kernel.context import current_org_id
from kernel.db import ENTITY_REGISTRY
from kernel.message.mongodb_bus import MongoDBMessageBus
from kernel.message.schema import Message
from kernel.observability.tracing import create_span

logger = logging.getLogger(__name__)


class PermanentProcessingError(Exception):
    """Non-retryable processing error."""

    pass


class SkillTamperError(Exception):
    """Skill content hash mismatch."""

    pass


class BulkAbortError(Exception):
    """Raised in abort mode when any entity in a bulk batch fails."""

    pass


# --- Core message lifecycle activities ---


@activity.defn
async def claim_message(message_id: str, actor_id: str) -> dict | None:
    """Atomic claim via findOneAndUpdate. Returns message data or None if already claimed."""
    with create_span("activity.claim_message", message_id=message_id, actor_id=actor_id):
        bus = MongoDBMessageBus()
        msg = await bus.claim_by_id(message_id, ObjectId(actor_id))
        if not msg:
            return None
        return {
            "id": str(msg.id),
            "entity_type": msg.entity_type,
            "entity_id": str(msg.entity_id),
            "correlation_id": msg.correlation_id or "",
            "depth": getattr(msg, "depth", 0),
            "target_role": msg.target_role,
        }


@activity.defn
async def load_actor(actor_id: str) -> dict:
    """Load minimal actor config for workflow dispatch routing."""
    from kernel_entities.actor import Actor

    actor = await Actor.get(ObjectId(actor_id))
    if not actor:
        raise ValueError(f"Actor {actor_id} not found")
    return {
        "id": str(actor.id),
        "type": actor.type,
        "runtime_id": str(actor.runtime_id) if actor.runtime_id else None,
        "status": actor.status,
    }


@activity.defn
async def load_entity_context(message_id: str) -> dict:
    """Load the entity referenced by the message. Fresh from MongoDB."""
    message = await Message.get(ObjectId(message_id))
    if not message:
        return {"message": None, "entity": None}

    entity_cls = ENTITY_REGISTRY.get(message.entity_type)
    entity = None
    if entity_cls:
        entity = await entity_cls.get(message.entity_id)

    return {
        "message": message.model_dump(),
        "entity": entity.model_dump() if entity else None,
    }


# process_with_associate DELETED (G2.3, 2026-04-17).
# Agent execution now runs outside the kernel trust boundary in harness images.
# See: harnesses/async-deepagents/main.py


@activity.defn
async def process_human_decision(message_id: str, decision: dict) -> dict:
    """Process a human's decision from the HumanReviewWorkflow."""
    message = await Message.get(ObjectId(message_id))
    if not message:
        return {"status": "message_not_found"}

    entity_cls = ENTITY_REGISTRY.get(message.entity_type)
    if not entity_cls:
        return {"status": "entity_type_not_found"}

    entity = await entity_cls.get(message.entity_id)
    if not entity:
        return {"status": "entity_not_found"}

    action = decision.get("action")  # approve, reject, escalate
    reason = decision.get("reason", "")

    if action == "approve":
        target = decision.get("target_state")
        if target and hasattr(entity, "_state_machine") and entity._state_machine:
            entity.transition_to(target, reason=reason)
            await entity.save_tracked(
                method="human_approve", method_metadata={"decision": decision}
            )
    elif action == "reject":
        target = decision.get("target_state")
        if target:
            entity.transition_to(target, reason=reason)
            await entity.save_tracked(method="human_reject", method_metadata={"decision": decision})

    return {"status": action, "entity_id": str(entity.id)}


@activity.defn
async def complete_message(message_id: str, result: dict) -> None:
    """Move message from queue to log."""
    with create_span("activity.complete_message", message_id=message_id):
        bus = MongoDBMessageBus()
        await bus.complete(ObjectId(message_id), result)


@activity.defn
async def fail_message(message_id: str, error: str) -> None:
    """Return message to queue or move to dead_letter."""
    with create_span("activity.fail_message", message_id=message_id):
        bus = MongoDBMessageBus()
        await bus.fail(ObjectId(message_id), error)


# --- Bulk operation activities ---


def _coerce_bulk_filter(entity_cls, entity_type: str, filter_query: dict) -> dict:
    """Apply parse_filter to a bulk activity's raw filter_query (Bug #23).

    The API boundary validates the filter shape (and 400s on bad input
    before the workflow starts), but typed values like bson.ObjectId and
    datetime don't cross the Temporal serialization boundary cleanly —
    so the workflow input carries the raw JSON-safe dict and the activity
    re-runs the parser to get MongoDB-typed values for find_scoped().

    On failure (which should be impossible if the API validated), raise
    PermanentProcessingError so Temporal doesn't retry a bad filter forever.
    """
    from fastapi import HTTPException

    from kernel.api._filter_safelist import parse_filter

    try:
        return parse_filter(entity_cls, entity_type, filter_query)
    except HTTPException as e:
        raise PermanentProcessingError(
            f"Invalid bulk filter for {entity_type}: {e.detail}"
        )


@activity.defn
async def process_bulk_batch(spec_dict: dict, offset: int) -> dict:
    """Process one batch of a bulk operation within a MongoDB transaction."""
    with create_span("activity.process_bulk_batch", offset=offset):
        from kernel.capability.registry import get_capability
        from kernel.entity.save import VersionConflictError
        from kernel.entity.state_machine import StateMachineError
        from kernel.temporal.workflows import BulkOperationSpec

        spec = BulkOperationSpec(**spec_dict)

        # Restore org_id + actor_id context — Temporal activities run in a fresh
        # contextvar scope, so both must be re-set from the spec for save-path
        # code (entity.save_tracked + bulk_delete_tracked + bulk_update_tracked)
        # to write ChangeRecord(actor_id: str) without failing Pydantic validation.
        if spec.org_id:
            current_org_id.set(ObjectId(spec.org_id))
        if spec.actor_id:
            from kernel.context import current_actor_id
            current_actor_id.set(spec.actor_id)

        entity_cls = ENTITY_REGISTRY.get(spec.entity_type)
        if not entity_cls:
            raise PermanentProcessingError(f"Entity type {spec.entity_type} not found")

        # Query entities
        # Bug #37 follow-on: skip_invalid=True so a single malformed row
        # (e.g., Email with `company` containing a stringified dict from
        # a pre-Bug-#9-fix associate run) doesn't abort the activity. The
        # valid rows iterate normally; malformed rows are handled below
        # by the DELETE-cleanup pass when the operator's filter targets
        # them for removal.
        #
        # Bug #4 follow-on (2026-05-25): `if spec.filter_query` is Python-truthy,
        # which treats filter_query={} as "no filter" and falls through to the
        # else branch — even when match_all=True opts in to match-everything-in-org
        # explicitly. Need to also accept the empty-filter-with-match_all case.
        # The API layer (registration.py:826) ALREADY rejects empty filter without
        # match_all for destructive ops, so by the time we get here, an empty
        # filter implies match_all is the operator's intent.
        use_filter = spec.filter_query is not None and (
            spec.filter_query or spec.match_all
        )
        if use_filter:
            typed_filter = _coerce_bulk_filter(
                entity_cls, spec.entity_type, spec.filter_query
            )
            entities = (
                await entity_cls.find_scoped(typed_filter)
                .skip(offset)
                .limit(spec.batch_size)
                .to_list(skip_invalid=True)
            )
        elif spec.source_data:
            entities = spec.source_data[offset : offset + spec.batch_size]
            typed_filter = None
        else:
            return {"done": True, "batch_processed": 0}

        # Bug #37 follow-on: when the filter matches malformed rows but
        # NO valid rows in this batch, skip_invalid above returned [].
        # For DELETE, fall through to the cleanup pass below so we can
        # still remove the malformed rows the operator targeted. For
        # other ops, there's nothing to do — return done.
        if not entities and spec.operation != "delete":
            return {"done": True, "batch_processed": 0, "total_count": offset}

        errors = []
        batch_processed = 0
        malformed_deleted = 0

        # ---------------- DELETE path (Stage A3 — D-C complete audit) ----------------
        # Per Session-35 Decision D-C: every delete emits a ChangeRecord with
        # pre-delete state snapshot. bulk_delete_tracked writes per-entity audit
        # records (in-memory hash-chained, single insert_many) then deletes via
        # single delete_many. Cascade nullification of inbound refs is per-entity
        # (Stage A4 refactors `cascade_nullify_references` to emit cascade audit
        # records via _emit_cascade_audit; in A3 cascade still uses the existing
        # update_many shortcut path).
        #
        # NOTE per Session 35 R1 F9: there is no @router.delete route in
        # registration.py. Single-entity deletes route through CLI → /api/{slug}/bulk
        # → BulkExecuteWorkflow delete branch, so this path covers BOTH single
        # and bulk delete cases.
        if spec.operation == "delete":
            if entities:
                from kernel.entity.save import bulk_delete_tracked, cascade_nullify_references

                for entity in entities:
                    await cascade_nullify_references(
                        spec.entity_type, entity.id, entity.org_id
                    )
                    activity.heartbeat(f"cascade: {entity.id}")

                delete_result = await bulk_delete_tracked(entities, actor_id=None)
                batch_processed = delete_result["succeeded"]

            # Bug #37 follow-on: cleanup pass for malformed rows. Entities that
            # failed Pydantic validation aren't in `entities` (skip_invalid=True
            # above). For DELETE, find their matched _ids in this batch's offset
            # window and delete_many them directly. Audit chain skipped (can't
            # compute changes from a doc that doesn't validate; accept the lossy
            # audit for the cleanup case).
            if typed_filter is not None:
                coll = entity_cls.get_motor_collection()
                matched_ids_cursor = (
                    coll.find(typed_filter, {"_id": 1})
                    .skip(offset)
                    .limit(spec.batch_size)
                )
                matched_ids = [doc["_id"] for doc in await matched_ids_cursor.to_list(length=None)]
                valid_ids = {e.id for e in entities}
                bad_ids = [_id for _id in matched_ids if _id not in valid_ids]
                if bad_ids:
                    result = await coll.delete_many({"_id": {"$in": bad_ids}})
                    malformed_deleted = result.deleted_count
                    for bad_id in bad_ids:
                        logger.warning(
                            "Bug #37 cleanup: bulk-delete removed malformed %s _id=%s "
                            "(audit chain skipped — entity failed Pydantic validation)",
                            spec.entity_type,
                            bad_id,
                        )

            total_count = offset + len(entities) + malformed_deleted
            done = (len(entities) + malformed_deleted) < spec.batch_size

            return {
                "done": done,
                "batch_processed": batch_processed + malformed_deleted,
                "total_count": total_count,
                "errors": errors,
            }

        # ---------------- UPDATE path (Stage A5 — D4 + D24 audit completion) ----------------
        # Per Session-35 D4: D-A audit scope expands to cover bulk-update path.
        # The prior update_one shortcut is removed; bulk updates now route through
        # bulk_update_tracked which emits per-entity ChangeRecord via in-memory
        # hash chain (D24: same audit shape for create + update + delete + cascade;
        # mirror bulk_save_tracked lines 312-337).
        #
        # Watch event emission: NO (locked Session-36 Dev#3 as audit-only).
        # bulk-update is administrative / migration (operator expects silent);
        # bulk-delete via bulk_delete_tracked is also silent (symmetric);
        # bulk-create via bulk_save_tracked IS noisy (ingestion path SHOULD
        # trigger downstream associates). See bulk_update_tracked docstring
        # for full rationale.
        if spec.operation == "update":
            if entities and spec.sets:
                from kernel.entity.save import bulk_update_tracked

                update_result = await bulk_update_tracked(
                    entities,
                    spec.sets,
                    method="bulk_update",
                    method_metadata={"bulk_operation_id": activity.info().workflow_id},
                )
                batch_processed = update_result["succeeded"]

            total_count = offset + len(entities)
            done = len(entities) < spec.batch_size

            return {
                "done": done,
                "batch_processed": batch_processed,
                "total_count": total_count,
                "errors": errors,
            }

        # ---------------- Non-delete-non-update path: per-entity transaction-wrapped loop ----------------
        # transition / method / create — atomic per batch via MongoDB transaction.
        from kernel.db import get_client

        mongo_client = get_client()
        async with await mongo_client.start_session() as session:
            async with session.start_transaction():
                for entity in entities:
                    try:
                        if spec.operation == "transition":
                            entity.transition_to(spec.target_state)
                            await entity.save_tracked(
                                method="bulk_transition",
                                method_metadata={"bulk_operation_id": activity.info().workflow_id},
                            )
                        elif spec.operation == "method":
                            cap_fn = get_capability(spec.method_name)
                            result = await cap_fn(entity, {}, entity.org_id)
                            if not result.get("needs_reasoning"):
                                for field, value in result.get("result", {}).items():
                                    setattr(entity, field, value)
                                await entity.save_tracked(
                                    method=spec.method_name,
                                    method_metadata={
                                        "rule_evaluation": result.get("rule_evaluation"),
                                        "bulk_operation_id": activity.info().workflow_id,
                                    },
                                )
                        elif spec.operation == "create":
                            org = ObjectId(spec.org_id) if spec.org_id else current_org_id.get()
                            new_entity = entity_cls(org_id=org, **entity)
                            await new_entity.save_tracked(method="bulk_create")

                        batch_processed += 1

                    except VersionConflictError:
                        # Transient — propagate for Temporal retry
                        raise
                    except (StateMachineError, ValueError, PermissionError) as e:
                        if spec.failure_mode == "abort":
                            raise BulkAbortError(str(e))
                        errors.append(
                            {
                                "entity_id": str(entity.id)
                                if hasattr(entity, "id")
                                else str(entity),
                                "error_type": type(e).__name__,
                                "message": str(e),
                            }
                        )

                    activity.heartbeat(f"batch progress: {batch_processed}")

        total_count = offset + len(entities)
        done = len(entities) < spec.batch_size

        return {
            "done": done,
            "batch_processed": batch_processed,
            "total_count": total_count,
            "errors": errors,
        }


@activity.defn
async def preview_bulk_operation(spec_dict: dict) -> dict:
    """Dry-run preview — count and sample affected entities. [G-81]"""
    from kernel.temporal.workflows import BulkOperationSpec

    spec = BulkOperationSpec(**spec_dict)

    # Restore org_id + actor_id context (same reason as process_bulk_batch).
    if spec.org_id:
        current_org_id.set(ObjectId(spec.org_id))
    if spec.actor_id:
        from kernel.context import current_actor_id
        current_actor_id.set(spec.actor_id)

    entity_cls = ENTITY_REGISTRY.get(spec.entity_type)
    if not entity_cls:
        return {"count": 0, "error": f"Entity type {spec.entity_type} not found"}

    # Bug #4 follow-on (2026-05-25): same truthy-check fix as process_bulk_batch.
    # Accept the empty-filter-with-match_all case so dry-runs report the actual
    # match count (instead of returning {"count": 0} and misleading the operator
    # into thinking nothing would be deleted).
    use_filter = spec.filter_query is not None and (
        spec.filter_query or spec.match_all
    )
    if use_filter:
        # Restore org_id context for find_scoped (same reason as process_bulk_batch)
        if spec.org_id:
            current_org_id.set(ObjectId(spec.org_id))
        typed_filter = _coerce_bulk_filter(entity_cls, spec.entity_type, spec.filter_query)
        count = await entity_cls.find_scoped(typed_filter).count()
        # Bug #37 follow-on: skip_invalid=True for the sample so a malformed
        # row matching the filter doesn't abort the dry-run preview. Sample
        # may underrepresent malformed rows, but the count above is honest
        # (count uses motor directly, not Pydantic).
        sample = await entity_cls.find_scoped(typed_filter).limit(5).to_list(skip_invalid=True)
        # Sample entities must be JSON-safe before crossing the Temporal data
        # converter — raw model_dump() returns ObjectId/datetime which choke
        # Pydantic v2's default JSON encoder. Use the API's to_dict() helper
        # which knows about ObjectId/datetime/Decimal. Latent bug exposed
        # by the org_id-context fix in this same change (Bug #32).
        from kernel.api.serialize import to_dict
        return {
            "count": count,
            "sample": [to_dict(e) for e in sample],
            "operation": spec.operation,
            "dry_run": True,
        }
    return {"count": 0}
