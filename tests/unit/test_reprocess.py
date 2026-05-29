"""Tests for the reprocess primitive — Bug #10 backfill against new watches.

When a watch is added to a role, only future events fire it. Existing entities
are invisible to that role. `reprocess_for_role` re-emits a message for one
named entity, scoped to one named role, so newly-onboarded associates can
process previously-ingested data without manual intervention.

Pins:
  - happy path emits one message with the right shape (target_role, fresh
    correlation_id, causation_id, event_metadata.reprocess=True)
  - role with no watches on the entity type → ReprocessError with the
    entity type in the message so the operator sees what's missing
  - role with watches but not for the requested event_type → ReprocessError
    listing the role's actual events so the caller can pick the right one
  - default event_type is "created" (the most common backfill case)
  - effective_actor_id from contextvar flows into reprocess_requested_by
  - custom causation_id is preserved; auto-generated when not provided
  - scoped watch that resolves to None → ReprocessError ("empty scope")
  - multiple matching watches on the same role → uses highest context_depth

Mocks: get_cached_watches, MongoDBMessageBus.publish, _build_context, scope
resolution. The actual MongoDB insert is exercised by integration tests
against the dev cluster — these tests stay pure-unit.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from kernel.context import current_actor_id
from kernel.message.reprocess import ReprocessError, reprocess_for_role

# --- Fixtures ---


def _watch(event="created", conditions=None, scope=None, context_depth=1):
    return SimpleNamespace(
        event=event,
        conditions=conditions,
        scope=scope,
        context_depth=context_depth,
    )


def _entity(entity_cls_name="Meeting", entity_id=None, org_id=None):
    """Mock an entity instance with the minimum surface reprocess_for_role uses."""
    cls = type(entity_cls_name, (), {})
    instance = cls()
    instance.id = entity_id or ObjectId()
    instance.org_id = org_id or ObjectId()
    instance.model_dump = MagicMock(return_value={"_id": instance.id, "name": "Test"})
    return instance


@pytest.fixture
def mock_emit_helpers():
    """Stub _build_context + _build_summary so we don't traverse entity defs."""
    with (
        patch(
            "kernel.message.emit._build_context",
            new=AsyncMock(return_value={"meeting": {"name": "Test"}}),
        ),
        patch(
            "kernel.message.emit._build_summary",
            return_value={"display": "Meeting Test"},
        ),
    ):
        yield


@pytest.fixture
def mock_bus():
    """Capture the published message without writing to MongoDB.

    Also substitutes _build_message with a SimpleNamespace factory because
    Beanie's Message(...) constructor would call `get_motor_collection()`
    and raise CollectionWasNotInitialized in pure-unit context.
    """
    published = []

    def fake_build_message(**kwargs):
        return SimpleNamespace(id=ObjectId(), depth=0, **kwargs)

    async def fake_publish(self, message, session=None):
        published.append(message)

    with (
        patch("kernel.message.reprocess._build_message", new=fake_build_message),
        patch(
            "kernel.message.reprocess.MongoDBMessageBus.publish",
            new=fake_publish,
        ),
    ):
        yield published


# --- Happy path ---


@pytest.mark.asyncio
async def test_happy_path_emits_one_message(mock_emit_helpers, mock_bus):
    """Role with a created-watch → one message emitted with the right
    target_role, target_actor_id=None (no scope), event_type=created,
    event_metadata.reprocess=True."""
    entity = _entity()
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[{"watch": _watch(event="created"), "role_name": "synth"}],
    ):
        msg = await reprocess_for_role(entity, role_name="synth")

    assert len(mock_bus) == 1
    assert mock_bus[0] is msg
    assert msg.target_role == "synth"
    assert msg.target_actor_id is None
    assert msg.event_type == "created"
    assert msg.entity_type == "Meeting"
    assert msg.entity_id == entity.id
    assert msg.event_metadata["reprocess"] is True
    assert msg.event_metadata["reprocess_event_type"] == "created"


