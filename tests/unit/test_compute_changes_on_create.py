"""Tests for _compute_changes(entity, is_new) — per-field FieldChange emission on create.

Per Session-35 Decision D1: the kernel changes collection becomes a complete
append-only audit trail. Create records emit one FieldChange entry per set
field with a non-None value (no default-omission, no implicit defaults). This
is the substrate that lets sub-piece 12 reconstruct entity state at
trace.end_time without ever falling back to live MongoDB.

Tests pin:
- Create path emits per-field FieldChange for non-None fields (D1)
- Create skips _id / id / version / updated_at / revision_id metadata fields
- Create skips None-valued fields (no synthetic None entries)
- Create emits both halves of polymorphic field pairs in the same record (D9)
- Update path emits diff against _loaded_state (regression)
- save_tracked_impl flips is_new flag at the gate (source pin)
- bulk_save_tracked emits per-entity per-field FieldChanges (D1)
- bulk_save_tracked hash chain remains intact with non-empty changes
- Heartbeat fast-path on Attention/Runtime untouched (D8 carveout)
- Update path against _loaded_state preserved
"""

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId


def _make_pydantic_entity(field_values: dict, loaded_state: dict = None, type_name: str = "TestEntity"):
    """Build a mock entity whose model_dump returns the given dict."""
    entity = MagicMock()
    for fname, fval in field_values.items():
        setattr(entity, fname, fval)
    type(entity).__name__ = type_name
    entity.model_dump = MagicMock(return_value=dict(field_values))
    entity._loaded_state = loaded_state or {}
    return entity


def test_compute_changes_on_create_emits_field_per_set_field():
    """D1: every non-None field gets a FieldChange on create."""
    from kernel.entity.save import _compute_changes

    entity = _make_pydantic_entity({
        "org_id": ObjectId("aabbccddee0011223344aabb"),
        "name": "Acme Corp",
        "domain": "acme.com",
        "status": "received",
    })

    changes = _compute_changes(entity, is_new=True)

    fields_emitted = {c["field"] for c in changes}
    assert fields_emitted == {"org_id", "name", "domain", "status"}
    assert len(changes) == 4


def test_compute_changes_on_create_field_change_old_value_is_none():
    """D1: all create FieldChanges have old_value=None."""
    from kernel.entity.save import _compute_changes

    entity = _make_pydantic_entity({
        "name": "Acme",
        "domain": "acme.com",
    })

    changes = _compute_changes(entity, is_new=True)

    for c in changes:
        assert c["old_value"] is None, f"Field {c['field']} should have old_value=None on create"


def test_compute_changes_on_create_field_change_new_value_matches_entity():
    """D1: each FieldChange.new_value matches the entity's field value."""
    from kernel.entity.save import _compute_changes

    entity = _make_pydantic_entity({
        "name": "Acme",
        "annual_revenue": 50_000_000,
        "active": True,
    })

    changes = _compute_changes(entity, is_new=True)
    by_field = {c["field"]: c for c in changes}

    assert by_field["name"]["new_value"] == "Acme"
    assert by_field["annual_revenue"]["new_value"] == 50_000_000
    assert by_field["active"]["new_value"] is True


def test_compute_changes_on_create_skips_none_fields():
    """D1: None-valued fields are not emitted as FieldChanges (no synthetic None entries)."""
    from kernel.entity.save import _compute_changes

    entity = _make_pydantic_entity({
        "name": "Acme",
        "domain": None,           # never-set optional
        "annual_revenue": None,   # never-set optional
        "status": "received",
    })

    changes = _compute_changes(entity, is_new=True)

    fields_emitted = {c["field"] for c in changes}
    assert fields_emitted == {"name", "status"}
    assert "domain" not in fields_emitted
    assert "annual_revenue" not in fields_emitted


def test_compute_changes_on_create_skips_metadata_fields():
    """_id, id, revision_id, version, updated_at are never emitted as FieldChanges."""
    from kernel.entity.save import _compute_changes

    entity = _make_pydantic_entity({
        "_id": ObjectId(),
        "id": ObjectId(),
        "revision_id": "rev-1",
        "version": 1,
        "updated_at": datetime.now(timezone.utc),
        "name": "Acme",
    })

    changes = _compute_changes(entity, is_new=True)

    fields_emitted = {c["field"] for c in changes}
    assert fields_emitted == {"name"}


