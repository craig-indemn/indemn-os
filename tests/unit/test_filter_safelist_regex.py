"""Tests for `$regex` / `$options` support in the filter safelist.

CLI gap #1 — Session 24 logged 'Entity search by content' as a pain point:
every "find emails where subject contains X" needed a mongosh fallback
because `$regex` wasn't in the safelist.

These tests pin:
- `$regex` with a string operand passes through to MongoDB unchanged
- `$options` companion operator passes alongside $regex in same dict
- Non-string operand for $regex or $options → 400
- Field-name safelisting still applies (unknown field still 400)
- $regex on an ObjectId field — the operator allowlist accepts it; MongoDB
  itself will return no matches (regex on non-string types). We don't
  block this at the safelist layer.
"""

from types import SimpleNamespace
from typing import Optional

import pytest
from bson import ObjectId
from fastapi import HTTPException

from kernel.api._filter_safelist import parse_filter


def _field(annotation, alias=None):
    return SimpleNamespace(annotation=annotation, alias=alias)


def _cls(model_fields: dict):
    return SimpleNamespace(model_fields=model_fields)


# --- Happy path ---


def test_regex_string_operand_passes_through():
    """`{"subject": {"$regex": "ncaia"}}` → MongoDB-ready dict, unchanged."""
    cls = _cls({"subject": _field(str)})
    result = parse_filter(cls, "Email", {"subject": {"$regex": "ncaia"}})
    assert result == {"subject": {"$regex": "ncaia"}}


def test_regex_with_options_passes_through():
    """`{"subject": {"$regex": "ncaia", "$options": "i"}}` — case-insensitive."""
    cls = _cls({"subject": _field(str)})
    result = parse_filter(
        cls, "Email", {"subject": {"$regex": "ncaia", "$options": "i"}}
    )
    assert result == {"subject": {"$regex": "ncaia", "$options": "i"}}


def test_regex_special_chars_preserved():
    """Anchors, character classes, alternation — preserved (Mongo handles)."""
    cls = _cls({"sender": _field(str)})
    pattern = r"^(no-reply|notification)@.*\.(com|org)$"
    result = parse_filter(cls, "Email", {"sender": {"$regex": pattern}})
    assert result == {"sender": {"$regex": pattern}}


def test_regex_combined_with_other_operators():
    """`{$regex, $in}` — multiple operators on same field, all pass through."""
    cls = _cls({"subject": _field(str), "status": _field(str)})
    result = parse_filter(
        cls,
        "Email",
        {
            "subject": {"$regex": "ncaia"},
            "status": {"$in": ["received", "classified"]},
        },
    )
    assert result == {
        "subject": {"$regex": "ncaia"},
        "status": {"$in": ["received", "classified"]},
    }


# --- Validation ---


def test_regex_non_string_operand_rejected():
    """`$regex: 123` is malformed — surface as 400 with message naming the field."""
    cls = _cls({"subject": _field(str)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Email", {"subject": {"$regex": 123}})
    assert exc.value.status_code == 400
    assert "subject" in exc.value.detail


def test_options_non_string_operand_rejected():
    """`$options: true` is malformed."""
    cls = _cls({"subject": _field(str)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(
            cls, "Email", {"subject": {"$regex": "ncaia", "$options": True}}
        )
    assert exc.value.status_code == 400


def test_regex_on_unknown_field_rejected():
    """Field-name safelist still applies. Typo'd field name → 400."""
    cls = _cls({"subject": _field(str)})
    with pytest.raises(HTTPException) as exc:
        parse_filter(cls, "Email", {"sbject": {"$regex": "ncaia"}})
    assert exc.value.status_code == 400
    assert "sbject" in exc.value.detail


def test_regex_no_type_coercion_on_objectid_field():
    """When $regex is applied to an ObjectId field, the pattern stays a
    string — DO NOT coerce it via ObjectId() (the value isn't a 24-char
    hex; coercion would 400). Bug it would cause: a regex pattern like
    "ncaia" passed against `company` field (an ObjectId field) would
    fail ObjectId() construction. Verify it passes through unchanged
    instead; Mongo will simply return no matches (regex on ObjectId)."""
    cls = _cls({"company": _field(Optional[ObjectId])})
    result = parse_filter(cls, "Email", {"company": {"$regex": "ncaia"}})
    assert result == {"company": {"$regex": "ncaia"}}


def test_regex_combined_with_in_operator():
    """{$in: [hex_strings]} still coerces to ObjectId. $regex on same
    document but different field doesn't break that."""
    cls = _cls(
        {
            "subject": _field(str),
            "company": _field(Optional[ObjectId]),
        }
    )
    cid = "69e23d586a448759a34d3823"
    result = parse_filter(
        cls,
        "Email",
        {
            "subject": {"$regex": "ncaia"},
            "company": {"$in": [cid]},
        },
    )
    assert result["subject"] == {"$regex": "ncaia"}
    # $in operands coerce to ObjectId for ObjectId-typed fields
    assert result["company"] == {"$in": [ObjectId(cid)]}