@pytest.mark.asyncio
async def test_default_event_type_is_created(mock_emit_helpers, mock_bus):
    """The most common backfill case — newly-added watch on `created`. Default
    avoids forcing every caller to spell it."""
    entity = _entity()
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[{"watch": _watch(event="created"), "role_name": "synth"}],
    ):
        msg = await reprocess_for_role(entity, role_name="synth")
    assert msg.event_type == "created"


@pytest.mark.asyncio
async def test_fresh_correlation_id_each_call(mock_emit_helpers, mock_bus):
    """Each reprocess starts a NEW chain (not a continuation of the original
    creation's chain — that one is closed). Two reprocesses → two distinct
    correlation_ids."""
    entity = _entity()
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[{"watch": _watch(), "role_name": "synth"}],
    ):
        m1 = await reprocess_for_role(entity, role_name="synth")
        m2 = await reprocess_for_role(entity, role_name="synth")
    assert m1.correlation_id != m2.correlation_id
    assert m1.correlation_id  # non-empty


@pytest.mark.asyncio
async def test_causation_id_default_marks_reprocess_origin(mock_emit_helpers, mock_bus):
    """When no causation_id is passed, the kernel generates a "reprocess:<hex>"
    sentinel so trace tools can immediately see the origin without joining
    against a separate audit table."""
    entity = _entity()
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[{"watch": _watch(), "role_name": "synth"}],
    ):
        msg = await reprocess_for_role(entity, role_name="synth")
    assert msg.causation_id is not None
    assert msg.causation_id.startswith("reprocess:")


@pytest.mark.asyncio
async def test_explicit_causation_id_preserved(mock_emit_helpers, mock_bus):
    """When the API endpoint or another caller passes a causation_id (e.g.
    the reprocess request's own correlation_id), use it verbatim."""
    entity = _entity()
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[{"watch": _watch(), "role_name": "synth"}],
    ):
        msg = await reprocess_for_role(
            entity, role_name="synth", causation_id="trace-abc-123"
        )
    assert msg.causation_id == "trace-abc-123"


@pytest.mark.asyncio
async def test_effective_actor_id_flows_to_event_metadata(mock_emit_helpers, mock_bus):
    """Whoever triggered the reprocess (a human via CLI, an associate via
    a chained command) becomes event_metadata.reprocess_requested_by — Bug #22's
    forensics property carries through into reprocess so we can answer
    'who reprocessed what.'"""
    entity = _entity()
    actor_id = ObjectId()
    token = current_actor_id.set(actor_id)
    try:
        with patch(
            "kernel.message.reprocess.get_cached_watches",
            return_value=[{"watch": _watch(), "role_name": "synth"}],
        ):
            msg = await reprocess_for_role(entity, role_name="synth")
    finally:
        current_actor_id.reset(token)
    assert msg.event_metadata["reprocess_requested_by"] == str(actor_id)


# --- Error paths ---


@pytest.mark.asyncio
async def test_role_has_no_watches_on_entity_type(mock_emit_helpers, mock_bus):
    """Reprocess to a role that has watches on OTHER entity types but not this
    one → ReprocessError listing the entity type so the operator can fix the
    role config."""
    entity = _entity(entity_cls_name="Meeting")
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[],  # No watches on Meeting for any role
    ):
        with pytest.raises(ReprocessError) as exc:
            await reprocess_for_role(entity, role_name="synth")
    assert "synth" in str(exc.value)
    assert "Meeting" in str(exc.value)
    assert mock_bus == []  # Nothing emitted


@pytest.mark.asyncio
async def test_role_has_watches_but_not_for_event_type(mock_emit_helpers, mock_bus):
    """Role watches Meeting:created but caller asks for event_type=transitioned
    → ReprocessError listing the role's actual watch events. Surfacing the
    ACTUAL events lets the caller fix the request without trial and error."""
    entity = _entity()
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[{"watch": _watch(event="created"), "role_name": "synth"}],
    ):
        with pytest.raises(ReprocessError) as exc:
            await reprocess_for_role(entity, role_name="synth", event_type="transitioned")
    detail = str(exc.value)
    assert "transitioned" in detail
    assert "created" in detail  # Lists the actual events available
    assert mock_bus == []


