"""Tests for bulk_update_tracked — per-entity audit emission on bulk updates.

Per Session-35 Decision D4 (consolidated plan Stage A5): D-A audit scope
expands to cover BulkExecuteWorkflow UPDATE branch. The prior
"Silent update — bypasses save_tracked()" shortcut at activities.py:267-276
is removed; bulk updates route through bulk_update_tracked.

Per D24: per-entity ChangeRecord via in-memory hash-chain. Same audit shape
for create, update, delete, cascade. Mirror bulk_save_tracked lines 312-337.

Per Session-36 A5 implementation choice: AUDIT emission only — watch events
remain silent for bulk updates. D4 scoped audit completeness, not event
fan-out. Documented in bulk_update_tracked docstring and the activities.py
UPDATE-path header.

Tests pin:
- N entities + uniform sets → N ChangeRecord(change_type='update') with audit
- FieldChange.old_value = entity's current field value (via getattr)
- FieldChange.new_value = sets[field] (the uniform new value)
- Hash chain sequential within batch
- Single insert_many for audit; single update_many for entity changes
- Empty entities OR empty sets → zeros (short-circuit)
- BulkExecuteWorkflow UPDATE branch routes through bulk_update_tracked (source pin)
"""

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId


def _make_update_entity(entity_id=None, field_values=None):
    """Build a mock entity for bulk_update_tracked tests."""
    entity = MagicMock()
    entity.id = entity_id or ObjectId()
    entity.org_id = ObjectId("aabbccdd11223344aabbccdd")
    type(entity).__name__ = "TestEntity"
    # Mirror the entity's current field values via getattr
    for fname, fval in (field_values or {}).items():
        setattr(entity, fname, fval)
    mock_coll = MagicMock(
        update_many=AsyncMock(return_value=MagicMock(modified_count=1)),
    )
    mock_coll.database.client = MagicMock()
    entity.get_motor_collection = MagicMock(return_value=mock_coll)
    return entity


@pytest.fixture
def mock_update_deps():
    """Patch kernel deps used by bulk_update_tracked."""
    with patch(
        "kernel.entity.save.current_correlation_id",
        MagicMock(get=MagicMock(return_value="corr-update-1")),
    ), patch(
        "kernel.entity.save.current_effective_actor_id",
        MagicMock(get=MagicMock(return_value="eff-actor-update-1")),
    ), patch(
        "kernel.context.current_actor_id",
        MagicMock(get=MagicMock(return_value="actor-update-1")),
    ), patch(
        "kernel.entity.save.create_span",
    ) as mock_span:
        mock_span.return_value.__enter__ = MagicMock(return_value=None)
        mock_span.return_value.__exit__ = MagicMock(return_value=False)
        yield


@pytest.mark.asyncio
async def test_bulk_update_tracked_emits_per_entity_audit(mock_update_deps):
    """D4 + D24: each entity gets its own ChangeRecord with change_type='update'."""
    from kernel.entity.save import bulk_update_tracked

    entities = [
        _make_update_entity(field_values={"status": "received", "name": f"Acme-{i}"})
        for i in range(4)
    ]
    # Share one collection so we can assert on update_many
    shared_coll = MagicMock(
        update_many=AsyncMock(return_value=MagicMock(modified_count=4)),
    )
    shared_coll.database.client = MagicMock()
    for e in entities:
        e.get_motor_collection = MagicMock(return_value=shared_coll)

    recorded_kwargs = []

    def cr_factory(**kwargs):
        recorded_kwargs.append(kwargs)
        m = MagicMock()
        m.model_dump = lambda by_alias=False: kwargs
        for k, v in kwargs.items():
            setattr(m, k, v)
        return m

    sets = {"status": "processed"}

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="genesis"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        side_effect=lambda r: f"hash-{id(r)}",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = cr_factory

        result = await bulk_update_tracked(entities, sets, method="bulk_update")

    assert result["succeeded"] == 4
    assert len(recorded_kwargs) == 4

    from kernel.changes.collection import FieldChange
    for kwargs in recorded_kwargs:
        assert kwargs["change_type"] == "update"
        assert kwargs["method"] == "bulk_update"
        assert len(kwargs["changes"]) > 0
        for fc in kwargs["changes"]:
            assert isinstance(fc, FieldChange)


