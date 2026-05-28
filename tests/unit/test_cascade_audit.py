"""Tests for _emit_cascade_audit + cascade_nullify_references refactor.

Per Session-35 Decision D5 (consolidated plan Stage A4): cascade nullification
of inbound refs emits one ChangeRecord per affected entity (replaces the prior
`update_many` shortcut that wrote no audit). Each record has:
- change_type = "cascade_nullify"
- method = "relationship_target_deleted"
- method_metadata = {deleted_entity_type, deleted_entity_id, affected_field_names}
- changes = [FieldChange(field=..., old_value=..., new_value=None), ...]

Per D9 polymorphic pair atomicity: `_emit_cascade_audit.fields_changed`
accepts a list so polymorphic refs (id-field + type-field) can be captured
in ONE ChangeRecord. Stage B B2 uses this for polymorphic cascade.

Tests pin:
- _emit_cascade_audit signature accepts list of FieldChanges (D9-ready)
- Single call writes one ChangeRecord with change_type='cascade_nullify'
- method_metadata structure matches spec
- old_value captures pre-cascade field value
- Hash chain uses get_previous_hash + compute_hash
- Polymorphic pair: 2 FieldChanges in one record (D9 atomicity)
- cascade_nullify_references emits per-entity audit (no update_many shortcut)
- cascade_nullify_references uses logger (NOT undefined `log`) — latent bug fixed
"""

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId


@pytest.fixture
def mock_cascade_deps():
    """Patch kernel deps used by _emit_cascade_audit."""
    with patch(
        "kernel.entity.save.current_correlation_id",
        MagicMock(get=MagicMock(return_value="corr-cascade-1")),
    ), patch(
        "kernel.entity.save.current_effective_actor_id",
        MagicMock(get=MagicMock(return_value="eff-actor-cascade-1")),
    ), patch(
        "kernel.context.current_actor_id",
        MagicMock(get=MagicMock(return_value="actor-cascade-1")),
    ):
        yield


@pytest.mark.asyncio
async def test_emit_cascade_audit_writes_single_changerecord(mock_cascade_deps):
    """_emit_cascade_audit writes one ChangeRecord per call."""
    from kernel.entity.save import _emit_cascade_audit

    recorded = {}
    insert_calls = []

    def cr_factory(**kwargs):
        recorded.update(kwargs)
        m = MagicMock()
        async def fake_insert(session=None):
            insert_calls.append(kwargs)
        m.insert = fake_insert
        m.id = kwargs.get("id")
        return m

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev-h"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="cur-h",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = cr_factory

        record_id = await _emit_cascade_audit(
            affected_entity_id=ObjectId("aaaa11112222333344445555"),
            affected_entity_type="Task",
            fields_changed=[{"field": "touchpoint", "old_value": ObjectId("bbbb11112222333344445555"), "new_value": None}],
            deleted_target_type="Touchpoint",
            deleted_target_id=ObjectId("bbbb11112222333344445555"),
            org_id=ObjectId("ccc11112222333344445555c"),
        )

    assert isinstance(record_id, ObjectId)
    assert len(insert_calls) == 1


@pytest.mark.asyncio
async def test_emit_cascade_audit_change_type_is_cascade_nullify(mock_cascade_deps):
    """ChangeRecord constructed with change_type='cascade_nullify' (D5)."""
    from kernel.entity.save import _emit_cascade_audit

    recorded = {}

    def cr_factory(**kwargs):
        recorded.update(kwargs)
        m = MagicMock(insert=AsyncMock())
        m.id = kwargs.get("id")
        return m

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = cr_factory

        await _emit_cascade_audit(
            affected_entity_id=ObjectId(),
            affected_entity_type="Task",
            fields_changed=[{"field": "touchpoint", "old_value": ObjectId(), "new_value": None}],
            deleted_target_type="Touchpoint",
            deleted_target_id=ObjectId(),
            org_id=ObjectId(),
        )

    assert recorded["change_type"] == "cascade_nullify"


@pytest.mark.asyncio
async def test_emit_cascade_audit_method_and_metadata(mock_cascade_deps):
    """method='relationship_target_deleted' + method_metadata structure pinned."""
    from kernel.entity.save import _emit_cascade_audit

    recorded = {}

    def cr_factory(**kwargs):
        recorded.update(kwargs)
        m = MagicMock(insert=AsyncMock())
        m.id = kwargs.get("id")
        return m

    deleted_tp_id = ObjectId("bbbb11112222333344445555")

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = cr_factory

        await _emit_cascade_audit(
            affected_entity_id=ObjectId(),
            affected_entity_type="Task",
            fields_changed=[{"field": "touchpoint", "old_value": deleted_tp_id, "new_value": None}],
            deleted_target_type="Touchpoint",
            deleted_target_id=deleted_tp_id,
            org_id=ObjectId(),
        )

    assert recorded["method"] == "relationship_target_deleted"
    md = recorded["method_metadata"]
    assert md["deleted_entity_type"] == "Touchpoint"
    assert md["deleted_entity_id"] == str(deleted_tp_id)
    assert md["affected_field_names"] == ["touchpoint"]


