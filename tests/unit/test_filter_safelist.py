"""Tests for kernel.api._filter_safelist.parse_filter.

Bug #23 — `bulk-delete` (and the list endpoint) silently dropped MongoDB
operator filters because string operands don't compare to typed values
in MongoDB. The safelist parser fixes this by:

  - field-name safelisting against `entity_cls.model_fields`
  - operator allowlist ($in, $nin, $ne, $gt/$gte/$lt/$lte, $exists)
  - per-field type coercion: ObjectId hex / $oid -> bson.ObjectId,
    ISO 8601 / $date -> datetime
  - rejecting unknown operators, top-level $-prefixed keys, nested
    operator dicts, and wrong operand shapes

These tests pin all of the above. The list endpoint integration tests
remain in test_list_filter_parser.py (which now exercises the operator
path through the same shared parser).
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest
from bson import ObjectId
from fastapi import HTTPException

from kernel.api._filter_safelist import parse_filter


# --- Fixtures ---


def _field(annotation, alias=None):
    return SimpleNamespace(annotation=annotation, alias=alias)


def _cls(model_fields: dict):
    return SimpleNamespace(model_fields=model_fields)


# --- Trivial paths ---


def test_none_returns_empty():
    assert parse_filter(_cls({}), "Email", None) == {}


def test_empty_string_raises_400():
    """An empty string isn't valid JSON. Surface the error so callers can fix it."""
    with pytest.raises(HTTPException) as exc:
        parse_filter(_cls({}), "Email", "")
    assert exc.value.status_code == 400


def test_empty_dict_returns_empty():
    assert parse_filter(_cls({}), "Email", {}) == {}


def test_empty_object_string_returns_empty():
    assert parse_filter(_cls({}), "Email", "{}") == {}


def test_accepts_dict_directly():
    """Bulk operations pass an already-parsed dict, not a JSON string."""
    cls = _cls({"status": _field(str)})
    assert parse_filter(cls, "Email", {"status": "classified"}) == {"status": "classified"}


def test_accepts_json_string():
    """The list endpoint passes a JSON string from ?filter=..."""
    cls = _cls({"status": _field(str)})
    assert parse_filter(cls, "Email", '{"status": "classified"}') == {"status": "classified"}


# --- Invalid input ---


def test_rejects_invalid_json_string():
    with pytest.raises(HTTPException) as exc:
        parse_filter(_cls({}), "Email", "not json{")
    assert exc.value.status_code == 400
    assert "Invalid JSON" in str(exc.value.detail)


def test_rejects_non_object_top_level():
    """Arrays, scalars, strings as top-level filter input are rejected."""
    cls = _cls({})
    for bad in ['"just a string"', "[1,2,3]", "42", "null"]:
        with pytest.raises(HTTPException) as exc:
            parse_filter(cls, "Email", bad)
        assert exc.value.status_code == 400
        assert "must be a JSON object" in str(exc.value.detail)


