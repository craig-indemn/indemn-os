"""Tests for kernel.api.registration._parse_list_filter.

Apr 27 — `GET /api/{entity}/` only supported `status`, `search`, `limit`,
`offset`, `sort`. Anything else (filter by company, lookup by external_ref,
reverse-lookup Meeting where touchpoint=X) was impossible from the API,
which forced associates to fetch + filter client-side or fail silently.

This module tests the new filter parser that backs the `?filter=`/`--data`
query parameter. The parser:
  - rejects non-JSON input
  - rejects non-object top-level (must be {field: value})
  - rejects unknown fields (safelist via entity_cls.model_fields)
  - rejects operator dicts ($in, $gte, etc.) for now — a separate bug
    will land a unified safelist
  - coerces 24-char hex strings to bson.ObjectId for relationship fields
  - returns a MongoDB-ready filter dict on success

The dynamic Pydantic class `entity_cls` is mocked with a SimpleNamespace
exposing `model_fields` — same shape Pydantic uses, enough to drive the
parser without instantiating real entities.
"""

from types import SimpleNamespace
from typing import Optional

import pytest
from bson import ObjectId
from fastapi import HTTPException

from kernel.api.registration import _parse_list_filter


# --- Fixtures ---


def _field_info(annotation):
    """Build a Pydantic-shaped field info stand-in. Real model_fields entries
    have many attributes; the parser only reads `annotation`."""
    return SimpleNamespace(annotation=annotation)


def _entity_cls(model_fields: dict):
    """Build an entity_cls stand-in exposing model_fields like a Pydantic model."""
    return SimpleNamespace(model_fields=model_fields)


# --- Invalid input ---


def test_rejects_invalid_json():
    cls = _entity_cls({})
    with pytest.raises(HTTPException) as exc:
        _parse_list_filter(cls, "Email", "not json{")
    assert exc.value.status_code == 400
    assert "Invalid JSON" in str(exc.value.detail)


def test_rejects_non_object_top_level():
    """JSON arrays / scalars / strings are rejected — we always want {field: value}."""
    cls = _entity_cls({})
    for bad in ['"just a string"', "[1,2,3]", "42", "null"]:
        with pytest.raises(HTTPException) as exc:
            _parse_list_filter(cls, "Email", bad)
        assert exc.value.status_code == 400
        assert "must be a JSON object" in str(exc.value.detail)


def test_rejects_unknown_fields():
    """A typo in field name returns 400 with the actual known fields, so the
    caller can fix the typo without trial and error."""
    cls = _entity_cls(
        {
            "title": _field_info(str),
            "status": _field_info(str),
        }
    )
    with pytest.raises(HTTPException) as exc:
        _parse_list_filter(cls, "Email", '{"copmany": "x"}')
    assert exc.value.status_code == 400
    detail = str(exc.value.detail)
    assert "Unknown field 'copmany'" in detail
    # The error message lists known fields so the caller can self-correct.
    assert "title" in detail or "status" in detail


def test_rejects_operator_dicts_for_now():
    """Operator filters ($in, $gte) need a safelist that lands with Bug #23.
    Until then, surface a clear rejection so callers don't think they were
    silently applied."""
    cls = _entity_cls({"status": _field_info(str)})
    with pytest.raises(HTTPException) as exc:
        _parse_list_filter(cls, "Email", '{"status": {"$in": ["a", "b"]}}')
    assert exc.value.status_code == 400
    assert "$in" in str(exc.value.detail) or "Operator filters" in str(exc.value.detail)


# --- Valid input — equality matches ---


def test_simple_equality_filter():
    cls = _entity_cls({"status": _field_info(str)})
    result = _parse_list_filter(cls, "Email", '{"status": "classified"}')
    assert result == {"status": "classified"}


def test_multiple_field_equality():
    cls = _entity_cls(
        {"status": _field_info(str), "title": _field_info(str)}
    )
    result = _parse_list_filter(cls, "Email", '{"status":"classified","title":"hi"}')
    assert result == {"status": "classified", "title": "hi"}


def test_int_value_passes_through():
    cls = _entity_cls({"count": _field_info(int)})
    result = _parse_list_filter(cls, "Sample", '{"count": 5}')
    assert result == {"count": 5}


def test_null_value_filters_for_null():
    """A literal null in the filter matches docs where the field is null."""
    cls = _entity_cls({"company": _field_info(Optional[str])})
    result = _parse_list_filter(cls, "Email", '{"company": null}')
    assert result == {"company": None}


def test_empty_filter_object_returns_empty_dict():
    """An empty filter dict is valid — equivalent to no filter."""
    cls = _entity_cls({})
    result = _parse_list_filter(cls, "Email", "{}")
    assert result == {}


# --- ObjectId coercion ---


def test_coerces_objectid_hex_string():
    """Filter on a relationship field with a 24-char hex string converts
    to bson.ObjectId so the equality match against the stored document works."""
    cls = _entity_cls({"company": _field_info(ObjectId)})
    hex_id = "69eb95f22b0a508618923977"
    result = _parse_list_filter(cls, "Email", f'{{"company": "{hex_id}"}}')
    assert isinstance(result["company"], ObjectId)
    assert str(result["company"]) == hex_id


def test_coerces_optional_objectid_hex_string():
    """Optional[ObjectId] fields also coerce — the Optional wrapper unwraps."""
    cls = _entity_cls({"company": _field_info(Optional[ObjectId])})
    hex_id = "69eb95f22b0a508618923977"
    result = _parse_list_filter(cls, "Email", f'{{"company": "{hex_id}"}}')
    assert isinstance(result["company"], ObjectId)


def test_rejects_invalid_objectid_hex():
    cls = _entity_cls({"company": _field_info(ObjectId)})
    with pytest.raises(HTTPException) as exc:
        _parse_list_filter(cls, "Email", '{"company": "not-a-hex-string"}')
    assert exc.value.status_code == 400
    assert "ObjectId" in str(exc.value.detail) or "hex" in str(exc.value.detail)


def test_objectid_field_with_null_value_passes_through():
    """Filtering for null on a relationship field is valid — finds docs where
    the relationship is unset. Don't try to coerce null to ObjectId."""
    cls = _entity_cls({"company": _field_info(Optional[ObjectId])})
    result = _parse_list_filter(cls, "Email", '{"company": null}')
    assert result == {"company": None}


def test_non_objectid_field_does_not_coerce():
    """A 24-char string on a regular str field is left alone (no false
    coercion to ObjectId just because the length matches)."""
    cls = _entity_cls({"external_ref": _field_info(str)})
    val = "69eb95f22b0a508618923977"  # 24 hex chars but not a relationship field
    result = _parse_list_filter(cls, "Email", f'{{"external_ref": "{val}"}}')
    assert result == {"external_ref": val}
    assert isinstance(result["external_ref"], str)
