"""Tests for `_coerce_datetime_fields` — the API-boundary coercion that
fixes Bug #19.

Bug as filed: "Change records occasionally have non-Date `timestamp`
fields." Original hypothesis: bulk operations write strings to the
top-level `timestamp` column. Pre-flight code-read showed every kernel
writer goes through `ChangeRecord` with `default_factory=datetime.now(UTC)`
and a live mongo scan confirmed all 56,485 existing records have
top-level `timestamp` as BSON Date.

The actual symptom was on `changes[].old_value` / `new_value` sub-fields:
when an API PUT updates a `datetime`-typed field with a string-formatted
ISO date, the update path's `setattr(entity, key, value)` does NOT trigger
Pydantic validation. The string passed straight through and was stored
in MongoDB as a STRING — corrupting:

  * the entity field itself (sort/filter on `date` silently misbehaves
    because string sort != datetime sort)
  * the changes-collection record (old_value loaded as Date, new_value
    captured as string — same logical value, two types, two different
    hashes via `hash_chain._normalize_value` strftime-vs-string-fallthrough)

Live evidence on dev OS at fix time: 2 corrupted entities (1 Touchpoint,
1 Email) — Touchpoint `69ea7f70a25d34b927d74f3f` and Email
`69efc96a0fed948fce0a83e9` both had `date` stored as the string
`"2026-04-21T19:08:15"` / `"2026-02-11T22:00:00"` and corresponding
change records where new_value mismatched old_value's type.

Fix: peer helper to `_coerce_objectid_fields` that walks Pydantic field
annotations, finds `datetime`-typed (and `Optional[datetime]`,
`list[datetime]`) fields, and parses string values via `fromisoformat`.
Wired into BOTH create and update paths so the canonical typed value is
what reaches `setattr` / Pydantic `__init__`.

These tests pin:
  * idempotent on real datetime instances
  * ISO-8601 string → datetime (with and without trailing 'Z')
  * fields not in `data` are untouched
  * non-datetime fields (str, int, ObjectId) untouched
  * Optional[datetime] with None left as None
  * list[datetime] elements coerced
  * unparseable strings raise HTTPException 400 with the field name and
    value (NOT a silent pass-through that corrupts the audit chain)
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest
from bson import ObjectId
from fastapi import HTTPException

from kernel.api.registration import _coerce_datetime_fields

# --- Test fixtures: minimal entity_cls stand-in with model_fields ---


def _entity_cls(**field_annotations):
    """Build an entity_cls stand-in. field_annotations maps field_name to a
    Python type annotation (e.g. datetime, Optional[datetime], list[datetime])."""
    fields = {
        name: SimpleNamespace(annotation=annot)
        for name, annot in field_annotations.items()
    }
    return SimpleNamespace(model_fields=fields)


# --- Idempotence ---


def test_real_datetime_instance_passes_through():
    cls = _entity_cls(date=datetime)
    real_dt = datetime(2026, 2, 11, 22, 0, 0)
    out = _coerce_datetime_fields(cls, {"date": real_dt})
    assert out["date"] is real_dt
    assert isinstance(out["date"], datetime)


def test_field_not_in_data_is_untouched():
    cls = _entity_cls(date=datetime, other=str)
    out = _coerce_datetime_fields(cls, {"other": "anything"})
    assert "date" not in out
    assert out["other"] == "anything"


# --- The actual fix: ISO-string → datetime ---


def test_iso_string_no_offset_becomes_datetime():
    """The exact shape that produced Bug #19's offending records:
    `"2026-02-11T22:00:00"` — no offset, no Z. Must coerce to datetime."""
    cls = _entity_cls(date=datetime)
    out = _coerce_datetime_fields(cls, {"date": "2026-02-11T22:00:00"})
    assert isinstance(out["date"], datetime)
    assert out["date"].year == 2026
    assert out["date"].month == 2
    assert out["date"].day == 11
    assert out["date"].hour == 22


def test_iso_string_with_z_suffix_becomes_aware_datetime():
    """API payloads commonly include 'Z' suffix. Python 3.11+ handles this in
    fromisoformat; we explicitly normalize to '+00:00' for older runtimes
    and clarity."""
    cls = _entity_cls(timestamp_field=datetime)
    out = _coerce_datetime_fields(cls, {"timestamp_field": "2026-02-11T22:00:00Z"})
    assert isinstance(out["timestamp_field"], datetime)
    assert out["timestamp_field"].tzinfo is not None
    assert out["timestamp_field"].utcoffset() == timezone.utc.utcoffset(None)


def test_iso_string_with_explicit_offset_becomes_aware():
    cls = _entity_cls(date=datetime)
    out = _coerce_datetime_fields(cls, {"date": "2026-02-11T22:00:00+05:00"})
    assert isinstance(out["date"], datetime)
    assert out["date"].tzinfo is not None


# --- Optional / list / non-datetime ---


def test_optional_datetime_with_none_stays_none():
    cls = _entity_cls(date=Optional[datetime])
    out = _coerce_datetime_fields(cls, {"date": None})
    assert out["date"] is None


def test_optional_datetime_with_iso_string_coerced():
    cls = _entity_cls(date=Optional[datetime])
    out = _coerce_datetime_fields(cls, {"date": "2026-02-11T22:00:00"})
    assert isinstance(out["date"], datetime)


def test_list_datetime_elements_coerced():
    cls = _entity_cls(timeline=list[datetime])
    out = _coerce_datetime_fields(
        cls, {"timeline": ["2026-02-11T22:00:00", "2026-02-12T08:30:00Z"]}
    )
    assert all(isinstance(v, datetime) for v in out["timeline"])
    assert out["timeline"][0].day == 11
    assert out["timeline"][1].day == 12


def test_non_datetime_fields_untouched():
    """str / int / ObjectId / dict fields must not be touched even if their
    values happen to LOOK like ISO strings."""
    cls = _entity_cls(
        name=str,
        count=int,
        company_id=ObjectId,
        metadata=dict,
    )
    out = _coerce_datetime_fields(
        cls,
        {
            "name": "2026-04-28T12:00:00",  # ISO-shaped string in a str field
            "count": 42,
            "company_id": ObjectId(),
            "metadata": {"key": "2026-04-28T12:00:00"},
        },
    )
    assert out["name"] == "2026-04-28T12:00:00"  # left as string
    assert out["count"] == 42
    assert isinstance(out["company_id"], ObjectId)
    assert out["metadata"] == {"key": "2026-04-28T12:00:00"}  # nested untouched


# --- Bad input surfaces at the boundary ---


def test_unparseable_string_raises_400_with_field_name():
    """Boundary check: bad input gets a 400 with the field name and the
    offending value, so the caller learns immediately. Pre-fix the bad
    string would have passed through, corrupted the entity, and caused
    audit-chain noise — silent, expensive, distant from the bug source."""
    cls = _entity_cls(date=datetime)
    with pytest.raises(HTTPException) as exc:
        _coerce_datetime_fields(cls, {"date": "not-a-date"})
    assert exc.value.status_code == 400
    assert "date" in exc.value.detail
    assert "not-a-date" in exc.value.detail


def test_non_string_non_datetime_value_passes_through():
    """If the caller sends an int or dict for a datetime field, don't try to
    parse it — let Pydantic surface a real type error downstream. The helper
    only TRANSFORMS strings; everything else is passthrough so we don't
    swallow legitimate validation errors."""
    cls = _entity_cls(date=datetime)
    out = _coerce_datetime_fields(cls, {"date": 12345})  # int
    assert out["date"] == 12345


# --- The original bug shape, end-to-end ---


def test_bug_19_offending_shapes_are_now_coerced():
    """Both real-world shapes from the dev DB at fix time."""
    cls = _entity_cls(date=Optional[datetime])
    # The exact strings found in dev: Touchpoint date + Email date.
    out_tp = _coerce_datetime_fields(cls, {"date": "2026-04-21T19:08:15"})
    out_em = _coerce_datetime_fields(cls, {"date": "2026-02-11T22:00:00"})
    assert isinstance(out_tp["date"], datetime)
    assert isinstance(out_em["date"], datetime)
