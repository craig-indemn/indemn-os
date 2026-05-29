"""Tests for delete_tracked + bulk_delete_tracked — delete-time audit emission.

Per Session-35 Decision D-C (consolidated plan Stage A3): every entity delete
emits a ChangeRecord with `change_type=delete` + a pre-delete state snapshot.
Audit record is written BEFORE delete_one so a crash between the two leaves
the audit present (rebuilds see soft-deleted, never silently-missing).

Closes the gap that today causes 0 audit records for the 12,612 historical
creates and ~thousands of subsequent deletes — the substrate for sub-piece 12
reconstruction depends on this completeness.

Tests pin:
- delete_tracked writes audit ChangeRecord with change_type="delete"
- Pre-delete state snapshot: each non-None field → FieldChange(old=value, new=None)
- Hash chain: each delete audit chains from get_previous_hash
- bulk_delete_tracked emits per-entity audits in-memory hash-chained
- bulk_delete_tracked uses single insert_many + single delete_many shape
- BulkExecuteWorkflow delete branch routes via bulk_delete_tracked (source pin)
- BulkExecuteWorkflow delete path is OUTSIDE the transaction (source pin)
- actor_id captured on audit record (defaults to current_actor_id context)
- Polymorphic field pairs (id-field + type-field) both appear in one record
"""

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId


def _make_delete_entity(name="Acme", extra_fields=None, entity_id=None, type_name="TestEntity"):
    """Build a mock entity for delete_tracked / bulk_delete_tracked tests."""
    entity = MagicMock()
    entity.id = entity_id or ObjectId()
    entity.org_id = ObjectId("aabbccdd11223344aabbccdd")
    type(entity).__name__ = type_name

    full = {
        "_id": entity.id,
        "org_id": entity.org_id,
        "version": 1,
        "updated_at": datetime.now(timezone.utc),
        "name": name,
        "status": "logged",
    }
    if extra_fields:
        full.update(extra_fields)
    entity.model_dump = lambda by_alias=False: full

    mock_coll = MagicMock()
    mock_coll.delete_one = AsyncMock()
    mock_coll.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))
    mock_coll.database.client = MagicMock()
    entity.get_motor_collection = MagicMock(return_value=mock_coll)
    return entity


@pytest.fixture
def mock_delete_deps():
    """Patch kernel deps used by delete_tracked + bulk_delete_tracked."""
    with patch(
        "kernel.entity.save.current_correlation_id",
        MagicMock(get=MagicMock(return_value="corr-delete-1")),
    ), patch(
        "kernel.entity.save.current_effective_actor_id",
        MagicMock(get=MagicMock(return_value="eff-actor-delete-1")),
    ), patch(
        "kernel.entity.save.create_span",
    ) as mock_span, patch(
        "kernel.context.current_actor_id",
        MagicMock(get=MagicMock(return_value="actor-delete-1")),
    ):
        mock_span.return_value.__enter__ = MagicMock(return_value=None)
        mock_span.return_value.__exit__ = MagicMock(return_value=False)
        yield


@pytest.mark.asyncio
async def test_delete_tracked_writes_changerecord_before_deleting(mock_delete_deps):
    """Audit insert happens BEFORE delete_one — order pinned for crash safety."""
    from kernel.entity.save import delete_tracked

    entity = _make_delete_entity()
    call_order = []

    record_insert = AsyncMock(side_effect=lambda **kw: call_order.append("audit_insert"))
    delete_one = entity.get_motor_collection().delete_one
    delete_one.side_effect = lambda *a, **kw: call_order.append("entity_delete")

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev-hash"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="cur-hash",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        cr_instance = MagicMock(insert=record_insert, id=ObjectId())
        MockCR.return_value = cr_instance

        await delete_tracked(entity, actor_id="actor-1")

    assert call_order == ["audit_insert", "entity_delete"], (
        "Audit must be inserted before entity deletion"
    )


@pytest.mark.asyncio
async def test_delete_tracked_changerecord_change_type_is_delete(mock_delete_deps):
    """ChangeRecord constructed with change_type='delete'."""
    from kernel.entity.save import delete_tracked

    entity = _make_delete_entity()
    recorded = {}

    def capture_cr(**kwargs):
        recorded.update(kwargs)
        m = MagicMock(insert=AsyncMock(), id=ObjectId())
        return m

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev-hash"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = capture_cr

        await delete_tracked(entity, actor_id="actor-1")

    assert recorded["change_type"] == "delete"


