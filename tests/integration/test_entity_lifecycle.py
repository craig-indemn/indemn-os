"""Integration tests: Entity lifecycle — CRUD + state machine + computed fields.

Acceptance tests covered:
  #1 ENTITY DEFINITION → DYNAMIC CLASS
  #2 ENTITY CRUD + STATE MACHINE
  #17 COMPUTED FIELDS
"""

import pytest

from kernel.changes.collection import ChangeRecord
from kernel.entity.definition import (
    ComputedFieldDef,
    EntityDefinition,
    FieldDefinition,
)
from kernel.entity.factory import create_entity_class
from kernel.entity.state_machine import StateMachineError


@pytest.mark.asyncio
async def test_dynamic_entity_crud(db, org_id, actor):
    """#2: Create, read, update an entity. Changes tracked."""
    # Create a domain entity definition
    defn = EntityDefinition(
        org_id=org_id,
        name="TestSubmission",
        collection_name="test_submissions",
        fields={
            "named_insured": FieldDefinition(type="str", required=True),
            "status": FieldDefinition(
                type="str",
                enum_values=["received", "triaging", "closed"],
                is_state_field=True,
                default="received",
            ),
            "lob": FieldDefinition(type="str"),
        },
        state_machine={
            "received": ["triaging"],
            "triaging": ["closed"],
        },
    )
    await defn.insert()

    # Create dynamic class from definition
    SubmissionCls = create_entity_class(defn)

    # Set database reference for domain entity Motor operations
    SubmissionCls._db_ref = db

    # Create instance
    sub = SubmissionCls(
        org_id=org_id,
        named_insured="Acme Corp",
        status="received",
        lob="GL",
    )
    await sub.save_tracked(actor_id=str(actor.id), method="create")

    assert sub.id is not None
    assert sub.version == 2  # Incremented from 1 to 2 on save

    # Read back
    loaded = await SubmissionCls.get(sub.id)
    assert loaded.named_insured == "Acme Corp"
    assert loaded.status == "received"
    assert loaded.lob == "GL"

    # Update
    loaded.lob = "WC"
    await loaded.save_tracked(actor_id=str(actor.id))
    assert loaded.version == 3

    # Verify changes recorded
    changes = await ChangeRecord.find({"entity_id": sub.id}).sort("-timestamp").to_list()
    assert len(changes) >= 2  # create + update
    assert changes[0].change_type == "update"


@pytest.mark.asyncio
async def test_state_machine_enforcement(db, org_id, actor):
    """#2: State machine enforces valid transitions."""
    defn = EntityDefinition(
        org_id=org_id,
        name="TestEmail",
        collection_name="test_emails",
        fields={
            "subject": FieldDefinition(type="str"),
            "status": FieldDefinition(
                type="str",
                enum_values=["received", "classified", "processed"],
                is_state_field=True,
                default="received",
            ),
        },
        state_machine={
            "received": ["classified"],
            "classified": ["processed"],
        },
    )
    await defn.insert()

    EmailCls = create_entity_class(defn)
    EmailCls._db_ref = db

    email = EmailCls(org_id=org_id, subject="Test", status="received")
    await email.save_tracked(actor_id=str(actor.id), method="create")

    # Valid transition
    email.transition_to("classified")
    assert email.status == "classified"
    await email.save_tracked(actor_id=str(actor.id), method="transition")

    # Invalid transition — can't skip to processed from received
    email2 = EmailCls(org_id=org_id, subject="Test 2", status="received")
    await email2.save_tracked(actor_id=str(actor.id), method="create")
    with pytest.raises(StateMachineError, match="Cannot transition"):
        email2.transition_to("processed")


@pytest.mark.asyncio
async def test_computed_fields(db, org_id, actor):
    """#17: Computed field auto-populated on save."""
    defn = EntityDefinition(
        org_id=org_id,
        name="TestTask",
        collection_name="test_tasks",
        fields={
            "stage": FieldDefinition(
                type="str",
                enum_values=["received", "processing", "done"],
                is_state_field=True,
                default="received",
            ),
            "ball_holder": FieldDefinition(type="str"),
        },
        state_machine={
            "received": ["processing"],
            "processing": ["done"],
        },
        computed_fields={
            "ball_holder": ComputedFieldDef(
                source_field="stage",
                mapping={"received": "queue", "processing": "team", "done": "nobody"},
            ),
        },
    )
    await defn.insert()

    TaskCls = create_entity_class(defn)
    TaskCls._db_ref = db

    task = TaskCls(org_id=org_id, stage="received")
    await task.save_tracked(actor_id=str(actor.id), method="create")

    assert task.ball_holder == "queue"

    # Transition and verify computed field updates
    task.transition_to("processing")
    await task.save_tracked(actor_id=str(actor.id), method="transition")
    assert task.ball_holder == "team"
