"""Tests for Stage B B2 — cascade extension to list-typed + polymorphic refs.

Per consolidated plan Stage B Phase B2 (+ Craig decisions D28, D29 locked Session 37):

- **List refs** (`field.type == "list"`, e.g. `Company.systems: list[->System]`):
  cascade `$pull`s the dead id from the list (NOT `$set: None`, which would
  clobber other live elements — the Session 26 bug). FieldChange shape per D28:
  `old_value=<full original list>, new_value=<list minus the dead id>` (symmetric
  with how `_compute_changes` records list-field UPDATES, save.py:439).

- **Polymorphic refs** (`field.is_polymorphic_relationship == True`, e.g.
  `Touchpoint.source_entity_id` paired with `source_entity_type`): cascade clears
  BOTH halves (id-field + type-field) in ONE ChangeRecord with 2 FieldChanges
  per D9 atomicity. Scanned for ANY deleted entity_type (the target type is
  dynamic per-doc), matched by the dead id (globally-unique ObjectId).

- **Scope per D29**: DOMAIN-source only. Kernel-entity-as-source cascade
  (Trace.entity_id) is intentionally OUT of B2 — preserved as historical (like
  D17). The function still iterates only `EntityDefinition` (domain) records;
  the D7 `_relationship_field_targets` ClassVar is NOT consulted here.

All cascade writes preserve the Dev#1 in-memory hash-chain batched pattern
(single insert_many for audits + per-(collection, field) batched update op).
"""

import inspect
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId


def _field(
    *,
    is_relationship=False,
    type="objectid",
    relationship_target=None,
    is_polymorphic_relationship=False,
    target_type_field=None,
):
    """Build a FieldDefinition-shaped mock with ALL relevant attrs set explicitly
    (mirrors the real FieldDefinition — definition.py:30-31 carry the poly flags)."""
    fd = MagicMock()
    fd.is_relationship = is_relationship
    fd.type = type
    fd.relationship_target = relationship_target
    fd.is_polymorphic_relationship = is_polymorphic_relationship
    fd.target_type_field = target_type_field
    return fd


def _defn(name, fields: dict):
    d = MagicMock()
    d.name = name
    d.fields = fields
    return d


@contextmanager
def cascade_harness(definitions, registry):
    """Patch the deps cascade_nullify_references touches; capture audit kwargs.

    Yields a dict with:
      - 'audit_kwargs': list of ChangeRecord(**kwargs) seen (in chain order)
      - 'audit_insert_many': AsyncMock for the changes-collection insert_many
      - 'update_calls': {defn_name: [(filter_doc, update_doc), ...]} per entity collection
    """
    audit_kwargs = []
    audit_records = []
    audit_insert_many = AsyncMock()
    update_calls = {name: [] for name in registry}

    def cr_factory(**kwargs):
        audit_kwargs.append(kwargs)
        m = MagicMock()
        m.model_dump = lambda by_alias=False: kwargs
        for k, v in kwargs.items():
            setattr(m, k, v)
        audit_records.append(m)  # previous_hash/current_hash are set on the instance post-construction
        return m

    # Wire each entity_cls's collection: find -> its docs, update_many -> capture
    for name, entity_cls in registry.items():
        collection = entity_cls.get_motor_collection()

        async def _upd(filter_doc, update_doc, _name=name):
            update_calls[_name].append((filter_doc, update_doc))
            return MagicMock(modified_count=len(filter_doc.get("_id", {}).get("$in", []) or [1]))

        collection.update_many = AsyncMock(side_effect=_upd)

    with patch(
        "kernel.entity.definition.EntityDefinition.find",
    ) as mock_find_defs, patch(
        "kernel.db.ENTITY_REGISTRY", registry,
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="genesis"),
    ), patch(
        "kernel.changes.hash_chain.compute_hash",
        side_effect=lambda r: f"hash-{len(audit_kwargs)}",
    ), patch(
        "kernel.entity.save.current_correlation_id",
        MagicMock(get=MagicMock(return_value="corr-1")),
    ), patch(
        "kernel.entity.save.current_effective_actor_id",
        MagicMock(get=MagicMock(return_value="eff-1")),
    ), patch(
        "kernel.context.current_actor_id",
        MagicMock(get=MagicMock(return_value="actor-1")),
    ):
        fq = MagicMock()
        fq.to_list = AsyncMock(return_value=definitions)
        mock_find_defs.return_value = fq
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=audit_insert_many)
        )
        MockCR.side_effect = cr_factory
        yield {
            "audit_kwargs": audit_kwargs,
            "audit_records": audit_records,
            "audit_insert_many": audit_insert_many,
            "update_calls": update_calls,
        }