def test_compute_changes_on_create_polymorphic_pair_atomicity():
    """D9: polymorphic field pairs (id-field + type-field) emit as 2 FieldChanges in the same record.

    A Touchpoint with source_entity_id + source_entity_type both set should
    produce both as FieldChanges in the single ChangeRecord that captures the
    create. Atomicity is inherent — one ChangeRecord per create, one
    FieldChange per set field.
    """
    from kernel.entity.save import _compute_changes

    touchpoint = _make_pydantic_entity({
        "source_entity_id": ObjectId("aaaa11112222333344445555"),
        "source_entity_type": "Email",
        "company": ObjectId("bbbb11112222333344445555"),
        "status": "logged",
    }, type_name="Touchpoint")

    changes = _compute_changes(touchpoint, is_new=True)

    fields_emitted = {c["field"] for c in changes}
    assert "source_entity_id" in fields_emitted
    assert "source_entity_type" in fields_emitted
    assert len(changes) == 4  # one record holds all four — same record for both halves


def test_compute_changes_on_update_preserves_diff_behavior():
    """Regression: update path (is_new=False) still emits diffs against _loaded_state."""
    from kernel.entity.save import _compute_changes

    entity = _make_pydantic_entity(
        field_values={
            "name": "Acme New",
            "status": "processed",
            "domain": "acme.com",
        },
        loaded_state={
            "name": "Acme Old",
            "status": "received",
            "domain": "acme.com",  # unchanged
        },
    )

    changes = _compute_changes(entity, is_new=False)

    by_field = {c["field"]: c for c in changes}
    assert "name" in by_field
    assert by_field["name"] == {"field": "name", "old_value": "Acme Old", "new_value": "Acme New"}
    assert "status" in by_field
    assert by_field["status"] == {"field": "status", "old_value": "received", "new_value": "processed"}
    assert "domain" not in by_field  # unchanged → not emitted


def test_compute_changes_on_update_default_signature_unchanged():
    """is_new defaults to False — call sites without explicit is_new still get update behavior."""
    from kernel.entity.save import _compute_changes

    entity = _make_pydantic_entity(
        field_values={"name": "Acme New"},
        loaded_state={"name": "Acme Old"},
    )

    # Default call — no is_new kwarg — should diff against _loaded_state
    changes = _compute_changes(entity)

    assert len(changes) == 1
    assert changes[0]["field"] == "name"
    assert changes[0]["old_value"] == "Acme Old"
    assert changes[0]["new_value"] == "Acme New"


def test_save_tracked_impl_propagates_is_new_to_compute_changes():
    """save_tracked_impl source contains the new is_new-aware call site.

    Pin: the gate flipped from `_compute_changes(entity) if not is_new else []`
    to `_compute_changes(entity, is_new=is_new)` so create records also emit
    field-level changes (D1).
    """
    from kernel.entity import save

    src = inspect.getsource(save.save_tracked_impl)
    # New form: pass is_new keyword to _compute_changes
    assert "_compute_changes(entity, is_new=is_new)" in src
    # Old form must be gone
    assert "_compute_changes(entity) if not is_new else []" not in src


def test_compute_changes_signature_accepts_is_new_kwarg():
    """Shape pin: _compute_changes(entity, is_new: bool = False) is the new signature."""
    from kernel.entity.save import _compute_changes

    sig = inspect.signature(_compute_changes)
    params = list(sig.parameters.keys())
    assert params == ["entity", "is_new"]
    assert sig.parameters["is_new"].default is False


@pytest.fixture
def mock_kernel_deps_for_bulk():
    """Patch kernel deps for bulk_save_tracked tests."""
    with patch(
        "kernel.entity.save.current_correlation_id",
        MagicMock(get=MagicMock(return_value="corr-bulk-1")),
    ), patch(
        "kernel.entity.save.current_effective_actor_id",
        MagicMock(get=MagicMock(return_value="eff-actor-bulk-1")),
    ), patch(
        "kernel.entity.save.current_causation_message_id",
        MagicMock(get=MagicMock(return_value="caus-bulk-1")),
    ), patch(
        "kernel.entity.save.current_depth",
        MagicMock(get=MagicMock(return_value=0)),
    ), patch(
        "kernel.entity.save.evaluate_computed_fields",
    ), patch(
        "kernel.entity.save.evaluate_watches_and_emit",
        new=AsyncMock(return_value=[]),
    ), patch(
        "kernel.entity.save.build_event_metadata",
        return_value={"event": "created"},
    ), patch(
        "kernel.entity.save.create_span",
    ) as mock_span:
        mock_span.return_value.__enter__ = MagicMock(return_value=None)
        mock_span.return_value.__exit__ = MagicMock(return_value=False)
        yield