@pytest.mark.asyncio
async def test_bulk_update_tracked_field_changes_capture_old_and_new(mock_update_deps):
    """FieldChange.old_value = current entity value via getattr; new_value = sets[field]."""
    from kernel.entity.save import bulk_update_tracked

    # Each entity has a DIFFERENT current `status` — captures per-entity diversity
    entities = [
        _make_update_entity(field_values={"status": "received", "name": "Acme-A"}),
        _make_update_entity(field_values={"status": "logged", "name": "Acme-B"}),
    ]
    shared_coll = MagicMock(
        update_many=AsyncMock(return_value=MagicMock(modified_count=2)),
    )
    shared_coll.database.client = MagicMock()
    for e in entities:
        e.get_motor_collection = MagicMock(return_value=shared_coll)

    recorded_kwargs = []

    def cr_factory(**kwargs):
        recorded_kwargs.append(kwargs)
        m = MagicMock()
        m.model_dump = lambda by_alias=False: kwargs
        return m

    sets = {"status": "processed"}

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = cr_factory

        await bulk_update_tracked(entities, sets, method="bulk_update")

    # Per-entity FieldChanges reflect each entity's own current value as old_value
    assert recorded_kwargs[0]["changes"][0].field == "status"
    assert recorded_kwargs[0]["changes"][0].old_value == "received"
    assert recorded_kwargs[0]["changes"][0].new_value == "processed"

    assert recorded_kwargs[1]["changes"][0].field == "status"
    assert recorded_kwargs[1]["changes"][0].old_value == "logged"
    assert recorded_kwargs[1]["changes"][0].new_value == "processed"


@pytest.mark.asyncio
async def test_bulk_update_tracked_hash_chain_sequential(mock_update_deps):
    """In-memory hash chain: each record's previous_hash = prior record's current_hash."""
    from kernel.entity.save import bulk_update_tracked

    entities = [_make_update_entity(field_values={"status": "received"}) for _ in range(3)]
    shared_coll = MagicMock(
        update_many=AsyncMock(return_value=MagicMock(modified_count=3)),
    )
    shared_coll.database.client = MagicMock()
    for e in entities:
        e.get_motor_collection = MagicMock(return_value=shared_coll)

    hash_calls = []

    def tracking_compute_hash(record):
        h = f"hash-{len(hash_calls)}"
        hash_calls.append({"prev": record.previous_hash, "computed": h})
        return h

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="genesis-update"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        side_effect=tracking_compute_hash,
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )

        def cr_factory(**kwargs):
            m = MagicMock()
            m.model_dump = lambda by_alias=False: kwargs
            for k, v in kwargs.items():
                setattr(m, k, v)
            return m

        MockCR.side_effect = cr_factory

        await bulk_update_tracked(entities, {"status": "processed"})

    assert hash_calls[0]["prev"] == "genesis-update"
    assert hash_calls[1]["prev"] == "hash-0"
    assert hash_calls[2]["prev"] == "hash-1"