def test_rejects_unknown_fields():
    cls = _cls({"title": _field(str), "status": _field(str)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Email", {"copmany": "x"})
    assert exc.value.status_code == 400
    detail = str(exc.value.detail)
    assert "Unknown field 'copmany'" in detail
    # Surfaces known fields so caller can fix the typo.
    assert "title" in detail or "status" in detail


def test_rejects_top_level_logical_operators():
    """$or / $and / $not at the top level are out of scope and would bypass
    field safelisting if allowed. Reject with a clear message."""
    cls = _cls({"status": _field(str)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Email", {"$or": [{"status": "a"}, {"status": "b"}]})
    assert exc.value.status_code == 400
    assert "Top-level operator" in str(exc.value.detail)


# --- Equality matches with type coercion ---


def test_simple_string_equality():
    cls = _cls({"status": _field(str)})
    assert parse_filter(cls, "Email", {"status": "classified"}) == {"status": "classified"}


def test_int_equality_passes_through():
    cls = _cls({"count": _field(int)})
    assert parse_filter(cls, "Sample", {"count": 5}) == {"count": 5}


def test_null_equality_passes_through():
    """A literal null matches docs where the field is null. Don't coerce."""
    cls = _cls({"company": _field(Optional[ObjectId])})
    assert parse_filter(cls, "Email", {"company": None}) == {"company": None}


def test_objectid_hex_string_coerced():
    cls = _cls({"company": _field(ObjectId)})
    hex_id = "69eb95f22b0a508618923977"
    result = parse_filter(cls, "Email", {"company": hex_id})
    assert isinstance(result["company"], ObjectId)
    assert str(result["company"]) == hex_id


def test_optional_objectid_field_also_coerced():
    """Optional[ObjectId] unwraps and coerces."""
    cls = _cls({"company": _field(Optional[ObjectId])})
    hex_id = "69eb95f22b0a508618923977"
    result = parse_filter(cls, "Email", {"company": hex_id})
    assert isinstance(result["company"], ObjectId)


def test_invalid_objectid_hex_raises_400():
    cls = _cls({"company": _field(ObjectId)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Email", {"company": "not-a-hex-string"})
    assert exc.value.status_code == 400
    assert "ObjectId" in str(exc.value.detail) or "hex" in str(exc.value.detail)


def test_extended_json_oid_coerced():
    """{"$oid": "<hex>"} -> bson.ObjectId — even on plain equality."""
    cls = _cls({"_id": _field(ObjectId)})
    hex_id = "69eb95f22b0a508618923977"
    result = parse_filter(cls, "Email", {"_id": {"$oid": hex_id}})
    assert isinstance(result["_id"], ObjectId)
    assert str(result["_id"]) == hex_id


def test_iso_datetime_string_coerced_for_datetime_field():
    """ISO 8601 strings on datetime fields coerce — otherwise MongoDB compares
    strings, not dates, and the filter silently no-matches."""
    cls = _cls({"created_at": _field(datetime)})
    result = parse_filter(cls, "Meeting", {"created_at": "2026-04-23T12:00:00Z"})
    assert isinstance(result["created_at"], datetime)


def test_extended_json_date_coerced():
    cls = _cls({"created_at": _field(datetime)})
    result = parse_filter(
        cls, "Meeting", {"created_at": {"$date": "2026-04-23T12:00:00Z"}}
    )
    assert isinstance(result["created_at"], datetime)


def test_invalid_iso_datetime_raises_400():
    cls = _cls({"created_at": _field(datetime)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Meeting", {"created_at": "not-a-date"})
    assert exc.value.status_code == 400
    assert "datetime" in str(exc.value.detail) or "ISO" in str(exc.value.detail)


def test_string_field_does_not_coerce_24_hex_string():
    """A 24-char string on a regular `str` field is NOT coerced to ObjectId
    just because the length matches."""
    cls = _cls({"external_ref": _field(str)})
    val = "69eb95f22b0a508618923977"
    result = parse_filter(cls, "Email", {"external_ref": val})
    assert isinstance(result["external_ref"], str)
    assert result["external_ref"] == val


# --- Operator dicts ---


def test_in_operator_with_objectid_hex_strings():
    """The most common Bug #23 case: $in on _id with hex strings.
    Each list element is coerced to ObjectId."""
    cls = _cls({"_id": _field(ObjectId)})
    ids = ["69eb95f22b0a508618923977", "69eb95f22b0a508618923988"]
    result = parse_filter(cls, "Company", {"_id": {"$in": ids}})
    assert "$in" in result["_id"]
    assert all(isinstance(x, ObjectId) for x in result["_id"]["$in"])
    assert [str(x) for x in result["_id"]["$in"]] == ids


def test_in_operator_with_extended_json_oid_elements():
    cls = _cls({"_id": _field(ObjectId)})
    result = parse_filter(
        cls, "Company", {"_id": {"$in": [{"$oid": "69eb95f22b0a508618923977"}]}}
    )
    assert isinstance(result["_id"]["$in"][0], ObjectId)


def test_nin_operator_coerces():
    cls = _cls({"_id": _field(ObjectId)})
    hex_id = "69eb95f22b0a508618923977"
    result = parse_filter(cls, "Company", {"_id": {"$nin": [hex_id]}})
    assert isinstance(result["_id"]["$nin"][0], ObjectId)


def test_in_operator_with_strings_on_string_field():
    cls = _cls({"name": _field(str)})
    result = parse_filter(cls, "Company", {"name": {"$in": ["Acme", "Beta"]}})
    assert result == {"name": {"$in": ["Acme", "Beta"]}}


def test_in_operator_requires_list_operand():
    cls = _cls({"_id": _field(ObjectId)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Company", {"_id": {"$in": "69eb95f22b0a508618923977"}})
    assert exc.value.status_code == 400
    assert "list" in str(exc.value.detail)


def test_gte_operator_coerces_datetime_string():
    """The trace example: --filter '{"created_at": {"$gte": "2026-04-23T..."}}'
    silently no-matched because string comparison vs stored datetime."""
    cls = _cls({"created_at": _field(datetime)})
    result = parse_filter(
        cls, "Meeting", {"created_at": {"$gte": "2026-04-23T00:00:00Z"}}
    )
    assert isinstance(result["created_at"]["$gte"], datetime)


def test_gte_operator_with_extended_json_date():
    cls = _cls({"created_at": _field(datetime)})
    result = parse_filter(
        cls,
        "Meeting",
        {"created_at": {"$gte": {"$date": "2026-04-23T00:00:00Z"}}},
    )
    assert isinstance(result["created_at"]["$gte"], datetime)


def test_compound_operators_on_same_field():
    """$gte + $lt on a datetime field — both operands coerced."""
    cls = _cls({"created_at": _field(datetime)})
    result = parse_filter(
        cls,
        "Meeting",
        {
            "created_at": {
                "$gte": "2026-04-23T00:00:00Z",
                "$lt": "2026-04-24T00:00:00Z",
            }
        },
    )
    assert isinstance(result["created_at"]["$gte"], datetime)
    assert isinstance(result["created_at"]["$lt"], datetime)


def test_ne_operator_coerces():
    cls = _cls({"company": _field(ObjectId)})
    hex_id = "69eb95f22b0a508618923977"
    result = parse_filter(cls, "Email", {"company": {"$ne": hex_id}})
    assert isinstance(result["company"]["$ne"], ObjectId)


def test_ne_with_null():
    """{"$ne": null} matches docs where the field is set."""
    cls = _cls({"company": _field(Optional[ObjectId])})
    result = parse_filter(cls, "Email", {"company": {"$ne": None}})
    assert result == {"company": {"$ne": None}}


def test_exists_operator_takes_bool():
    cls = _cls({"company": _field(Optional[ObjectId])})
    result = parse_filter(cls, "Email", {"company": {"$exists": True}})
    assert result == {"company": {"$exists": True}}


def test_exists_operator_rejects_non_bool():
    cls = _cls({"company": _field(Optional[ObjectId])})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Email", {"company": {"$exists": "yes"}})
    assert exc.value.status_code == 400
    assert "boolean" in str(exc.value.detail)


def test_unknown_operator_rejected():
    """$where / $regex / etc. are not in the safelist — reject explicitly."""
    cls = _cls({"name": _field(str)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Company", {"name": {"$regex": "^Acme"}})
    assert exc.value.status_code == 400
    assert "$regex" in str(exc.value.detail)
    assert "safelist" in str(exc.value.detail) or "Allowed" in str(exc.value.detail)


def test_nested_operator_dict_rejected():
    """{"$gte": {"$ne": ...}} would be a compositional disaster. Reject it."""
    cls = _cls({"created_at": _field(datetime)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(
            cls,
            "Meeting",
            {"created_at": {"$gte": {"$ne": "2026-04-23T00:00:00Z"}}},
        )
    assert exc.value.status_code == 400
    assert "Nested" in str(exc.value.detail)


def test_invalid_objectid_in_in_list_raises():
    """One bad element in $in fails the whole filter — fail fast and loud."""
    cls = _cls({"_id": _field(ObjectId)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Company", {"_id": {"$in": ["not-hex"]}})
    assert exc.value.status_code == 400


def test_invalid_oid_extended_json_raises():
    cls = _cls({"_id": _field(ObjectId)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Company", {"_id": {"$oid": "not-hex"}})
    assert exc.value.status_code == 400
    assert "$oid" in str(exc.value.detail)


def test_invalid_date_extended_json_raises():
    cls = _cls({"created_at": _field(datetime)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Meeting", {"created_at": {"$date": "not-a-date"}})
    assert exc.value.status_code == 400
    assert "$date" in str(exc.value.detail)


def test_extended_json_with_extra_keys_does_not_unwrap():
    """{"$oid": "...", "other": "x"} is not extended-JSON shape (multiple keys)
    — pass through as a regular dict-equality match."""
    cls = _cls({"data": _field(dict)})
    payload = {"$oid": "abc", "other": "x"}
    result = parse_filter(cls, "Sample", {"data": payload})
    assert result == {"data": payload}


# --- Multi-field combinations ---


def test_multiple_fields_with_mix_of_equality_and_operators():
    cls = _cls(
        {
            "_id": _field(ObjectId),
            "status": _field(str),
            "created_at": _field(datetime),
        }
    )
    hex_a = "69eb95f22b0a508618923977"
    hex_b = "69eb95f22b0a508618923988"
    result = parse_filter(
        cls,
        "Company",
        {
            "_id": {"$in": [hex_a, hex_b]},
            "status": "active",
            "created_at": {"$gte": "2026-04-23T00:00:00Z"},
        },
    )
    assert all(isinstance(x, ObjectId) for x in result["_id"]["$in"])
    assert result["status"] == "active"
    assert isinstance(result["created_at"]["$gte"], datetime)


def test_unknown_field_in_multi_field_filter_still_rejected():
    cls = _cls({"status": _field(str)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Email", {"status": "a", "unknown_field": "x"})
    assert exc.value.status_code == 400
    assert "Unknown field" in str(exc.value.detail)


# --- Pydantic field aliases (e.g. id <-> _id) ---


def test_alias_accepted_and_emits_alias_name():
    """DomainBaseEntity declares `id: ObjectId = Field(alias="_id")`. Callers
    naturally pass `_id` because that's the MongoDB-stored field name. The
    safelist must accept `_id` AND emit it as the dict key so MongoDB matches
    the stored document. Caught live during Bug #23 verification."""
    cls = _cls({"id": _field(ObjectId, alias="_id")})
    hex_id = "69eb95f22b0a508618923977"
    result = parse_filter(cls, "Company", {"_id": hex_id})
    assert "_id" in result
    assert isinstance(result["_id"], ObjectId)


def test_canonical_name_also_accepted_and_translated_to_alias():
    """If the caller uses the Python canonical name (`id`), we accept it but
    emit the alias (`_id`) — because that's the field name MongoDB indexes."""
    cls = _cls({"id": _field(ObjectId, alias="_id")})
    hex_id = "69eb95f22b0a508618923977"
    result = parse_filter(cls, "Company", {"id": hex_id})
    assert "_id" in result
    assert "id" not in result


def test_alias_works_with_in_operator():
    cls = _cls({"id": _field(ObjectId, alias="_id")})
    hex_a = "69eb95f22b0a508618923977"
    hex_b = "69eb95f22b0a508618923988"
    result = parse_filter(cls, "Company", {"_id": {"$in": [hex_a, hex_b]}})
    assert "_id" in result
    assert all(isinstance(x, ObjectId) for x in result["_id"]["$in"])


def test_field_with_no_alias_uses_canonical_name():
    """No-alias fields keep their canonical name in the output (no spurious
    rewriting)."""
    cls = _cls({"status": _field(str, alias=None)})
    result = parse_filter(cls, "Email", {"status": "active"})
    assert result == {"status": "active"}
