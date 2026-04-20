"""Integration tests: Entity enable capability + modify definition.

Tests the ability to activate capabilities and modify field definitions
on existing entity types.
"""

import pytest

from kernel.entity.definition import (
    CapabilityActivation,
    EntityDefinition,
    FieldDefinition,
)
from kernel.entity.factory import create_entity_class


@pytest.mark.asyncio
async def test_enable_capability(db, org_id, actor):
    """Enable stale_check on an entity and verify it's stored."""
    defn = EntityDefinition(
        org_id=org_id,
        name="TestActionItem",
        collection_name="test_action_items",
        fields={
            "description": FieldDefinition(type="str", required=True),
            "status": FieldDefinition(
                type="str",
                enum_values=["open", "completed"],
                is_state_field=True,
                default="open",
            ),
            "is_overdue": FieldDefinition(type="bool", default=False),
        },
        state_machine={"open": ["completed"]},
    )
    await defn.insert()

    # Enable stale_check
    config = {
        "when": {
            "all": [
                {"field": "status", "op": "equals", "value": "open"},
            ]
        },
        "sets_field": "is_overdue",
        "sets_value": True,
    }
    defn.activated_capabilities.append(
        CapabilityActivation(capability="stale_check", config=config)
    )
    defn.version += 1
    await defn.save()

    # Reload and verify
    loaded = await EntityDefinition.get(defn.id)
    assert len(loaded.activated_capabilities) == 1
    assert loaded.activated_capabilities[0].capability == "stale_check"
    assert loaded.activated_capabilities[0].config["sets_field"] == "is_overdue"


@pytest.mark.asyncio
async def test_stale_check_on_entity(db, org_id, actor):
    """stale_check capability evaluates correctly on a domain entity."""
    from kernel.capability.stale_check import stale_check

    defn = EntityDefinition(
        org_id=org_id,
        name="TestTask2",
        collection_name="test_tasks2",
        fields={
            "status": FieldDefinition(
                type="str",
                enum_values=["open", "completed"],
                is_state_field=True,
                default="open",
            ),
            "is_overdue": FieldDefinition(type="bool", default=False),
            "followup_count": FieldDefinition(type="int", default=0),
        },
        state_machine={"open": ["completed"]},
        activated_capabilities=[
            CapabilityActivation(
                capability="stale_check",
                config={
                    "when": {
                        "all": [
                            {"field": "status", "op": "equals", "value": "open"},
                            {"field": "followup_count", "op": "gte", "value": 2},
                        ]
                    },
                    "sets_field": "is_overdue",
                    "sets_value": True,
                },
            )
        ],
    )
    await defn.insert()

    TaskCls = create_entity_class(defn)
    TaskCls._db_ref = db

    # Create entity that meets conditions
    task = TaskCls(
        org_id=org_id,
        status="open",
        is_overdue=False,
        followup_count=3,
    )
    await task.save_tracked(actor_id=str(actor.id), method="create")

    # Run stale_check
    cap_config = defn.activated_capabilities[0].config
    result = await stale_check(task, cap_config, org_id)

    assert result["matched"] is True
    assert result["result"] == {"is_overdue": True}

    # Create entity that does NOT meet conditions
    task2 = TaskCls(
        org_id=org_id,
        status="open",
        is_overdue=False,
        followup_count=0,
    )
    await task2.save_tracked(actor_id=str(actor.id), method="create")

    result2 = await stale_check(task2, cap_config, org_id)
    assert result2["matched"] is False


@pytest.mark.asyncio
async def test_modify_add_field(db, org_id, actor):
    """Add a field to an existing entity definition."""
    defn = EntityDefinition(
        org_id=org_id,
        name="TestCompany",
        collection_name="test_companies",
        fields={
            "name": FieldDefinition(type="str", required=True),
        },
    )
    await defn.insert()

    # Add a field
    defn.fields["industry"] = FieldDefinition(type="str")
    defn.version += 1
    await defn.save()

    loaded = await EntityDefinition.get(defn.id)
    assert "industry" in loaded.fields
    assert loaded.fields["industry"].type == "str"
    assert loaded.version == 2


@pytest.mark.asyncio
async def test_modify_remove_field(db, org_id, actor):
    """Remove a field from an entity definition."""
    defn = EntityDefinition(
        org_id=org_id,
        name="TestCompany2",
        collection_name="test_companies2",
        fields={
            "name": FieldDefinition(type="str", required=True),
            "legacy_field": FieldDefinition(type="str"),
        },
    )
    await defn.insert()

    # Remove field
    del defn.fields["legacy_field"]
    defn.version += 1
    await defn.save()

    loaded = await EntityDefinition.get(defn.id)
    assert "legacy_field" not in loaded.fields