def _entity_cls_returning(docs):
    """An entity class whose collection.find(...).to_list() returns `docs`."""
    fq = MagicMock()
    fq.to_list = AsyncMock(return_value=docs)
    collection = MagicMock(find=MagicMock(return_value=fq))
    cls = MagicMock()
    cls.get_motor_collection = MagicMock(return_value=collection)
    return cls


# --------------------------------------------------------------------------
# LIST-typed relationship cascade (D28 + D-D)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_cascade_uses_pull_not_set():
    """Deleting a list-target uses $pull (not $set:None) so the list isn't clobbered."""
    from kernel.entity import save

    dead = ObjectId("dddd11112222333344445555")
    live1, live2 = ObjectId(), ObjectId()
    c1 = ObjectId()
    docs = [{"_id": c1, "systems": [live1, dead, live2]}]

    defn = _defn("Company", {"systems": _field(
        is_relationship=True, type="list", relationship_target="System")})
    registry = {"Company": _entity_cls_returning(docs)}

    with cascade_harness([defn], registry) as cap:
        await save.cascade_nullify_references("System", dead, ObjectId())

    calls = cap["update_calls"]["Company"]
    assert len(calls) == 1
    filter_doc, update_doc = calls[0]
    assert update_doc == {"$pull": {"systems": dead}}
    assert "$set" not in update_doc


@pytest.mark.asyncio
async def test_list_cascade_fieldchange_full_old_to_list_minus_d28():
    """D28: FieldChange old_value = full original list, new_value = list minus dead id."""
    from kernel.entity import save

    dead = ObjectId("dddd11112222333344445555")
    live1, live2 = ObjectId(), ObjectId()
    docs = [{"_id": ObjectId(), "systems": [live1, dead, live2]}]

    defn = _defn("Company", {"systems": _field(
        is_relationship=True, type="list", relationship_target="System")})
    registry = {"Company": _entity_cls_returning(docs)}

    with cascade_harness([defn], registry) as cap:
        await save.cascade_nullify_references("System", dead, ObjectId())

    assert len(cap["audit_kwargs"]) == 1
    fcs = cap["audit_kwargs"][0]["changes"]
    assert len(fcs) == 1
    assert fcs[0].field == "systems"
    assert fcs[0].old_value == [live1, dead, live2]
    assert fcs[0].new_value == [live1, live2]


@pytest.mark.asyncio
async def test_list_cascade_preserves_other_live_elements():
    """The Session-26 bug guard: other live System refs survive the cascade."""
    from kernel.entity import save

    dead = ObjectId("dddd11112222333344445555")
    live1, live2 = ObjectId(), ObjectId()
    docs = [{"_id": ObjectId(), "systems": [live1, dead, live2]}]

    defn = _defn("Company", {"systems": _field(
        is_relationship=True, type="list", relationship_target="System")})
    registry = {"Company": _entity_cls_returning(docs)}

    with cascade_harness([defn], registry) as cap:
        await save.cascade_nullify_references("System", dead, ObjectId())

    new_list = cap["audit_kwargs"][0]["changes"][0].new_value
    assert live1 in new_list and live2 in new_list
    assert dead not in new_list


@pytest.mark.asyncio
async def test_list_cascade_change_type_and_metadata():
    """List cascade record carries change_type=cascade_nullify + correct metadata."""
    from kernel.entity import save

    dead = ObjectId("dddd11112222333344445555")
    docs = [{"_id": ObjectId(), "systems": [dead]}]
    defn = _defn("Company", {"systems": _field(
        is_relationship=True, type="list", relationship_target="System")})
    registry = {"Company": _entity_cls_returning(docs)}

    with cascade_harness([defn], registry) as cap:
        await save.cascade_nullify_references("System", dead, ObjectId())

    kw = cap["audit_kwargs"][0]
    assert kw["change_type"] == "cascade_nullify"
    assert kw["method"] == "relationship_target_deleted"
    assert kw["method_metadata"]["deleted_entity_type"] == "System"
    assert kw["method_metadata"]["affected_field_names"] == ["systems"]