@pytest.mark.asyncio
async def test_emit_cascade_audit_old_value_captures_pre_cascade(mock_cascade_deps):
    """FieldChange.old_value = the field's value before cascade (the deleted target id)."""
    from kernel.entity.save import _emit_cascade_audit

    recorded = {}

    def cr_factory(**kwargs):
        recorded.update(kwargs)
        m = MagicMock(insert=AsyncMock())
        m.id = kwargs.get("id")
        return m

    pre_value = ObjectId("bbbb11112222333344445555")

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = cr_factory

        await _emit_cascade_audit(
            affected_entity_id=ObjectId(),
            affected_entity_type="Task",
            fields_changed=[{"field": "touchpoint", "old_value": pre_value, "new_value": None}],
            deleted_target_type="Touchpoint",
            deleted_target_id=pre_value,
            org_id=ObjectId(),
        )

    fcs = recorded["changes"]
    assert len(fcs) == 1
    assert fcs[0].field == "touchpoint"
    assert fcs[0].old_value == pre_value
    assert fcs[0].new_value is None


@pytest.mark.asyncio
async def test_emit_cascade_audit_hash_chain_set(mock_cascade_deps):
    """previous_hash + current_hash set on the record via get_previous_hash + compute_hash."""
    from kernel.entity.save import _emit_cascade_audit

    recorded_instance = None
    get_prev = AsyncMock(return_value="seed-cascade-prev")

    def cr_factory(**kwargs):
        nonlocal recorded_instance
        m = MagicMock(insert=AsyncMock())
        m.id = kwargs.get("id")
        recorded_instance = m
        return m

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=get_prev,
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="cur-cascade-hash",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = cr_factory

        await _emit_cascade_audit(
            affected_entity_id=ObjectId(),
            affected_entity_type="Task",
            fields_changed=[{"field": "touchpoint", "old_value": ObjectId(), "new_value": None}],
            deleted_target_type="Touchpoint",
            deleted_target_id=ObjectId(),
            org_id=ObjectId("ccc11112222333344445555c"),
        )

    assert recorded_instance.previous_hash == "seed-cascade-prev"
    assert recorded_instance.current_hash == "cur-cascade-hash"


@pytest.mark.asyncio
async def test_emit_cascade_audit_polymorphic_pair_atomicity(mock_cascade_deps):
    """D9: polymorphic pair (id-field + type-field) in ONE record with 2 FieldChanges.

    When Touchpoint's source_entity_id + source_entity_type both need clearing
    (e.g., the referenced Email gets deleted), passing both as 2 FieldChanges
    in one _emit_cascade_audit call produces a SINGLE ChangeRecord — atomic
    at the record level. Stage B B2 uses this for polymorphic cascade.
    """
    from kernel.entity.save import _emit_cascade_audit

    recorded = {}

    def cr_factory(**kwargs):
        recorded.update(kwargs)
        m = MagicMock(insert=AsyncMock())
        m.id = kwargs.get("id")
        return m

    deleted_email_id = ObjectId("eeee11112222333344445555")

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = cr_factory

        await _emit_cascade_audit(
            affected_entity_id=ObjectId("aaaa11112222333344445555"),
            affected_entity_type="Touchpoint",
            fields_changed=[
                {"field": "source_entity_id", "old_value": deleted_email_id, "new_value": None},
                {"field": "source_entity_type", "old_value": "Email", "new_value": None},
            ],
            deleted_target_type="Email",
            deleted_target_id=deleted_email_id,
            org_id=ObjectId(),
        )

    # Both FieldChanges in the SAME ChangeRecord (D9 atomicity)
    fcs = recorded["changes"]
    assert len(fcs) == 2
    field_names = {fc.field for fc in fcs}
    assert field_names == {"source_entity_id", "source_entity_type"}
    # method_metadata records both affected fields
    assert set(recorded["method_metadata"]["affected_field_names"]) == {
        "source_entity_id", "source_entity_type",
    }


@pytest.mark.asyncio
async def test_emit_cascade_audit_accepts_fieldchange_instances(mock_cascade_deps):
    """fields_changed can be list of FieldChange instances OR list of dicts."""
    from kernel.entity.save import _emit_cascade_audit
    from kernel.changes.collection import FieldChange

    recorded = {}

    def cr_factory(**kwargs):
        recorded.update(kwargs)
        m = MagicMock(insert=AsyncMock())
        m.id = kwargs.get("id")
        return m

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = cr_factory

        # Pass FieldChange instances directly
        fc = FieldChange(field="touchpoint", old_value=ObjectId(), new_value=None)
        await _emit_cascade_audit(
            affected_entity_id=ObjectId(),
            affected_entity_type="Task",
            fields_changed=[fc],
            deleted_target_type="Touchpoint",
            deleted_target_id=ObjectId(),
            org_id=ObjectId(),
        )

    fcs = recorded["changes"]
    assert len(fcs) == 1
    assert fcs[0].field == "touchpoint"


