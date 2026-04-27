"""Tests for kernel.entity.indexes.reconcile_indexes.

Bug surfaced 2026-04-27 by the Alliance trace: Meeting create returned a
500 with `DuplicateKeyError: org_id_1_external_ref_1 dup key { ...,
external_ref: null }`. Diagnosis: the current Meeting entity definition
declared no unique on external_ref, but a prior version had — the
kernel's additive `create_index` loop never dropped the stale index.

This module tests the declarative reconciler that fixes that. The
contract: the entity definition is the source of truth; MongoDB indexes
follow. Stale kernel-managed indexes get dropped; missing ones get
created; operator-added custom indexes (non-kernel naming) and `_id_`
are preserved.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernel.entity.indexes import (
    _desired_indexes,
    _is_kernel_managed_name,
    _kernel_index_name,
    reconcile_indexes,
)


# --- Fixtures ---


def _field(
    type: str = "str",
    required: bool = False,
    unique: bool = False,
    indexed: bool = False,
    sparse: bool = False,
    is_relationship: bool = False,
    is_state_field: bool = False,
):
    return SimpleNamespace(
        type=type,
        required=required,
        unique=unique,
        indexed=indexed,
        sparse=sparse,
        is_relationship=is_relationship,
        is_state_field=is_state_field,
        enum_values=None,
        relationship_target=None,
    )


def _index_def(fields: list[tuple[str, int]], unique: bool = False, sparse: bool = False):
    """Build an IndexDef stand-in. Real IndexDef has `fields: list[tuple]`,
    `unique: bool`, and `sparse: bool`. SimpleNamespace mirrors the shape."""
    return SimpleNamespace(fields=fields, unique=unique, sparse=sparse)


def _definition(
    name: str = "Sample",
    fields: dict | None = None,
    indexes: list | None = None,
):
    return SimpleNamespace(
        name=name,
        collection_name=f"{name.lower()}s",
        fields=fields or {},
        indexes=indexes or [],
    )


def _fake_collection(existing_indexes: list[dict]):
    """Build a collection mock that mimics Motor's API surface.

    `list_indexes()` returns an async cursor — implemented as an async
    generator. `drop_index` and `create_index` are AsyncMocks so tests
    can assert call args.
    """
    coll = MagicMock()
    coll.name = "samples"

    async def _list_indexes_iter():
        for idx in existing_indexes:
            yield idx

    coll.list_indexes = MagicMock(return_value=_list_indexes_iter())
    coll.drop_index = AsyncMock()
    coll.create_index = AsyncMock()
    return coll


# --- _kernel_index_name ---


def test_kernel_index_name_single_field():
    assert _kernel_index_name([("org_id", 1)]) == "org_id_1"


def test_kernel_index_name_compound():
    assert _kernel_index_name([("org_id", 1), ("status", 1)]) == "org_id_1_status_1"


def test_kernel_index_name_descending():
    assert _kernel_index_name([("created_at", -1)]) == "created_at_-1"


# --- _is_kernel_managed_name ---


def test_id_index_is_not_kernel_managed():
    """The MongoDB primary index is owned by the database — never droppable."""
    assert _is_kernel_managed_name("_id_") is False


def test_org_id_prefixed_names_are_kernel_managed():
    assert _is_kernel_managed_name("org_id_1") is True
    assert _is_kernel_managed_name("org_id_1_status_1") is True
    assert _is_kernel_managed_name("org_id_1_external_ref_1") is True


def test_custom_named_indexes_are_not_kernel_managed():
    """Operator-added indexes don't follow the kernel naming convention and
    must be preserved during reconciliation."""
    assert _is_kernel_managed_name("meetings_search_idx") is False
    assert _is_kernel_managed_name("custom_compound_index") is False
    assert _is_kernel_managed_name("text_search_2dsphere") is False


# --- _desired_indexes ---


def test_desired_always_includes_org_id():
    """Every entity collection always has an org_id index."""
    desired = _desired_indexes(_definition())
    assert "org_id_1" in desired
    assert desired["org_id_1"]["unique"] is False


def test_desired_includes_indexdef_compound_indexes_with_org_id_prefix():
    indexes = [_index_def(fields=[("status", 1), ("created_at", -1)], unique=False)]
    desired = _desired_indexes(_definition(indexes=indexes))
    name = "org_id_1_status_1_created_at_-1"
    assert name in desired
    assert desired[name]["unique"] is False


def test_desired_includes_unique_field_indexes():
    fields = {"email": _field(unique=True)}
    desired = _desired_indexes(_definition(fields=fields))
    assert "org_id_1_email_1" in desired
    assert desired["org_id_1_email_1"]["unique"] is True


def test_desired_includes_indexed_field_indexes_non_unique():
    fields = {"status": _field(indexed=True)}
    desired = _desired_indexes(_definition(fields=fields))
    assert "org_id_1_status_1" in desired
    assert desired["org_id_1_status_1"]["unique"] is False


def test_desired_unique_takes_precedence_over_indexed():
    """If a field is BOTH unique and indexed, the unique flag wins. They
    produce the same compound key shape so the unique version is what's
    stored."""
    fields = {"email": _field(unique=True, indexed=True)}
    desired = _desired_indexes(_definition(fields=fields))
    assert desired["org_id_1_email_1"]["unique"] is True


# --- reconcile_indexes — drop ---


@pytest.mark.asyncio
async def test_drops_stale_kernel_managed_index_not_in_desired():
    """The Meeting case: a stale `org_id_1_external_ref_1` unique index
    exists, but the current definition declares no unique on external_ref.
    Reconciliation drops it.
    """
    existing = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "org_id_1", "key": {"org_id": 1}},
        {"name": "org_id_1_external_ref_1", "key": {"org_id": 1, "external_ref": 1}, "unique": True},
    ]
    coll = _fake_collection(existing)
    defn = _definition(name="Meeting", fields={"title": _field(required=True)})
    summary = await reconcile_indexes(coll, defn)
    assert "org_id_1_external_ref_1" in summary["dropped"]
    coll.drop_index.assert_called_once_with("org_id_1_external_ref_1")


@pytest.mark.asyncio
async def test_does_not_drop_id_primary_index():
    """`_id_` is owned by MongoDB and never droppable."""
    existing = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "org_id_1", "key": {"org_id": 1}},
    ]
    coll = _fake_collection(existing)
    defn = _definition()
    await reconcile_indexes(coll, defn)
    # _id_ never goes through drop_index.
    drop_calls = [call.args[0] for call in coll.drop_index.call_args_list]
    assert "_id_" not in drop_calls


@pytest.mark.asyncio
async def test_does_not_drop_custom_named_indexes():
    """Operator-added indexes (custom names) are preserved."""
    existing = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "org_id_1", "key": {"org_id": 1}},
        {"name": "meetings_search_idx", "key": {"title": "text"}},  # Custom text index
    ]
    coll = _fake_collection(existing)
    defn = _definition()
    summary = await reconcile_indexes(coll, defn)
    assert "meetings_search_idx" in summary["preserved"]
    coll.drop_index.assert_not_called()


# --- reconcile_indexes — create ---


@pytest.mark.asyncio
async def test_creates_missing_org_id_index():
    """Empty collection (only _id_) gets the org_id index created."""
    existing = [{"name": "_id_", "key": {"_id": 1}}]
    coll = _fake_collection(existing)
    defn = _definition()
    summary = await reconcile_indexes(coll, defn)
    assert "org_id_1" in summary["created"]
    coll.create_index.assert_called_once_with([("org_id", 1)], unique=False)


@pytest.mark.asyncio
async def test_creates_missing_unique_field_index():
    existing = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "org_id_1", "key": {"org_id": 1}},
    ]
    coll = _fake_collection(existing)
    defn = _definition(fields={"email": _field(unique=True)})
    summary = await reconcile_indexes(coll, defn)
    assert "org_id_1_email_1" in summary["created"]
    coll.create_index.assert_called_once_with([("org_id", 1), ("email", 1)], unique=True)


@pytest.mark.asyncio
async def test_idempotent_on_already_correct_state():
    """Indexes that already match the desired set are not dropped, not
    re-created. Reconciliation is idempotent."""
    existing = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "org_id_1", "key": {"org_id": 1}},
        {"name": "org_id_1_email_1", "key": {"org_id": 1, "email": 1}, "unique": True},
    ]
    coll = _fake_collection(existing)
    defn = _definition(fields={"email": _field(unique=True)})
    summary = await reconcile_indexes(coll, defn)
    assert summary["created"] == []
    assert summary["dropped"] == []
    coll.drop_index.assert_not_called()
    coll.create_index.assert_not_called()


# --- reconcile_indexes — drop+create together ---


@pytest.mark.asyncio
async def test_drops_stale_and_creates_new_in_one_pass():
    """Realistic scenario: definition was modified — old index goes, new
    index comes. Both happen in one reconciliation."""
    existing = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "org_id_1", "key": {"org_id": 1}},
        {"name": "org_id_1_external_ref_1", "key": {"org_id": 1, "external_ref": 1}, "unique": True},
    ]
    coll = _fake_collection(existing)
    # New definition: external_ref is no longer unique; status is now indexed.
    defn = _definition(
        fields={
            "external_ref": _field(unique=False),
            "status": _field(indexed=True),
        }
    )
    summary = await reconcile_indexes(coll, defn)
    assert "org_id_1_external_ref_1" in summary["dropped"]
    assert "org_id_1_status_1" in summary["created"]


@pytest.mark.asyncio
async def test_summary_keys_present_even_when_empty():
    """Callers always get a dict with all three buckets, even if no work."""
    existing = [{"name": "_id_", "key": {"_id": 1}}]
    coll = _fake_collection(existing)
    defn = _definition()
    summary = await reconcile_indexes(coll, defn)
    assert "created" in summary
    assert "dropped" in summary
    assert "preserved" in summary


# --- Sparse and option-mismatch (Apr 27 Alliance trace second-order finding) ---


def test_desired_translates_field_sparse_to_partial_filter_by_type():
    """Field-level sparse=True translates to a partialFilterExpression that
    excludes both null and missing — using the field's BSON $type.
    """
    fields = {"external_ref": _field(type="str", unique=True, sparse=True)}
    desired = _desired_indexes(_definition(fields=fields))
    spec = desired["org_id_1_external_ref_1"]
    assert spec["unique"] is True
    assert spec["partialFilter"] == {"external_ref": {"$type": "string"}}


def test_desired_partial_filter_uses_correct_bson_type_per_field_type():
    """Each scalar field type maps to its corresponding BSON $type."""
    fields = {
        "ref": _field(type="objectid", unique=True, sparse=True),
    }
    desired = _desired_indexes(_definition(fields=fields))
    assert desired["org_id_1_ref_1"]["partialFilter"] == {"ref": {"$type": "objectId"}}


def test_desired_indexdef_does_not_get_partial_filter():
    """Compound IndexDef indexes don't carry per-field type metadata in
    the spec, so they're not translated to partial filters in this build.
    The IndexDef.sparse flag is recorded but no partialFilter emitted."""
    indexes = [_index_def(fields=[("external_ref", 1)], unique=True, sparse=True)]
    desired = _desired_indexes(_definition(indexes=indexes))
    spec = desired["org_id_1_external_ref_1"]
    assert spec["unique"] is True
    assert spec["partialFilter"] is None


def test_desired_partial_filter_none_when_field_not_sparse():
    """Non-sparse fields don't get a partial filter."""
    fields = {"email": _field(type="str", unique=True)}
    desired = _desired_indexes(_definition(fields=fields))
    assert desired["org_id_1_email_1"]["partialFilter"] is None


