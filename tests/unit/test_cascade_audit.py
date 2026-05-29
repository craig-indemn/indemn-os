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


def test_cascade_nullify_references_uses_in_memory_batched_pattern():
    """Source pin: cascade_nullify_references uses in-memory batched pattern per D5 strict.

    Per Session-36 Dev#1 (D5 strict reading): cascade_nullify_references mirrors
    `bulk_save_tracked` lines 312-337 — builds all audit records in memory with
    hash chain, single insert_many for audits, per-(collection, field) batched
    update_many for nullifications. Earlier A4 implementation called
    `_emit_cascade_audit` per affected entity (N inserts + N update_ones)
    which deviated from D5.

    `_emit_cascade_audit` still exists as a single-record API for ad-hoc
    callers (and Stage B B2 polymorphic cascade).
    """
    from kernel.entity import save

    src = inspect.getsource(save.cascade_nullify_references)
    # In-memory chain construction
    assert "audit_records" in src
    assert "ChangeRecord(" in src  # inline construction, not via helper
    assert "compute_hash(record)" in src
    # Single insert_many at end
    assert "changes_coll.insert_many(change_docs)" in src
    # Batched per-(collection, field) update_many
    assert "nullify_ops" in src
    assert "collection.update_many" in src
    # update_one per-doc shortcut is gone
    assert "collection.update_one" not in src
    # Old pre-A4 update_many({org_id, field_name: entity_id}) shortcut is gone
    assert "update_many(\n                {\"org_id\": org_id, field_name: entity_id}" not in src
    # Old per-entity _emit_cascade_audit-in-the-loop pattern is gone
    assert "await _emit_cascade_audit(" not in src


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
async def test_cascade_nullify_references_batched_insert_and_update(mock_cascade_deps):
    """End-to-end: N affected entities → 1 insert_many for N audits + 1 update_many per (collection, field).

    Per Dev#1 (D5 strict reading): mirrors bulk_save_tracked lines 312-337.
    """
    from kernel.entity import save

    # Mock EntityDefinition with one relationship_target field
    defn = MagicMock()
    defn.name = "Task"
    field_def = MagicMock()
    field_def.is_relationship = True
    field_def.relationship_target = "Touchpoint"
    defn.fields = {"touchpoint": field_def}

    # Mock the affected entities and the entity class
    affected_ids = [ObjectId(), ObjectId(), ObjectId()]
    deleted_tp_id = ObjectId("bbbb11112222333344445555")
    affected_docs = [
        {"_id": aid, "touchpoint": deleted_tp_id} for aid in affected_ids
    ]

    find_query = MagicMock()
    find_query.to_list = AsyncMock(return_value=affected_docs)
    entity_update_many = AsyncMock(return_value=MagicMock(modified_count=3))
    collection = MagicMock(
        find=MagicMock(return_value=find_query),
        update_many=entity_update_many,
    )
    entity_cls = MagicMock()
    entity_cls.get_motor_collection = MagicMock(return_value=collection)

    # Capture audit records constructed and the insert_many calls
    audit_kwargs_seen: list = []
    audit_insert_many = AsyncMock()

    def cr_factory(**kwargs):
        audit_kwargs_seen.append(kwargs)
        m = MagicMock()
        m.model_dump = lambda by_alias=False: kwargs
        for k, v in kwargs.items():
            setattr(m, k, v)
        return m

    with patch(
        "kernel.entity.definition.EntityDefinition.find",
    ) as mock_find_defs, patch(
        "kernel.db.ENTITY_REGISTRY",
        {"Task": entity_cls},
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="genesis"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        side_effect=lambda r: f"hash-{id(r)}",
    ):
        find_defs_query = MagicMock()
        find_defs_query.to_list = AsyncMock(return_value=[defn])
        mock_find_defs.return_value = find_defs_query

        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=audit_insert_many)
        )
        MockCR.side_effect = cr_factory

        org_id = ObjectId("ccc11112222333344445555c")
        total = await save.cascade_nullify_references(
            entity_type="Touchpoint",
            entity_id=deleted_tp_id,
            org_id=org_id,
        )

    # 3 affected entities → total_affected=3
    assert total == 3

    # ONE insert_many flushes all 3 audit records (D5 strict in-memory batched)
    audit_insert_many.assert_called_once()
    docs = audit_insert_many.call_args[0][0]
    assert len(docs) == 3

    # ONE update_many per (collection, field) — single update_many for the Task.touchpoint group
    entity_update_many.assert_called_once()
    filter_doc, update_doc = entity_update_many.call_args[0]
    assert "_id" in filter_doc
    assert "$in" in filter_doc["_id"]
    assert set(filter_doc["_id"]["$in"]) == set(affected_ids)
    assert update_doc == {"$set": {"touchpoint": None}}

    # Audit records have the right shape per affected entity
    assert len(audit_kwargs_seen) == 3
    for i, kwargs in enumerate(audit_kwargs_seen):
        assert kwargs["change_type"] == "cascade_nullify"
        assert kwargs["method"] == "relationship_target_deleted"
        assert kwargs["entity_type"] == "Task"
        assert kwargs["entity_id"] == affected_ids[i]
        assert kwargs["method_metadata"]["deleted_entity_type"] == "Touchpoint"
        assert kwargs["method_metadata"]["deleted_entity_id"] == str(deleted_tp_id)
        assert kwargs["method_metadata"]["affected_field_names"] == ["touchpoint"]