def _make_bulk_entity(ext_ref, org_id="aabbccdd11223344aabbccdd", extra_fields=None):
    """Build a mock entity for bulk_save_tracked tests with extra payload fields."""
    entity = MagicMock()
    entity.id = None
    entity.org_id = ObjectId(org_id)
    entity.version = 0
    entity.updated_at = None
    entity.created_by = None
    entity.external_ref = ext_ref
    type(entity).__name__ = "TestEntity"

    full = {
        "_id": None,
        "org_id": entity.org_id,
        "version": 1,
        "updated_at": "<timestamp>",
        "created_by": None,
        "external_ref": ext_ref,
    }
    if extra_fields:
        full.update(extra_fields)
        for k, v in extra_fields.items():
            setattr(entity, k, v)

    entity.model_dump = lambda by_alias=False: full

    mock_coll = MagicMock()
    mock_coll.insert_many = AsyncMock()
    mock_coll.database.client = MagicMock()
    entity.get_motor_collection = MagicMock(return_value=mock_coll)
    return entity


@pytest.mark.asyncio
async def test_bulk_save_tracked_emits_per_entity_per_field_changes(mock_kernel_deps_for_bulk):
    """D1: bulk_save_tracked builds ChangeRecord(changes=[...]) per entity with non-empty changes.

    Each entity contributes a non-empty changes array to its ChangeRecord —
    not the legacy `changes=[]` placeholder.
    """
    from kernel.entity.save import bulk_save_tracked

    entities = [
        _make_bulk_entity(f"ref-{i}", extra_fields={"name": f"Acme {i}", "status": "logged"})
        for i in range(3)
    ]

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

        result = await bulk_save_tracked(entities, actor_id="actor-1", method="fetch_new")

    assert result["succeeded"] == 3
    assert len(recorded_kwargs) == 3

    for kwargs in recorded_kwargs:
        # Per-entity ChangeRecord must NOT have empty changes (D1 fix target)
        assert len(kwargs["changes"]) > 0, "ChangeRecord.changes must be non-empty on create"
        # Each entry is a FieldChange instance (Pydantic wrap in bulk_save_tracked)
        from kernel.changes.collection import FieldChange
        for fc in kwargs["changes"]:
            assert isinstance(fc, FieldChange)
            assert fc.old_value is None  # all create records: old_value=None
        # Includes the populated extra fields (name + status) AND the auto-set
        # org_id + external_ref + created_by.
        field_names = {fc.field for fc in kwargs["changes"]}
        assert "name" in field_names
        assert "status" in field_names
        assert "external_ref" in field_names


@pytest.mark.asyncio
async def test_bulk_save_tracked_hash_chain_integrity_with_field_changes(mock_kernel_deps_for_bulk):
    """D1+D5 compatibility: hash chain still sequential when changes arrays are non-empty."""
    from kernel.entity.save import bulk_save_tracked

    entities = [
        _make_bulk_entity(f"ref-{i}", extra_fields={"name": f"Acme {i}"})
        for i in range(4)
    ]

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

        result = await bulk_save_tracked(entities, actor_id="actor-1", method="fetch_new")

    assert result["succeeded"] == 4
    # Chain sequential as before — D1 doesn't break ordering
    assert hash_calls[0]["prev"] == "genesis"
    assert hash_calls[1]["prev"] == "hash-0"
    assert hash_calls[2]["prev"] == "hash-1"
    assert hash_calls[3]["prev"] == "hash-2"


def test_heartbeat_carveout_preserved_in_source():
    """D8 carveout: _is_heartbeat_only fast-path is unchanged.

    Source pin: _is_heartbeat_only still gates on Attention/Runtime + heartbeat
    field set, and _save_heartbeat_only still short-circuits without writing
    a ChangeRecord. D1's per-field emission does NOT reach the heartbeat path.
    """
    from kernel.entity import save

    is_hb_src = inspect.getsource(save._is_heartbeat_only)
    save_hb_src = inspect.getsource(save._save_heartbeat_only)
    save_impl_src = inspect.getsource(save.save_tracked_impl)

    # Heartbeat carveout still gates on entity type Attention/Runtime
    assert "Attention" in is_hb_src
    assert "Runtime" in is_hb_src
    # Heartbeat fast-path still short-circuits (no ChangeRecord)
    assert "write_change_record" not in save_hb_src
    # save_tracked_impl still routes through heartbeat fast-path BEFORE compute_changes
    hb_check_pos = save_impl_src.index("_is_heartbeat_only")
    cc_pos = save_impl_src.index("_compute_changes")
    assert hb_check_pos < cc_pos, "heartbeat fast-path must run before _compute_changes"


def test_bulk_save_tracked_field_change_import_present():
    """bulk_save_tracked uses FieldChange to wrap dict entries (D1 implementation detail)."""
    from kernel.entity import save

    src = inspect.getsource(save.bulk_save_tracked)
    # FieldChange import is local-to-function in bulk_save_tracked
    assert "from kernel.changes.collection import ChangeRecord, FieldChange" in src
    # The new wrapping pattern
    assert "FieldChange(**c) for c in field_changes" in src
    # _compute_changes called with is_new=True
    assert "_compute_changes(entity, is_new=True)" in src