@pytest.mark.asyncio
async def test_delete_tracked_emits_field_change_per_set_field(mock_delete_deps):
    """Pre-delete state snapshot: each non-None field → FieldChange(old=value, new=None)."""
    from kernel.entity.save import delete_tracked

    entity = _make_delete_entity(
        name="Acme",
        extra_fields={"domain": "acme.com", "status": "logged"},
    )
    recorded = {}

    def capture_cr(**kwargs):
        recorded.update(kwargs)
        m = MagicMock(insert=AsyncMock(), id=ObjectId())
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
        MockCR.side_effect = capture_cr

        await delete_tracked(entity, actor_id="actor-1")

    field_changes = recorded["changes"]
    by_field = {fc.field: fc for fc in field_changes}

    # Pre-delete state captured as old_value; new_value=None for all
    assert "name" in by_field
    assert by_field["name"].old_value == "Acme"
    assert by_field["name"].new_value is None

    assert "domain" in by_field
    assert by_field["domain"].old_value == "acme.com"

    assert "status" in by_field
    assert by_field["status"].old_value == "logged"


@pytest.mark.asyncio
async def test_delete_tracked_omits_none_fields_from_snapshot(mock_delete_deps):
    """None-valued fields are not emitted as FieldChanges (symmetric with create path)."""
    from kernel.entity.save import delete_tracked

    entity = _make_delete_entity(
        name="Acme",
        extra_fields={"domain": None, "annual_revenue": None, "status": "logged"},
    )
    recorded = {}

    def capture_cr(**kwargs):
        recorded.update(kwargs)
        m = MagicMock(insert=AsyncMock(), id=ObjectId())
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
        MockCR.side_effect = capture_cr

        await delete_tracked(entity, actor_id="actor-1")

    field_names = {fc.field for fc in recorded["changes"]}
    assert "domain" not in field_names
    assert "annual_revenue" not in field_names
    assert "name" in field_names
    assert "status" in field_names


@pytest.mark.asyncio
async def test_delete_tracked_hash_chain_uses_previous_hash(mock_delete_deps):
    """Audit record's previous_hash comes from get_previous_hash(org_id, session)."""
    from kernel.entity.save import delete_tracked

    entity = _make_delete_entity()
    get_prev = AsyncMock(return_value="seed-hash-xyz")

    cr_instance = MagicMock(insert=AsyncMock(), id=ObjectId())

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=get_prev,
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="next-hash",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.return_value = cr_instance

        await delete_tracked(entity, actor_id="actor-1")

    # previous_hash set to the genesis value before compute_hash chains
    assert cr_instance.previous_hash == "seed-hash-xyz"
    assert cr_instance.current_hash == "next-hash"
    get_prev.assert_awaited_once_with(entity.org_id, None)


@pytest.mark.asyncio
async def test_delete_tracked_records_actor_id(mock_delete_deps):
    """Explicit actor_id is recorded on the audit."""
    from kernel.entity.save import delete_tracked

    entity = _make_delete_entity()
    recorded = {}

    def capture_cr(**kwargs):
        recorded.update(kwargs)
        return MagicMock(insert=AsyncMock(), id=ObjectId())

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = capture_cr

        await delete_tracked(entity, actor_id="explicit-actor")

    assert recorded["actor_id"] == "explicit-actor"
    # effective_actor_id derived from contextvar in fixture
    assert recorded["effective_actor_id"] == "eff-actor-delete-1"


@pytest.mark.asyncio
async def test_delete_tracked_polymorphic_field_pair_in_one_record(mock_delete_deps):
    """D9-compatible: polymorphic source pair (Touchpoint.source_entity_id +
    source_entity_type) both appear as FieldChanges in the single delete record."""
    from kernel.entity.save import delete_tracked

    entity = _make_delete_entity(
        name="touchpoint-1",
        extra_fields={
            "source_entity_id": ObjectId("aaaa11112222333344445555"),
            "source_entity_type": "Email",
            "company": ObjectId("bbbb11112222333344445555"),
        },
        type_name="Touchpoint",
    )
    recorded = {}

    def capture_cr(**kwargs):
        recorded.update(kwargs)
        return MagicMock(insert=AsyncMock(), id=ObjectId())

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR:
        MockCR.side_effect = capture_cr

        await delete_tracked(entity, actor_id="actor-1")

    field_names = {fc.field for fc in recorded["changes"]}
    assert "source_entity_id" in field_names
    assert "source_entity_type" in field_names
    # Same single ChangeRecord (atomic per Session-35 D9 — atomicity inherent on create + delete)