@pytest.mark.asyncio
async def test_cascade_nullify_references_hash_chain_sequential(mock_cascade_deps):
    """In-memory hash chain: each cascade audit record chains from prior record."""
    from kernel.entity import save

    defn = MagicMock()
    defn.name = "Task"
    field_def = MagicMock()
    field_def.is_relationship = True
    field_def.relationship_target = "Touchpoint"
    defn.fields = {"touchpoint": field_def}

    affected_ids = [ObjectId(), ObjectId(), ObjectId(), ObjectId()]
    deleted_tp_id = ObjectId("bbbb11112222333344445555")
    affected_docs = [{"_id": aid, "touchpoint": deleted_tp_id} for aid in affected_ids]

    find_query = MagicMock()
    find_query.to_list = AsyncMock(return_value=affected_docs)
    collection = MagicMock(
        find=MagicMock(return_value=find_query),
        update_many=AsyncMock(return_value=MagicMock(modified_count=4)),
    )
    entity_cls = MagicMock()
    entity_cls.get_motor_collection = MagicMock(return_value=collection)

    hash_calls = []

    def tracking_compute_hash(record):
        h = f"hash-{len(hash_calls)}"
        hash_calls.append({"prev": record.previous_hash, "computed": h})
        return h

    def cr_factory(**kwargs):
        m = MagicMock()
        m.model_dump = lambda by_alias=False: kwargs
        for k, v in kwargs.items():
            setattr(m, k, v)
        return m

    with patch(
        "kernel.entity.definition.EntityDefinition.find",
    ) as mock_find_defs, patch(
        "kernel.db.ENTITY_REGISTRY",
        {"Task": entity_cls},
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="genesis"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        side_effect=tracking_compute_hash,
    ):
        find_defs_query = MagicMock()
        find_defs_query.to_list = AsyncMock(return_value=[defn])
        mock_find_defs.return_value = find_defs_query
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = cr_factory

        await save.cascade_nullify_references(
            entity_type="Touchpoint",
            entity_id=deleted_tp_id,
            org_id=ObjectId(),
        )

    # Sequential chain
    assert hash_calls[0]["prev"] == "genesis"
    assert hash_calls[1]["prev"] == "hash-0"
    assert hash_calls[2]["prev"] == "hash-1"
    assert hash_calls[3]["prev"] == "hash-2"