# --------------------------------------------------------------------------
# POLYMORPHIC relationship cascade (D9 + D-D)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_polymorphic_cascade_clears_both_halves_one_record_d9():
    """D9: deleting the referenced Email clears source_entity_id + source_entity_type
    in ONE ChangeRecord with 2 FieldChanges; update sets both fields to None."""
    from kernel.entity import save

    dead_email = ObjectId("eeee11112222333344445555")
    tp = ObjectId()
    docs = [{"_id": tp, "source_entity_id": dead_email, "source_entity_type": "Email"}]

    defn = _defn("Touchpoint", {"source_entity_id": _field(
        is_relationship=False, type="objectid",
        is_polymorphic_relationship=True, target_type_field="source_entity_type")})
    registry = {"Touchpoint": _entity_cls_returning(docs)}

    with cascade_harness([defn], registry) as cap:
        await save.cascade_nullify_references("Email", dead_email, ObjectId())

    # one record, two FieldChanges
    assert len(cap["audit_kwargs"]) == 1
    fcs = cap["audit_kwargs"][0]["changes"]
    assert len(fcs) == 2
    by_field = {fc.field: fc for fc in fcs}
    assert by_field["source_entity_id"].old_value == dead_email
    assert by_field["source_entity_id"].new_value is None
    assert by_field["source_entity_type"].old_value == "Email"
    assert by_field["source_entity_type"].new_value is None
    # method_metadata records both affected fields
    assert set(cap["audit_kwargs"][0]["method_metadata"]["affected_field_names"]) == {
        "source_entity_id", "source_entity_type"}
    # update clears BOTH halves
    filter_doc, update_doc = cap["update_calls"]["Touchpoint"][0]
    assert update_doc == {"$set": {"source_entity_id": None, "source_entity_type": None}}


@pytest.mark.asyncio
async def test_polymorphic_cascade_scanned_for_any_target_type():
    """Polymorphic field has dynamic target — it's scanned regardless of the deleted
    entity_type (a Meeting deletion also clears matching Touchpoint.source_entity_id)."""
    from kernel.entity import save

    dead_meeting = ObjectId("4444aaaa2222333344445555")
    docs = [{"_id": ObjectId(), "source_entity_id": dead_meeting, "source_entity_type": "Meeting"}]

    defn = _defn("Touchpoint", {"source_entity_id": _field(
        is_relationship=False, type="objectid",
        is_polymorphic_relationship=True, target_type_field="source_entity_type")})
    registry = {"Touchpoint": _entity_cls_returning(docs)}

    # deleted type is "Meeting" — relationship_target is None on a poly field, so the
    # scalar `relationship_target != entity_type` skip must NOT apply to poly fields.
    with cascade_harness([defn], registry) as cap:
        total = await save.cascade_nullify_references("Meeting", dead_meeting, ObjectId())

    assert total == 1
    assert len(cap["update_calls"]["Touchpoint"]) == 1


@pytest.mark.asyncio
async def test_polymorphic_cascade_asymmetric_type_already_null():
    """Asymmetric: id-field set but type-field already null → clears gracefully,
    still records the pair (no crash)."""
    from kernel.entity import save

    dead_email = ObjectId("eeee11112222333344445555")
    docs = [{"_id": ObjectId(), "source_entity_id": dead_email, "source_entity_type": None}]

    defn = _defn("Touchpoint", {"source_entity_id": _field(
        is_relationship=False, type="objectid",
        is_polymorphic_relationship=True, target_type_field="source_entity_type")})
    registry = {"Touchpoint": _entity_cls_returning(docs)}

    with cascade_harness([defn], registry) as cap:
        total = await save.cascade_nullify_references("Email", dead_email, ObjectId())

    assert total == 1
    fcs = cap["audit_kwargs"][0]["changes"]
    by_field = {fc.field: fc for fc in fcs}
    assert by_field["source_entity_id"].old_value == dead_email
    assert by_field["source_entity_type"].old_value is None  # was already null
    assert by_field["source_entity_type"].new_value is None