@pytest.mark.asyncio
async def test_bulk_delete_tracked_emits_per_entity_audit(mock_delete_deps):
    """bulk_delete_tracked writes one ChangeRecord per entity (with non-empty changes)."""
    from kernel.entity.save import bulk_delete_tracked

    entities = [_make_delete_entity(name=f"Acme-{i}") for i in range(3)]
    # Force all entities to share one mock collection so insert_many/delete_many are easy to assert
    shared_coll = MagicMock(
        delete_many=AsyncMock(return_value=MagicMock(deleted_count=3)),
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

        result = await bulk_delete_tracked(entities, actor_id="actor-bulk")

    assert result["succeeded"] == 3
    assert len(recorded_kwargs) == 3

    from kernel.changes.collection import FieldChange
    for kwargs in recorded_kwargs:
        assert kwargs["change_type"] == "delete"
        assert kwargs["actor_id"] == "actor-bulk"
        assert len(kwargs["changes"]) > 0
        for fc in kwargs["changes"]:
            assert isinstance(fc, FieldChange)
            assert fc.new_value is None  # all delete records: new_value=None


@pytest.mark.asyncio
async def test_bulk_delete_tracked_hash_chain_sequential(mock_delete_deps):
    """Per-entity records chain sequentially within the batch (in-memory chain)."""
    from kernel.entity.save import bulk_delete_tracked

    entities = [_make_delete_entity(name=f"Acme-{i}") for i in range(4)]
    shared_coll = MagicMock(
        delete_many=AsyncMock(return_value=MagicMock(deleted_count=4)),
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
        new=AsyncMock(return_value="genesis"),
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

        result = await bulk_delete_tracked(entities, actor_id="actor-bulk")

    assert result["succeeded"] == 4
    assert hash_calls[0]["prev"] == "genesis"
    assert hash_calls[1]["prev"] == "hash-0"
    assert hash_calls[2]["prev"] == "hash-1"
    assert hash_calls[3]["prev"] == "hash-2"


@pytest.mark.asyncio
async def test_bulk_delete_tracked_uses_single_insert_many_and_delete_many(mock_delete_deps):
    """Single insert_many for audits, single delete_many for entities (mirrors bulk_save_tracked shape)."""
    from kernel.entity.save import bulk_delete_tracked

    entities = [_make_delete_entity(name=f"Acme-{i}") for i in range(5)]

    changes_insert_many = AsyncMock()
    entity_delete_many = AsyncMock(return_value=MagicMock(deleted_count=5))
    shared_coll = MagicMock(delete_many=entity_delete_many)
    shared_coll.database.client = MagicMock()
    for e in entities:
        e.get_motor_collection = MagicMock(return_value=shared_coll)

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

        await bulk_delete_tracked(entities, actor_id="actor-bulk")

    # Audits batched into one insert_many
    changes_insert_many.assert_called_once()
    docs = changes_insert_many.call_args[0][0]
    assert len(docs) == 5

    # Entity removal batched into one delete_many
    entity_delete_many.assert_called_once()
    delete_filter = entity_delete_many.call_args[0][0]
    assert "_id" in delete_filter
    assert "$in" in delete_filter["_id"]
    assert len(delete_filter["_id"]["$in"]) == 5


@pytest.mark.asyncio
async def test_bulk_delete_tracked_empty_list_returns_zeros(mock_delete_deps):
    """Empty input short-circuits — no DB calls, no error."""
    from kernel.entity.save import bulk_delete_tracked

    result = await bulk_delete_tracked([], actor_id="actor-1")
    assert result == {"succeeded": 0, "errored": 0, "errors": [], "deleted_ids": []}


def test_delete_tracked_signature():
    """Shape pin: delete_tracked(entity, actor_id=None, correlation_id=None, session=None) -> ObjectId."""
    from kernel.entity.save import delete_tracked

    assert inspect.iscoroutinefunction(delete_tracked)
    sig = inspect.signature(delete_tracked)
    params = list(sig.parameters.keys())
    assert params == ["entity", "actor_id", "correlation_id", "session"]


def test_bulk_delete_tracked_signature():
    """Shape pin: bulk_delete_tracked(entities, actor_id=None, correlation_id=None) -> dict."""
    from kernel.entity.save import bulk_delete_tracked

    assert inspect.iscoroutinefunction(bulk_delete_tracked)
    sig = inspect.signature(bulk_delete_tracked)
    params = list(sig.parameters.keys())
    assert params == ["entities", "actor_id", "correlation_id"]


def test_activities_bulk_workflow_delete_uses_bulk_delete_tracked():
    """Source pin: process_bulk_batch delete branch routes through bulk_delete_tracked.

    Replaces the prior `delete_one` shortcut at activities.py:281 — single-entity
    AND bulk delete cases share this path (no @router.delete exists; per Session
    35 R1 F9, single-entity deletes route through CLI → /api/{slug}/bulk).
    """
    from kernel.temporal import activities

    src = inspect.getsource(activities.process_bulk_batch)
    assert "bulk_delete_tracked" in src
    # Cascade still called per-entity (A4 will refactor cascade to audit)
    assert "cascade_nullify_references" in src
    # Old direct delete_one shortcut is gone
    assert "delete_one(\n                                {\"_id\": entity.id}, session=session" not in src


def test_activities_bulk_workflow_delete_outside_transaction():
    """Source pin: delete branch handled BEFORE the transaction-wrapped per-entity loop.

    bulk_delete_tracked uses non-transactional insert_many + delete_many (mirroring
    bulk_save_tracked shape). The transaction now wraps only transition / method /
    update / create operations.
    """
    from kernel.temporal import activities

    src = inspect.getsource(activities.process_bulk_batch)
    # Delete branch lives before the per-entity transaction-wrapped loop label
    delete_label_pos = src.index("DELETE path (Stage A3")
    non_delete_label_pos = src.index("Non-delete-non-update path")
    assert delete_label_pos < non_delete_label_pos


def test_activities_restores_actor_id_contextvar_from_spec():
    """Source pin: process_bulk_batch + preview_bulk_operation restore current_actor_id from spec.

    Session-36 Stage A live deploy surfaced a pre-existing latent gap: Temporal
    activities run in a fresh contextvar scope, so `current_actor_id.get()`
    returns None inside the activity even though the API auth middleware set
    it before workflow start. bulk_delete_tracked / bulk_update_tracked /
    entity.save_tracked() all dereference current_actor_id when building
    ChangeRecord, which then fails Pydantic validation (`actor_id: str`).

    Fix: propagate actor_id through BulkOperationSpec (workflows.py), set it
    in the API spec_dict (registration.py start_bulk), and restore the
    contextvar at activity entry alongside org_id (activities.py).

    Pin: the restore code is present in BOTH process_bulk_batch and
    preview_bulk_operation, alongside the existing org_id restore.
    """
    from kernel.temporal import activities

    pbb = inspect.getsource(activities.process_bulk_batch)
    pbo = inspect.getsource(activities.preview_bulk_operation)

    # Both activities import and set current_actor_id when spec.actor_id is present
    assert "from kernel.context import current_actor_id" in pbb
    assert "current_actor_id.set(spec.actor_id)" in pbb
    assert "if spec.actor_id" in pbb

    assert "from kernel.context import current_actor_id" in pbo
    assert "current_actor_id.set(spec.actor_id)" in pbo
    assert "if spec.actor_id" in pbo


def test_workflows_bulk_operation_spec_has_actor_id_field():
    """Source pin: BulkOperationSpec exposes actor_id (Optional[str], default None)."""
    from kernel.temporal.workflows import BulkOperationSpec

    spec = BulkOperationSpec(entity_type="Email", operation="delete", org_id="oid", actor_id="aid")
    assert spec.actor_id == "aid"
    # Default is None — preserves backward compat for any spec_dict missing the field
    spec_no_actor = BulkOperationSpec(entity_type="Email", operation="delete", org_id="oid")
    assert spec_no_actor.actor_id is None


def test_registration_start_bulk_propagates_actor_id():
    """Source pin: /bulk endpoint sets spec['actor_id'] from the auth-context actor.id."""
    from kernel.api import registration

    src = inspect.getsource(registration)
    # The propagation lives in start_bulk
    assert 'spec["actor_id"] = str(actor.id)' in src


def test_activities_update_branch_routes_through_bulk_update_tracked():
    """Source pin: UPDATE branch routes through bulk_update_tracked (Stage A5 — D4 + D24).

    The prior "Silent update — bypasses save_tracked()" shortcut at
    activities.py:267-276 is removed; bulk updates now emit per-entity audit
    ChangeRecord via bulk_update_tracked (in-memory hash chain pattern).

    NOTE on event emission: D4 expanded D-A's AUDIT scope to cover this path.
    Watch events remain silent for bulk updates per the original "Silent update"
    intent — event emission was orthogonal to the D-A completeness gap and is
    not pinned in D1-D26.
    """
    from kernel.temporal import activities

    src = inspect.getsource(activities.process_bulk_batch)
    assert "bulk_update_tracked" in src
    # Old silent-bypass shortcut at activities.py:267-276 is gone
    assert "Silent update — bypasses save_tracked()" not in src
    # UPDATE handled outside the per-entity transaction-wrapped loop
    update_label_pos = src.index("UPDATE path (Stage A5")
    non_delete_non_update_pos = src.index("Non-delete-non-update path")
    assert update_label_pos < non_delete_non_update_pos
