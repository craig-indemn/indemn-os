"""Tests for scripts/migrate_relationship_integrity.py (Stage B B3).

Spec-driven coverage of:
  - field_category D16 (deleted entity-type) / D17 (EvaluationResult preserve) rules
  - dangling detection for scalar / list / polymorphic fields against a fake DB
  - malformed-value + polymorphic-asymmetric handling
  - dry-run safety (commit path raises until enabled at B4/Session 38)
  - markdown report format (headline + category table)
"""

import importlib.util
import pathlib

import pytest
from bson import ObjectId

# Load the script (scripts/ is not an importable package).
_SCRIPT = pathlib.Path(__file__).parents[2] / "scripts" / "migrate_relationship_integrity.py"
_spec = importlib.util.spec_from_file_location("migrate_relationship_integrity", _SCRIPT)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


# --- Fake async Mongo -------------------------------------------------------

class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return self._docs


class _Collection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, query, projection=None):
        _id = query.get("_id")
        if isinstance(_id, dict) and "$in" in _id:
            wanted = {str(x) for x in _id["$in"]}
            return _Cursor([d for d in self._docs if str(d["_id"]) in wanted])
        return _Cursor(list(self._docs))


class _DB:
    def __init__(self, collections):
        self._c = collections

    def __getitem__(self, name):
        return self._c.get(name, _Collection([]))


# --- field_category (pure; D16 / D17) --------------------------------------

def test_field_category_evaluationresult_preserved_d17():
    assert mig.field_category("EvaluationResult", "Evaluator", {"Evaluator"}) == mig.CAT_EVALRESULT_HISTORICAL


def test_field_category_deleted_entity_type_d16():
    # CustomerSystem was wiped Session 26 — a field still targeting it is D16.
    assert mig.field_category("Company", "CustomerSystem", {"Company"}) == mig.CAT_DELETED_TYPE


def test_field_category_known_domain_target_regular():
    assert mig.field_category("Task", "Touchpoint", {"Task", "Touchpoint"}) == mig.CAT_REGULAR


def test_field_category_kernel_target_regular():
    # Actor is a kernel entity (no EntityDefinition) but a valid target.
    assert mig.field_category("Company", "Actor", set()) == mig.CAT_REGULAR


def test_field_category_polymorphic_none_target_regular():
    assert mig.field_category("Touchpoint", None, set()) == mig.CAT_REGULAR


# --- scan_field: scalar -----------------------------------------------------

@pytest.mark.asyncio
async def test_scan_field_scalar_detects_dangling():
    dead = ObjectId()
    db = _DB({"tasks": _Collection([{"_id": ObjectId(), "touchpoint": dead}]),
              "touchpoints": _Collection([])})  # dead not present
    spec = {"entity_type": "Task", "collection": "tasks", "field": "touchpoint",
            "kind": "scalar", "target": "Touchpoint", "type_field": None}
    finding = await mig.scan_field(db, spec, {"Task": "tasks", "Touchpoint": "touchpoints"},
                                   {"Task", "Touchpoint"}, {})
    assert finding["dangling_count"] == 1
    assert finding["malformed_count"] == 0
    assert finding["category"] == mig.CAT_REGULAR


@pytest.mark.asyncio
async def test_scan_field_scalar_target_exists_no_dangling():
    live = ObjectId()
    db = _DB({"tasks": _Collection([{"_id": ObjectId(), "touchpoint": live}]),
              "touchpoints": _Collection([{"_id": live}])})  # target exists
    spec = {"entity_type": "Task", "collection": "tasks", "field": "touchpoint",
            "kind": "scalar", "target": "Touchpoint", "type_field": None}
    finding = await mig.scan_field(db, spec, {"Task": "tasks", "Touchpoint": "touchpoints"},
                                   {"Task", "Touchpoint"}, {})
    assert finding["dangling_count"] == 0


@pytest.mark.asyncio
async def test_scan_field_scalar_malformed_value():
    # the Email.touchpoint "[object Object]" residue — value is a dict, not an ObjectId
    db = _DB({"emails": _Collection([{"_id": ObjectId(), "touchpoint": {"name": "Oneleet"}}]),
              "touchpoints": _Collection([])})
    spec = {"entity_type": "Email", "collection": "emails", "field": "touchpoint",
            "kind": "scalar", "target": "Touchpoint", "type_field": None}
    finding = await mig.scan_field(db, spec, {"Email": "emails", "Touchpoint": "touchpoints"},
                                   {"Email", "Touchpoint"}, {})
    assert finding["malformed_count"] == 1


# --- scan_field: list -------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_field_list_dangling_element_and_preserves_all_live():
    dead, live = ObjectId(), ObjectId()
    db = _DB({"companys": _Collection([
                {"_id": ObjectId(), "systems": [live, dead]},  # has a dead element → dangling
                {"_id": ObjectId(), "systems": [live]},        # all live → NOT dangling
              ]),
              "systems": _Collection([{"_id": live}])})  # only `live` exists
    spec = {"entity_type": "Company", "collection": "companys", "field": "systems",
            "kind": "list", "target": "System", "type_field": None}
    finding = await mig.scan_field(db, spec, {"Company": "companys", "System": "systems"},
                                   {"Company", "System"}, {})
    assert finding["dangling_count"] == 1  # only the doc containing the dead id


# --- scan_field: polymorphic ------------------------------------------------