def test_emit_cascade_audit_signature():
    """Shape pin: _emit_cascade_audit signature matches consolidated plan A4."""
    from kernel.entity.save import _emit_cascade_audit

    assert inspect.iscoroutinefunction(_emit_cascade_audit)
    sig = inspect.signature(_emit_cascade_audit)
    params = list(sig.parameters.keys())
    # Required positional names per the plan
    assert "affected_entity_id" in params
    assert "affected_entity_type" in params
    assert "fields_changed" in params
    assert "deleted_target_type" in params
    assert "deleted_target_id" in params
    assert "actor_id" in params


def test_cascade_nullify_references_uses_emit_cascade_audit_per_entity():
    """Source pin: cascade_nullify_references calls _emit_cascade_audit per affected entity.

    Replaces the prior `update_many` shortcut that wrote NO audit (and had a
    latent `log.info` typo bug — `log` was never defined, only `logger`).
    """
    from kernel.entity import save

    src = inspect.getsource(save.cascade_nullify_references)
    # Helper called inside the loop
    assert "await _emit_cascade_audit(" in src
    # update_many shortcut is gone
    assert "update_many(\n                {\"org_id\": org_id, field_name: entity_id}" not in src


def test_cascade_nullify_references_uses_logger_not_undefined_log():
    """Source pin: latent bug fixed — `logger.info` not `log.info`.

    The previous cascade_nullify_references used `log.info(...)` at the
    modified_count > 0 log line, but no `log` symbol was defined in save.py
    (only `logger`). Any successful cascade would have raised NameError —
    explaining why the cascade-modified-count path was silent in practice.
    """
    from kernel.entity import save

    src = inspect.getsource(save.cascade_nullify_references)
    assert "logger.info" in src
    assert "log.info(" not in src


@pytest.mark.asyncio
async def test_cascade_nullify_references_emits_per_entity_via_helper(mock_cascade_deps):
    """End-to-end: cascade_nullify_references with N affected entities → N _emit_cascade_audit calls + N update_one calls."""
    from kernel.entity import save

    # Mock EntityDefinition.find().to_list() to return one defn with one
    # relationship_target field pointing at "Touchpoint"
    defn = MagicMock()
    defn.name = "Task"
    field_def = MagicMock()
    field_def.is_relationship = True
    field_def.relationship_target = "Touchpoint"
    defn.fields = {"touchpoint": field_def}

    # Mock ENTITY_REGISTRY
    entity_cls = MagicMock()
    affected_ids = [ObjectId(), ObjectId(), ObjectId()]
    deleted_tp_id = ObjectId("bbbb11112222333344445555")
    affected_docs = [
        {"_id": aid, "touchpoint": deleted_tp_id} for aid in affected_ids
    ]

    # find() returns a query that has to_list returning affected_docs
    find_query = MagicMock()
    find_query.to_list = AsyncMock(return_value=affected_docs)
    collection = MagicMock(
        find=MagicMock(return_value=find_query),
        update_one=AsyncMock(),
    )
    entity_cls.get_motor_collection = MagicMock(return_value=collection)

    emit_calls = []

    async def fake_emit(**kwargs):
        emit_calls.append(kwargs)
        return ObjectId()

    with patch(
        "kernel.entity.definition.EntityDefinition.find",
    ) as mock_find_defs, patch(
        "kernel.db.ENTITY_REGISTRY",
        {"Task": entity_cls},
    ), patch(
        "kernel.entity.save._emit_cascade_audit",
        new=fake_emit,
    ):
        find_defs_query = MagicMock()
        find_defs_query.to_list = AsyncMock(return_value=[defn])
        mock_find_defs.return_value = find_defs_query

        org_id = ObjectId("ccc11112222333344445555c")
        total = await save.cascade_nullify_references(
            entity_type="Touchpoint",
            entity_id=deleted_tp_id,
            org_id=org_id,
        )

    # 3 affected entities → 3 audit emissions + 3 update_one calls
    assert total == 3
    assert len(emit_calls) == 3
    assert collection.update_one.await_count == 3

    # Each emit captures the right metadata
    for i, call in enumerate(emit_calls):
        assert call["affected_entity_type"] == "Task"
        assert call["affected_entity_id"] == affected_ids[i]
        assert call["deleted_target_type"] == "Touchpoint"
        assert call["deleted_target_id"] == deleted_tp_id
        assert call["org_id"] == org_id
        # fields_changed is a list with one FieldChange (scalar ref)
        assert len(call["fields_changed"]) == 1
        fc = call["fields_changed"][0]
        assert fc["field"] == "touchpoint"
        assert fc["old_value"] == deleted_tp_id
        assert fc["new_value"] is None