@pytest.mark.asyncio
async def test_create_pass_passes_partial_filter_to_motor():
    """When the field is sparse, create_index gets partialFilterExpression
    instead of (the broken-for-explicit-null) sparse=True."""
    existing = [{"name": "_id_", "key": {"_id": 1}}]
    coll = _fake_collection(existing)
    fields = {"external_ref": _field(type="str", unique=True, sparse=True)}
    defn = _definition(fields=fields)
    await reconcile_indexes(coll, defn)
    create_calls = coll.create_index.call_args_list
    partial_calls = [c for c in create_calls if "partialFilterExpression" in c.kwargs]
    assert len(partial_calls) == 1, f"Expected one partial-filter create, got: {create_calls}"
    assert partial_calls[0].args[0] == [("org_id", 1), ("external_ref", 1)]
    assert partial_calls[0].kwargs["unique"] is True
    assert partial_calls[0].kwargs["partialFilterExpression"] == {"external_ref": {"$type": "string"}}
    # Ensure the kernel does NOT send sparse= alongside (MongoDB rejects both).
    assert "sparse" not in partial_calls[0].kwargs


@pytest.mark.asyncio
async def test_drops_and_recreates_index_when_partial_filter_flips_in():
    """The Meeting case: existing `org_id_1_external_ref_1` has no
    partialFilterExpression; the new definition's sparse flag adds one.
    Reconciler must drop+recreate, not preserve.
    """
    existing = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "org_id_1", "key": {"org_id": 1}},
        {
            "name": "org_id_1_external_ref_1",
            "key": {"org_id": 1, "external_ref": 1},
            "unique": True,
            # No partialFilterExpression set → defaults to None
        },
    ]
    coll = _fake_collection(existing)
    fields = {"external_ref": _field(type="str", unique=True, sparse=True)}
    defn = _definition(fields=fields)
    summary = await reconcile_indexes(coll, defn)
    # Drop happened.
    assert "org_id_1_external_ref_1" in summary["dropped"]
    coll.drop_index.assert_called_once_with("org_id_1_external_ref_1")
    # Recreate happened — with partialFilterExpression this time.
    create_calls = coll.create_index.call_args_list
    rebuild = [c for c in create_calls if c.args[0] == [("org_id", 1), ("external_ref", 1)]]
    assert len(rebuild) == 1
    assert rebuild[0].kwargs == {
        "unique": True,
        "partialFilterExpression": {"external_ref": {"$type": "string"}},
    }


