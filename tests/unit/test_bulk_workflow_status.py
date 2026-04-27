"""Tests for BulkExecuteWorkflow status summarization + filter coercion (Bug #24).

The actual workflow runs in Temporal — exercising it directly requires a
WorkflowEnvironment. These tests pin the small pure-function pieces we
extracted from the workflow:

  - `summarize_bulk_status(matched, errors)` -> the three terminal status
    strings (`completed_no_match`, `completed_with_errors`, `completed`).
    Bug #24 was: a bulk-delete with zero matches reported `completed`
    indistinguishably from a successful one, so operators couldn't tell
    the operation did nothing.

  - `BulkResult` dataclass shape — sanity check that the field names line
    up with what the API status endpoint expects.

  - `_coerce_bulk_filter()` from the activity — it calls parse_filter and
    converts HTTPException -> PermanentProcessingError so a bad filter
    fails the workflow non-retryably instead of looping forever. Pinned
    here because the API boundary is supposed to validate first; this is
    the safety net.
"""

from datetime import datetime
from types import SimpleNamespace
from typing import Optional

import pytest
from bson import ObjectId
from fastapi import HTTPException

from kernel.temporal.activities import (
    PermanentProcessingError,
    _coerce_bulk_filter,
)
from kernel.temporal.workflows import BulkResult, summarize_bulk_status


# --- Status summarization (Bug #24) ---


def test_summarize_no_match():
    """Zero matches must produce completed_no_match — distinguishable from a
    clean completion. This is the symptom that drove Bug #24."""
    assert summarize_bulk_status(matched=0, errors=[]) == "completed_no_match"


def test_summarize_no_match_takes_precedence_over_empty_errors():
    """Even if errors list is empty, matched=0 wins — there's nothing to
    report success on."""
    assert summarize_bulk_status(matched=0, errors=[]) == "completed_no_match"


def test_summarize_clean_completion():
    assert summarize_bulk_status(matched=10, errors=[]) == "completed"


def test_summarize_with_errors():
    errs = [{"entity_id": "x", "error_type": "ValueError", "message": "..."}]
    assert summarize_bulk_status(matched=10, errors=errs) == "completed_with_errors"


def test_summarize_matched_priority_over_errors():
    """If matched=0 we pick completed_no_match even if errors exist (which
    shouldn't happen in practice — no entities were processed — but we want
    the priority order pinned regardless)."""
    errs = [{"entity_id": "x"}]
    assert summarize_bulk_status(matched=0, errors=errs) == "completed_no_match"


# --- BulkResult dataclass shape ---


def test_bulk_result_dataclass_field_names():
    """Pin the field names since the API status endpoint surfaces these
    verbatim — renaming would silently break clients."""
    r = BulkResult(
        status="completed",
        matched=10,
        succeeded=8,
        errored=2,
        errors=[{"entity_id": "x"}],
    )
    assert r.status == "completed"
    assert r.matched == 10
    assert r.succeeded == 8
    assert r.errored == 2
    assert r.errors == [{"entity_id": "x"}]


# --- _coerce_bulk_filter (Bug #23 safety net) ---


def _entity_cls(model_fields: dict):
    return SimpleNamespace(model_fields=model_fields)


def _field(annotation):
    return SimpleNamespace(annotation=annotation)


def test_coerce_bulk_filter_returns_typed_dict():
    """Happy path: a JSON-safe dict from the workflow input round-trips
    through parse_filter and produces a MongoDB-typed filter."""
    cls = _entity_cls({"_id": _field(ObjectId)})
    hex_id = "69eb95f22b0a508618923977"
    typed = _coerce_bulk_filter(cls, "Company", {"_id": {"$in": [hex_id]}})
    assert "$in" in typed["_id"]
    assert isinstance(typed["_id"]["$in"][0], ObjectId)


def test_coerce_bulk_filter_coerces_datetime_in_gte():
    cls = _entity_cls({"created_at": _field(datetime)})
    typed = _coerce_bulk_filter(
        cls, "Meeting", {"created_at": {"$gte": "2026-04-23T00:00:00Z"}}
    )
    assert isinstance(typed["created_at"]["$gte"], datetime)


def test_coerce_bulk_filter_converts_400_to_permanent_processing_error():
    """If the API boundary somehow lets a bad filter through to the workflow,
    the activity must fail non-retryably. Otherwise Temporal would retry the
    bad filter for hours."""
    cls = _entity_cls({"_id": _field(ObjectId)})
    with pytest.raises(PermanentProcessingError) as exc:
        _coerce_bulk_filter(cls, "Company", {"_id": {"$in": ["not-hex"]}})
    assert "Invalid bulk filter" in str(exc.value)
    assert "Company" in str(exc.value)


def test_coerce_bulk_filter_unknown_field_also_fails_permanently():
    cls = _entity_cls({"name": _field(str)})
    with pytest.raises(PermanentProcessingError) as exc:
        _coerce_bulk_filter(cls, "Company", {"unknown_field": "x"})
    assert "Invalid bulk filter" in str(exc.value)


def test_coerce_bulk_filter_passes_through_empty_filter():
    """Empty dict is valid — corresponds to "match all" semantics that
    bulk-delete has its own dry-run gate against (Bug #4 territory)."""
    cls = _entity_cls({})
    assert _coerce_bulk_filter(cls, "Company", {}) == {}