# --------------------------------------------------------------------------
# MIXED batch + batched-pattern preservation + hash chain
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mixed_batch_scalar_list_polymorphic():
    """A single cascade touching scalar + list + polymorphic fields applies the
    right update operator to each and emits the right FieldChanges."""
    from kernel.entity import save

    dead = ObjectId("dddd11112222333344445555")  # pretend this id is the deleted target

    # scalar: Task.touchpoint -> Touchpoint (only fires if deleted type == Touchpoint)
    task_doc = {"_id": ObjectId(), "touchpoint": dead}
    task_defn = _defn("Task", {"touchpoint": _field(
        is_relationship=True, type="objectid", relationship_target="Touchpoint")})
    # list: Company.systems -> System (won't fire for a Touchpoint deletion)
    comp_doc = {"_id": ObjectId(), "systems": [dead, ObjectId()]}
    comp_defn = _defn("Company", {"systems": _field(
        is_relationship=True, type="list", relationship_target="Touchpoint")})  # target set to Touchpoint for this test
    # polymorphic: Touchpoint.source_entity_id (fires for any type via id match)
    tp_doc = {"_id": ObjectId(), "source_entity_id": dead, "source_entity_type": "Touchpoint"}
    tp_defn = _defn("Touchpoint", {"source_entity_id": _field(
        is_relationship=False, type="objectid",
        is_polymorphic_relationship=True, target_type_field="source_entity_type")})

    registry = {
        "Task": _entity_cls_returning([task_doc]),
        "Company": _entity_cls_returning([comp_doc]),
        "Touchpoint": _entity_cls_returning([tp_doc]),
    }

    with cascade_harness([task_defn, comp_defn, tp_defn], registry) as cap:
        total = await save.cascade_nullify_references("Touchpoint", dead, ObjectId())

    assert total == 3
    # scalar -> $set None
    assert cap["update_calls"]["Task"][0][1] == {"$set": {"touchpoint": None}}
    # list -> $pull
    assert cap["update_calls"]["Company"][0][1] == {"$pull": {"systems": dead}}
    # polymorphic -> $set both None
    assert cap["update_calls"]["Touchpoint"][0][1] == {
        "$set": {"source_entity_id": None, "source_entity_type": None}}


@pytest.mark.asyncio
async def test_cascade_extension_in_memory_batched_preserved():
    """All field kinds still flush audits via ONE insert_many (Dev#1 batched pattern)."""
    from kernel.entity import save

    dead = ObjectId("dddd11112222333344445555")
    list_docs = [{"_id": ObjectId(), "systems": [dead]} for _ in range(3)]
    defn = _defn("Company", {"systems": _field(
        is_relationship=True, type="list", relationship_target="System")})
    registry = {"Company": _entity_cls_returning(list_docs)}

    with cascade_harness([defn], registry) as cap:
        await save.cascade_nullify_references("System", dead, ObjectId())

    cap["audit_insert_many"].assert_called_once()
    docs = cap["audit_insert_many"].call_args[0][0]
    assert len(docs) == 3  # 3 affected entities -> 3 audit records in one insert_many


@pytest.mark.asyncio
async def test_cascade_extension_hash_chain_sequential_mixed():
    """In-memory hash chain stays sequential across mixed field-type records."""
    from kernel.entity import save

    dead = ObjectId("dddd11112222333344445555")
    list_docs = [{"_id": ObjectId(), "systems": [dead]}, {"_id": ObjectId(), "systems": [dead]}]
    tp_docs = [{"_id": ObjectId(), "source_entity_id": dead, "source_entity_type": "System"}]
    comp_defn = _defn("Company", {"systems": _field(
        is_relationship=True, type="list", relationship_target="System")})
    tp_defn = _defn("Touchpoint", {"source_entity_id": _field(
        is_relationship=False, type="objectid",
        is_polymorphic_relationship=True, target_type_field="source_entity_type")})
    registry = {
        "Company": _entity_cls_returning(list_docs),
        "Touchpoint": _entity_cls_returning(tp_docs),
    }

    with cascade_harness([comp_defn, tp_defn], registry) as cap:
        await save.cascade_nullify_references("System", dead, ObjectId())

    recs = cap["audit_records"]
    assert len(recs) == 3
    # each record's previous_hash == the prior record's current_hash (true sequential chain)
    assert recs[0].previous_hash == "genesis"
    assert recs[1].previous_hash == recs[0].current_hash
    assert recs[2].previous_hash == recs[1].current_hash


def test_cascade_extension_iterates_domain_definitions_only_d29():
    """Source pin (D29): kernel-source cascade is OUT of B2 — the function still
    iterates only EntityDefinition (domain) and does NOT consult the kernel
    `_relationship_field_targets` ClassVar. Kernel refs (Trace.entity_id) are
    preserved as historical; the D7 ClassVar lands in Session 38 / B5."""
    from kernel.entity import save

    src = inspect.getsource(save.cascade_nullify_references)
    assert "EntityDefinition.find" in src  # domain definitions iterated
    # strip the docstring so its explanatory mention of the deferred ClassVar
    # doesn't trip the code-reference guard; the CODE must not consult it.
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    assert "_relationship_field_targets" not in code_body