@pytest.mark.asyncio
async def test_drops_and_recreates_when_unique_flips():
    """If a field's unique flag changes, the existing index has wrong
    semantics — must drop and rebuild."""
    existing = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "org_id_1", "key": {"org_id": 1}},
        {"name": "org_id_1_email_1", "key": {"org_id": 1, "email": 1}},  # non-unique
    ]
    coll = _fake_collection(existing)
    defn = _definition(fields={"email": _field(unique=True)})  # now unique
    summary = await reconcile_indexes(coll, defn)
    assert "org_id_1_email_1" in summary["dropped"]
    create_calls = [
        c for c in coll.create_index.call_args_list
        if c.args[0] == [("org_id", 1), ("email", 1)]
    ]
    assert len(create_calls) == 1
    assert create_calls[0].kwargs["unique"] is True


@pytest.mark.asyncio
async def test_preserves_partial_filter_index_when_options_already_match():
    """Idempotency carries through to options — a partial-filter unique
    index that matches the desired spec is preserved without churn."""
    existing = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "org_id_1", "key": {"org_id": 1}},
        {
            "name": "org_id_1_external_ref_1",
            "key": {"org_id": 1, "external_ref": 1},
            "unique": True,
            "partialFilterExpression": {"external_ref": {"$type": "string"}},
        },
    ]
    coll = _fake_collection(existing)
    fields = {"external_ref": _field(type="str", unique=True, sparse=True)}
    defn = _definition(fields=fields)
    summary = await reconcile_indexes(coll, defn)
    assert "org_id_1_external_ref_1" in summary["preserved"]
    assert summary["dropped"] == []
    coll.drop_index.assert_not_called()
