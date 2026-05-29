"""Reprocess primitive — re-emit a message for an existing entity (Bug #10).

When a watch is added to a role, only future entity changes fire it. Entities
that existed before the watch was added are invisible to that role. This module
provides the kernel-level operation to fire a watch retroactively against a
specific existing entity, scoped to ONE named role (not broadcast).

Why role-scoped, not broadcast: re-emitting to every role with a matching
watch would duplicate work (other roles already saw the original `created`
event when the entity was first inserted). The operator picks which role
should "see" the entity for the first time — typically a newly-added
associate's role processing previously-ingested data.

Provenance: every reprocessed message carries:
  - a fresh `correlation_id` (this is a NEW execution chain, not a replay
    of the original; the new chain's downstream effects need their own
    cascade tree)
  - `causation_id` linking back to the reprocess request (so the trace
    shows reprocess origin distinctly from organic creation)
  - `event_metadata.reprocess = True` so the receiving actor and trace
    tools know to treat this as a backfill, not a fresh creation
  - `event_metadata.reprocess_requested_by` = the actor_id of whoever
    triggered the reprocess (threaded from contextvar / API layer)

The receiving actor MUST be idempotent — same as all message handlers.
Multiple reprocesses against the same entity create separate messages with
separate correlation_ids; the handler is expected to detect already-done
work via entity state, not message dedup.
"""
from __future__ import annotations

import uuid
from typing import Optional

from kernel.context import current_actor_id
from kernel.message.mongodb_bus import MongoDBMessageBus
from kernel.message.schema import Message
from kernel.observability.tracing import create_span
from kernel.watch.cache import get_cached_watches


class ReprocessError(ValueError):
    """Raised when a reprocess request can't be fulfilled (e.g. the named role
    has no watch matching the requested entity_type+event_type). Subclass of
    ValueError so the API layer can map to 400 cleanly."""


def _watch_matches(watch_event: str, requested_event: str) -> bool:
    """True if a role's watch.event matches the reprocess request's event_type.

    Reprocess only emits one event at a time, so we're matching watch.event
    against a concrete event_type the caller asked for (e.g. "created",
    "transitioned", "transitioned:active", "method:classify"). Plain equality
    handles the common case; the wildcard forms (`transitioned` matches both
    `transitioned` and `transitioned:<state>`) are intentionally NOT relaxed
    here — the caller must be specific so the receiving actor sees the same
    event_type it would have seen organically.
    """
    return watch_event == requested_event


async def reprocess_for_role(
    entity,
    role_name: str,
    event_type: str = "created",
    causation_id: Optional[str] = None,
    session=None,
) -> Message:
    """Emit one message for `entity` targeting actors in `role_name`.

    Args:
        entity: an already-loaded entity instance (kernel or domain).
        role_name: the role whose watch should fire. The role MUST have a watch
            matching this entity's type AND the requested event_type.
        event_type: which event to simulate. Defaults to "created" since that's
            the most common backfill case (newly-onboarded associate processing
            previously-ingested entities).
        causation_id: optional parent message id; defaults to a fresh
            "reprocess:<uuid>" sentinel so the trace shows a clear origin
            even when the request came from a CLI or API call (not from
            another message).
        session: optional MongoDB session for transactional emission.

    Returns:
        The created Message.

    Raises:
        ReprocessError: if `role_name` has no watch matching this entity type
            and event_type. Surface listing the role's actual watches so the
            caller can pick the right event_type or fix the role config.
    """
    org_id = str(entity.org_id)
    entity_type_name = type(entity).__name__

    with create_span(
        "message.reprocess",
        entity_type=entity_type_name,
        target_role=role_name,
        event_type=event_type,
    ):
        # Find the requested role's watch among the cached watches for this
        # entity type. The cache is keyed (org_id, entity_type) -> list of
        # {watch, role_name}, so we filter to the role and check for an event
        # match. If the role has no watch on this entity type at all, the
        # filter returns empty.
        candidates = [
            wi
            for wi in get_cached_watches(org_id, entity_type_name)
            if wi["role_name"] == role_name
        ]
        if not candidates:
            raise ReprocessError(
                f"Role {role_name!r} has no watches on entity type "
                f"{entity_type_name!r}. Add one first via "
                f"`indemn role add-watch {role_name} --entity {entity_type_name} "
                f"--on {event_type}`."
            )
        matching = [wi for wi in candidates if _watch_matches(wi["watch"].event, event_type)]
        if not matching:
            available = sorted({wi["watch"].event for wi in candidates})
            raise ReprocessError(
                f"Role {role_name!r} has watches on {entity_type_name!r} for "
                f"events {available} but not for {event_type!r}. Use one of "
                f"the available events or add a new watch."
            )

        # Use the highest-context-depth watch among matches when there are
        # multiple watches on the same role for the same event (rare). The
        # context payload then satisfies any of them.
        winning = max(matching, key=lambda wi: wi["watch"].context_depth)
        watch = winning["watch"]

        # Build context using the same helper save_tracked uses for organic
        # emissions, so the receiving actor sees the same shape regardless
        # of whether this is a reprocess or a real creation.
        from kernel.message.emit import _build_context, _build_summary

        context = await _build_context(entity, watch.context_depth, session)

        # Resolve scope if the watch has one. Same semantics as organic
        # emission: a scoped watch that resolves to None means "no actor in
        # this role currently owns this entity," which is a real signal that
        # the reprocess can't be fulfilled — surface it as ReprocessError so
        # the operator sees what happened.
        target_actor_id = None
        if watch.scope:
            from kernel.watch.scope import resolve_scope

            target_actor_id = await resolve_scope(watch.scope, entity, session)
            if target_actor_id is None:
                raise ReprocessError(
                    f"Watch on role {role_name!r} for {entity_type_name!r} is "
                    f"scoped ({watch.scope.get('type')}) and no actor matched "
                    f"for this entity. Cannot reprocess into an empty scope."
                )

        # Fresh correlation_id — this is a NEW chain. The reprocess origin is
        # captured in event_metadata + causation_id, not by reusing the
        # original creation's correlation_id (which may be days old and
        # already-completed in the trace store).
        correlation_id = uuid.uuid4().hex
        causation = causation_id or f"reprocess:{uuid.uuid4().hex[:12]}"

        actor_id = current_actor_id.get()
        event_metadata: dict = {
            "reprocess": True,
            "reprocess_requested_by": str(actor_id) if actor_id else None,
            "reprocess_event_type": event_type,
        }

        message = _build_message(
            org_id=entity.org_id,
            entity_type=entity_type_name,
            entity_id=entity.id,
            event_type=event_type,
            target_role=role_name,
            target_actor_id=target_actor_id,
            correlation_id=correlation_id,
            causation_id=causation,
            event_metadata=event_metadata,
            context=context,
            summary=_build_summary(entity, event_type),
        )

        bus = MongoDBMessageBus()
        await bus.publish(message, session=session)
        return message


def _build_message(**kwargs) -> Message:
    """Factory for the Message Beanie Document.

    Wrapped in a function so unit tests can substitute it with a lightweight
    record (Beanie's `Message(**kwargs)` requires `init_beanie()` to have
    set up the MongoDB collection at import time, which we don't want in
    pure-unit tests). The kwargs are exactly what the production Message
    constructor takes; this helper just forwards them.
    """
    return Message(depth=0, **kwargs)