@pytest.mark.asyncio
async def test_scan_field_polymorphic_detects_dangling():
    dead_email = ObjectId()
    db = _DB({"touchpoints": _Collection([
                {"_id": ObjectId(), "source_entity_id": dead_email, "source_entity_type": "Email"}]),
              "emails": _Collection([])})  # dead_email missing
    spec = {"entity_type": "Touchpoint", "collection": "touchpoints", "field": "source_entity_id",
            "kind": "polymorphic", "target": None, "type_field": "source_entity_type"}
    finding = await mig.scan_field(db, spec, {"Touchpoint": "touchpoints", "Email": "emails"},
                                   {"Touchpoint", "Email"}, {})
    assert finding["dangling_count"] == 1


@pytest.mark.asyncio
async def test_scan_field_polymorphic_asymmetric_type_null():
    db = _DB({"touchpoints": _Collection([
                {"_id": ObjectId(), "source_entity_id": ObjectId(), "source_entity_type": None}])})
    spec = {"entity_type": "Touchpoint", "collection": "touchpoints", "field": "source_entity_id",
            "kind": "polymorphic", "target": None, "type_field": "source_entity_type"}
    finding = await mig.scan_field(db, spec, {"Touchpoint": "touchpoints"}, {"Touchpoint"}, {})
    # asymmetric (unresolvable target type) tracked as its own count; base category unchanged
    assert finding["asymmetric_count"] == 1
    assert finding["malformed_count"] == 0
    assert finding["category"] == mig.CAT_REGULAR


# --- scan_dangling: end-to-end aggregation ----------------------------------

@pytest.mark.asyncio
async def test_scan_dangling_aggregates_and_excludes_preserved_from_nullify():
    dead = ObjectId()
    er_dead = ObjectId()
    db = _DB({
        "entity_definitions": _Collection([
            {"name": "Task", "collection_name": "tasks",
             "fields": {"touchpoint": {"is_relationship": True, "type": "objectid",
                                       "relationship_target": "Touchpoint"}}},
            {"name": "EvaluationResult", "collection_name": "evaluation_results",
             "fields": {"evaluator_id": {"is_relationship": True, "type": "objectid",
                                         "relationship_target": "Evaluator"}}},
            {"name": "Touchpoint", "collection_name": "touchpoints", "fields": {}},
            {"name": "Evaluator", "collection_name": "evaluators", "fields": {}},
        ]),
        "tasks": _Collection([{"_id": ObjectId(), "touchpoint": dead}]),
        "evaluation_results": _Collection([{"_id": ObjectId(), "evaluator_id": er_dead}]),
        "touchpoints": _Collection([]),
        "evaluators": _Collection([]),
    })
    scan = await mig.scan_dangling(db, org_id=None)
    assert scan["total_dangling"] == 2  # Task.touchpoint + EvaluationResult.evaluator_id
    # EvaluationResult is preserved (D17) → excluded from the nullify total
    assert scan["total_to_nullify"] == 1
    assert scan["categories"].get(mig.CAT_EVALRESULT_HISTORICAL) == 1
    assert scan["categories"].get(mig.CAT_REGULAR) == 1


# --- dry-run safety + report format ----------------------------------------

@pytest.mark.asyncio
async def test_scan_dangling_buckets_malformed_and_asymmetric():
    """Malformed values + polymorphic-asymmetric refs land in their OWN report
    categories (not rolled into 'regular'), and both always count toward nullify."""
    db = _DB({
        "entity_definitions": _Collection([
            {"name": "Email", "collection_name": "emails",
             "fields": {"touchpoint": {"is_relationship": True, "type": "objectid",
                                       "relationship_target": "Touchpoint"}}},
            {"name": "Touchpoint", "collection_name": "touchpoints",
             "fields": {"source_entity_id": {"is_relationship": False, "type": "objectid",
                                             "is_polymorphic_relationship": True,
                                             "target_type_field": "source_entity_type"}}},
        ]),
        "emails": _Collection([{"_id": ObjectId(), "touchpoint": {"name": "x"}}]),  # malformed
        "touchpoints": _Collection([{"_id": ObjectId(), "source_entity_id": ObjectId(),
                                     "source_entity_type": None}]),  # asymmetric
    })
    scan = await mig.scan_dangling(db, org_id=None)
    assert scan["categories"].get(mig.CAT_MALFORMED) == 1
    assert scan["categories"].get(mig.CAT_POLY_ASYMMETRIC) == 1
    assert scan["total_to_nullify"] == 2  # both always nullify


@pytest.mark.asyncio
async def test_commit_is_gated_until_b4():
    """The --commit write path must not run before Craig's B4 sign-off (Session 38)."""
    with pytest.raises(NotImplementedError):
        await mig.commit_nullifications(None, {"fields": []}, actor_id="x", session_label="y")


def test_render_markdown_has_headline_and_category_table():
    scan = {
        "scanned_at": "2026-05-29T00:00:00+00:00", "mode": "DRY-RUN", "org_scope": "all orgs",
        "database": "indemn_os", "total_dangling": 276, "total_to_nullify": 276,
        "categories": {mig.CAT_REGULAR: 276},
        "fields": [{"entity_type": "Email", "collection": "emails", "field": "company",
                    "kind": "scalar", "target": "Company", "category": mig.CAT_REGULAR,
                    "dangling_count": 116, "malformed_count": 0,
                    "samples": [{"doc_id": "abc", "dead_ref": "def"}]}],
    }
    md = mig.render_markdown(scan)
    assert "# Stage B — Dangling-Refs Audit" in md
    assert "276 dangling/malformed refs" in md
    assert "## By category" in md
    assert "Email.company" in md
    assert "evaluationresult_historical (D17)" in md  # the preserve category is documented
