"""Integration tests: Watch evaluation → message creation.

Acceptance tests:
  #5 WATCH EVALUATION → MESSAGE
  #6 CONDITIONAL WATCH
  #12 SELECTIVE EMISSION
"""

import pytest

from kernel.entity.definition import EntityDefinition, FieldDefinition
from kernel.entity.factory import create_entity_class
from kernel.message.schema import Message
from kernel.watch.cache import load_watch_cache
from kernel_entities.role import Role, WatchDefinition


@pytest.mark.asyncio
async def test_watch_fires_on_entity_creation(db, org_id, actor):
    """#5: Role with watch → entity created → message in queue."""
    # Create entity definition
    defn = EntityDefinition(
        org_id=org_id,
        name="WatchTestItem",
        collection_name="watch_test_items",
        fields={
            "title": FieldDefinition(type="str", required=True),
            "status": FieldDefinition(
                type="str",
                is_state_field=True,
                default="new",
                enum_values=["new", "done"],
            ),
        },
        state_machine={"new": ["done"]},
    )
    await defn.insert()

    ItemCls = create_entity_class(defn)
    ItemCls._db_ref = db

    # Create role with watch on this entity type
    role = Role(
        org_id=org_id,
        name="watch_test_role",
        permissions={"read": ["WatchTestItem"], "write": ["WatchTestItem"]},
        watches=[
            WatchDefinition(entity_type="WatchTestItem", event="created"),
        ],
    )
    await role.insert()

    # Reload watch cache
    await load_watch_cache()

    # Create entity — should trigger watch → message
    item = ItemCls(org_id=org_id, title="Test Item")
    await item.save_tracked(actor_id=str(actor.id), method="create")

    # Check for message in queue
    messages = await Message.find({"entity_type": "WatchTestItem", "entity_id": item.id}).to_list()
    assert len(messages) == 1
    msg = messages[0]
    assert msg.event_type == "created"
    assert msg.target_role == "watch_test_role"
    assert msg.correlation_id is not None


@pytest.mark.asyncio
async def test_conditional_watch(db, org_id, actor):
    """#6: Condition false → no message. Condition true → message created."""
    defn = EntityDefinition(
        org_id=org_id,
        name="CondWatchItem",
        collection_name="cond_watch_items",
        fields={
            "priority": FieldDefinition(type="str", default="normal"),
            "status": FieldDefinition(
                type="str",
                is_state_field=True,
                default="new",
                enum_values=["new", "done"],
            ),
        },
        state_machine={"new": ["done"]},
    )
    await defn.insert()

    ItemCls = create_entity_class(defn)
    ItemCls._db_ref = db

    # Watch with condition: only fire for priority=high
    role = Role(
        org_id=org_id,
        name="cond_watch_role",
        permissions={"read": ["*"], "write": ["*"]},
        watches=[
            WatchDefinition(
                entity_type="CondWatchItem",
                event="created",
                conditions={"field": "priority", "op": "equals", "value": "high"},
            ),
        ],
    )
    await role.insert()
    await load_watch_cache()

    # Create normal priority — no message
    normal = ItemCls(org_id=org_id, priority="normal")
    await normal.save_tracked(actor_id=str(actor.id), method="create")
    msgs_normal = await Message.find({"entity_id": normal.id}).to_list()
    assert len(msgs_normal) == 0

    # Create high priority — message created
    high = ItemCls(org_id=org_id, priority="high")
    await high.save_tracked(actor_id=str(actor.id), method="create")
    msgs_high = await Message.find({"entity_id": high.id}).to_list()
    assert len(msgs_high) == 1
    assert msgs_high[0].target_role == "cond_watch_role"


@pytest.mark.asyncio
async def test_selective_emission(db, org_id, actor):
    """#12: Regular field update → no message. Transition → message. Creation → message."""
    defn = EntityDefinition(
        org_id=org_id,
        name="EmitTestItem",
        collection_name="emit_test_items",
        fields={
            "name": FieldDefinition(type="str"),
            "notes": FieldDefinition(type="str"),
            "status": FieldDefinition(
                type="str",
                is_state_field=True,
                default="draft",
                enum_values=["draft", "active", "closed"],
            ),
        },
        state_machine={"draft": ["active"], "active": ["closed"]},
    )
    await defn.insert()

    ItemCls = create_entity_class(defn)
    ItemCls._db_ref = db

    # Watch on all events
    role = Role(
        org_id=org_id,
        name="emit_test_role",
        permissions={"read": ["*"], "write": ["*"]},
        watches=[
            WatchDefinition(entity_type="EmitTestItem", event="created"),
            WatchDefinition(entity_type="EmitTestItem", event="transitioned"),
            WatchDefinition(entity_type="EmitTestItem", event="method_invoked"),
        ],
    )
    await role.insert()
    await load_watch_cache()

    # Creation → should emit
    item = ItemCls(org_id=org_id, name="Test", status="draft")
    await item.save_tracked(actor_id=str(actor.id), method="create")
    create_msgs = await Message.find({"entity_id": item.id, "event_type": "created"}).to_list()
    assert len(create_msgs) == 1

    # Regular field update (no method, no transition) → should NOT emit
    item.notes = "updated notes"
    await item.save_tracked(actor_id=str(actor.id))
    all_msgs = await Message.find({"entity_id": item.id}).to_list()
    assert len(all_msgs) == 1  # Still just the creation message

    # Transition → should emit
    item.transition_to("active")
    await item.save_tracked(actor_id=str(actor.id), method="transition")
    transition_msgs = await Message.find(
        {"entity_id": item.id, "event_type": "transitioned"}
    ).to_list()
    assert len(transition_msgs) == 1