@pytest.mark.asyncio
async def test_other_role_watch_doesnt_satisfy_request(mock_emit_helpers, mock_bus):
    """Even if SOMEONE has a Meeting:created watch, only the named role's
    watches count — we don't broadcast across roles. This is the
    role-scoped-not-broadcast guarantee."""
    entity = _entity()
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[
            {"watch": _watch(event="created"), "role_name": "different_role"},
        ],
    ):
        with pytest.raises(ReprocessError):
            await reprocess_for_role(entity, role_name="synth")


# --- Scope handling ---


@pytest.mark.asyncio
async def test_scoped_watch_resolves_to_actor(mock_emit_helpers, mock_bus):
    """Scoped watch with a resolvable target → message gets target_actor_id set,
    same as organic emission. The scope resolution path is shared with
    save_tracked's path."""
    entity = _entity()
    actor_id = ObjectId()
    with (
        patch(
            "kernel.message.reprocess.get_cached_watches",
            return_value=[
                {
                    "watch": _watch(scope={"type": "field_path", "path": "owner_id"}),
                    "role_name": "synth",
                }
            ],
        ),
        patch(
            "kernel.watch.scope.resolve_scope",
            new=AsyncMock(return_value=actor_id),
        ),
    ):
        msg = await reprocess_for_role(entity, role_name="synth")
    assert msg.target_actor_id == actor_id


@pytest.mark.asyncio
async def test_scoped_watch_resolves_to_none_raises(mock_emit_helpers, mock_bus):
    """Scoped watch + scope resolves to None (no actor matches) → ReprocessError.
    Don't silently emit a message no one will claim."""
    entity = _entity()
    with (
        patch(
            "kernel.message.reprocess.get_cached_watches",
            return_value=[
                {
                    "watch": _watch(scope={"type": "field_path", "path": "owner_id"}),
                    "role_name": "synth",
                }
            ],
        ),
        patch(
            "kernel.watch.scope.resolve_scope",
            new=AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(ReprocessError) as exc:
            await reprocess_for_role(entity, role_name="synth")
    assert "scope" in str(exc.value).lower() or "empty" in str(exc.value).lower()
    assert mock_bus == []


# --- Multiple matching watches ---


@pytest.mark.asyncio
async def test_multiple_matching_watches_uses_max_context_depth(mock_emit_helpers, mock_bus):
    """Rare but possible — same role, two watches on the same entity_type+event
    with different context_depth values. Pick the deepest so the receiving
    actor's context satisfies whichever one fires."""
    entity = _entity()
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[
            {"watch": _watch(event="created", context_depth=1), "role_name": "synth"},
            {"watch": _watch(event="created", context_depth=3), "role_name": "synth"},
        ],
    ):
        # Mock _build_context to record what depth got passed.
        called_with = []

        async def fake_build_context(entity, depth, session):
            called_with.append(depth)
            return {"meeting": {"name": "Test"}}

        with patch("kernel.message.emit._build_context", new=fake_build_context):
            await reprocess_for_role(entity, role_name="synth")

    assert called_with == [3]  # Picked the deepest


# --- Targeting fidelity (single-role guarantee) ---


@pytest.mark.asyncio
async def test_only_one_message_emitted_even_with_other_roles_watching(
    mock_emit_helpers, mock_bus
):
    """Role-scoped, not broadcast. Even if 5 other roles watch this entity
    type, reprocess to ONE role emits ONE message."""
    entity = _entity()
    with patch(
        "kernel.message.reprocess.get_cached_watches",
        return_value=[
            {"watch": _watch(), "role_name": "role_a"},
            {"watch": _watch(), "role_name": "role_b"},
            {"watch": _watch(), "role_name": "synth"},
            {"watch": _watch(), "role_name": "role_d"},
            {"watch": _watch(), "role_name": "role_e"},
        ],
    ):
        await reprocess_for_role(entity, role_name="synth")
    assert len(mock_bus) == 1
    assert mock_bus[0].target_role == "synth"