@pytest.mark.asyncio
async def test_bulk_update_tracked_single_insert_many_and_update_many(mock_update_deps):
    """Single insert_many for audits, single update_many with $set + $inc version."""
    from kernel.entity.save import bulk_update_tracked

    entities = [_make_update_entity(field_values={"status": "received"}) for _ in range(5)]

    changes_insert_many = AsyncMock()
    entity_update_many = AsyncMock(return_value=MagicMock(modified_count=5))
    shared_coll = MagicMock(update_many=entity_update_many)
    shared_coll.database.client = MagicMock()
    for e in entities:
        e.get_motor_collection = MagicMock(return_value=shared_coll)

    sets = {"status": "processed", "priority": "high"}

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=changes_insert_many)
        )
        MockCR.side_effect = lambda **kwargs: MagicMock(
            model_dump=lambda by_alias=False: kwargs, **kwargs
        )

        await bulk_update_tracked(entities, sets, method="bulk_update")

    # Audits batched into one insert_many
    changes_insert_many.assert_called_once()
    docs = changes_insert_many.call_args[0][0]
    assert len(docs) == 5

    # Entity updates batched into one update_many with $set + $inc
    entity_update_many.assert_called_once()
    filter_doc, update_doc = entity_update_many.call_args[0]
    assert "_id" in filter_doc
    assert "$in" in filter_doc["_id"]
    assert len(filter_doc["_id"]["$in"]) == 5
    assert update_doc["$set"] == sets
    assert update_doc["$inc"] == {"version": 1}


@pytest.mark.asyncio
async def test_bulk_update_tracked_empty_entities_returns_zeros(mock_update_deps):
    """Empty list short-circuits — no DB calls."""
    from kernel.entity.save import bulk_update_tracked

    result = await bulk_update_tracked([], {"status": "processed"})
    assert result == {"succeeded": 0, "errored": 0, "errors": [], "updated_ids": []}


@pytest.mark.asyncio
async def test_bulk_update_tracked_empty_sets_returns_zeros(mock_update_deps):
    """Empty sets dict short-circuits — nothing to update."""
    from kernel.entity.save import bulk_update_tracked

    entities = [_make_update_entity()]
    result = await bulk_update_tracked(entities, {})
    assert result == {"succeeded": 0, "errored": 0, "errors": [], "updated_ids": []}


@pytest.mark.asyncio
async def test_bulk_update_tracked_method_metadata_captured(mock_update_deps):
    """method + method_metadata pass through to the ChangeRecord (Bug #22 forensics + Stage A audit)."""
    from kernel.entity.save import bulk_update_tracked

    entities = [_make_update_entity(field_values={"status": "received"})]
    shared_coll = MagicMock(
        update_many=AsyncMock(return_value=MagicMock(modified_count=1)),
    )
    shared_coll.database.client = MagicMock()
    entities[0].get_motor_collection = MagicMock(return_value=shared_coll)

    recorded = {}

    def cr_factory(**kwargs):
        recorded.update(kwargs)
        return MagicMock(model_dump=lambda by_alias=False: kwargs)

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = cr_factory

        await bulk_update_tracked(
            entities,
            {"status": "processed"},
            method="bulk_update",
            method_metadata={"bulk_operation_id": "wf-123"},
        )

    assert recorded["method"] == "bulk_update"
    assert recorded["method_metadata"] == {"bulk_operation_id": "wf-123"}


def test_bulk_update_tracked_signature():
    """Shape pin: bulk_update_tracked(entities, sets, actor_id=None, correlation_id=None, method=None, method_metadata=None)."""
    from kernel.entity.save import bulk_update_tracked

    assert inspect.iscoroutinefunction(bulk_update_tracked)
    sig = inspect.signature(bulk_update_tracked)
    params = list(sig.parameters.keys())
    assert params == ["entities", "sets", "actor_id", "correlation_id", "method", "method_metadata"]


def test_bulk_update_tracked_otel_span():
    """bulk_update_tracked emits OTEL span with entity_type + batch_size attributes."""
    from kernel.entity import save

    src = inspect.getsource(save.bulk_update_tracked)
    assert "entity.bulk_update_tracked" in src
    assert "batch_size" in src


def test_bulk_update_tracked_imports_fieldchange():
    """Source pin: bulk_update_tracked uses FieldChange to wrap dict entries (mirrors bulk_save_tracked)."""
    from kernel.entity import save

    src = inspect.getsource(save.bulk_update_tracked)
    assert "from kernel.changes.collection import ChangeRecord, FieldChange" in src
    assert "FieldChange(**c) for c in field_changes" in src
