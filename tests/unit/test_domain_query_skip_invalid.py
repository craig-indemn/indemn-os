"""Tests for _DomainQuery.to_list(skip_invalid=...) — Bug #37.

A pre-Bug-#9-fix associate run wrote a stringified dict to `Email.company`
instead of an ObjectId. On read-back, `Email(**doc)` raises Pydantic
ValidationError ("Input should be an instance of ObjectId"), which used to
propagate through `_register_list_route` and return HTTP 400 for every
caller — one bad row poisoned the entire endpoint with no recovery path.

Fix: `_DomainQuery.to_list(skip_invalid=False)` — default preserves the
historical strict behavior. The user-facing list endpoint opts in to
`skip_invalid=True` so malformed rows are skipped (with a warning log
naming the entity type and bad doc `_id` for operator cleanup).

Migrations / audit code that need to know about every doc keep the strict
default; they'd silently drop bad rows otherwise, which is a worse footgun
than a loud failure.
"""

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from pydantic import BaseModel

from kernel.entity.base import _DomainQuery


# --- Minimal entity classes for testing ---


class _StrictEntity(BaseModel):
    """Pydantic model that raises on non-int `n` — stand-in for a domain entity
    with a strict relationship-field type (e.g. Email with `company: ObjectId`)."""

    id: ObjectId | None = None
    n: int

    model_config = {"arbitrary_types_allowed": True, "populate_by_name": True}

    _collection_name: ClassVar[str] = "fakes"
    _db_ref: ClassVar = None  # patched per-test


def _make_query(docs: list[dict]) -> _DomainQuery:
    """Build a _DomainQuery whose underlying cursor returns the given docs."""
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=docs)

    coll = MagicMock()
    coll.find = MagicMock(return_value=cursor)

    fake_db = {"fakes": coll}
    _StrictEntity._db_ref = fake_db
    return _DomainQuery(_StrictEntity, {})


# --- Strict default (skip_invalid=False) ---


@pytest.mark.asyncio
async def test_default_strict_raises_on_first_bad_doc():
    """Historical behavior preserved — without opt-in, one bad doc fails the
    entire list call. Migrations / audit code rely on this."""
    docs = [
        {"_id": ObjectId(), "n": 1},
        {"_id": ObjectId(), "n": "not-an-int"},  # invalid
        {"_id": ObjectId(), "n": 3},
    ]
    query = _make_query(docs)
    with pytest.raises(Exception):  # Pydantic ValidationError
        await query.to_list()


@pytest.mark.asyncio
async def test_default_strict_succeeds_when_all_valid():
    docs = [
        {"_id": ObjectId(), "n": 1},
        {"_id": ObjectId(), "n": 2},
        {"_id": ObjectId(), "n": 3},
    ]
    query = _make_query(docs)
    result = await query.to_list()
    assert len(result) == 3


# --- Tolerant opt-in (skip_invalid=True) ---


@pytest.mark.asyncio
async def test_skip_invalid_returns_only_valid_entities(caplog):
    """The list endpoint opts in. Bad rows are skipped; good rows returned."""
    bad_id = ObjectId()
    docs = [
        {"_id": ObjectId(), "n": 1},
        {"_id": bad_id, "n": "not-an-int"},  # invalid
        {"_id": ObjectId(), "n": 3},
    ]
    query = _make_query(docs)

    with caplog.at_level("WARNING"):
        result = await query.to_list(skip_invalid=True)

    assert len(result) == 2  # bad row dropped
    assert all(isinstance(e, _StrictEntity) for e in result)
    # Warning log names the entity type + bad doc _id for operator cleanup
    assert any(
        "_StrictEntity" in rec.message and str(bad_id) in rec.message
        for rec in caplog.records
    ), f"Expected warning naming _StrictEntity and {bad_id}, got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_skip_invalid_with_all_valid_returns_all():
    """Tolerance doesn't change behavior on clean data."""
    docs = [{"_id": ObjectId(), "n": i} for i in range(5)]
    query = _make_query(docs)
    result = await query.to_list(skip_invalid=True)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_skip_invalid_with_all_bad_returns_empty(caplog):
    """If every row is malformed, return empty list — endpoint stays alive."""
    docs = [{"_id": ObjectId(), "n": "x"} for _ in range(3)]
    query = _make_query(docs)

    with caplog.at_level("WARNING"):
        result = await query.to_list(skip_invalid=True)

    assert result == []
    # One warning per skipped row
    skip_warnings = [r for r in caplog.records if "Skipping malformed" in r.message]
    assert len(skip_warnings) == 3


@pytest.mark.asyncio
async def test_loaded_state_only_set_on_constructed_entities(caplog):
    """The skip path must not call model_dump on the failed construction
    (it doesn't exist yet) — this is just a sanity check that the try/except
    is structured correctly: the entity isn't appended, _loaded_state isn't
    touched, no NameError or AttributeError leaks out."""
    docs = [
        {"_id": ObjectId(), "n": "bad"},
        {"_id": ObjectId(), "n": 42},
    ]
    query = _make_query(docs)
    with caplog.at_level("WARNING"):
        result = await query.to_list(skip_invalid=True)
    assert len(result) == 1
    assert hasattr(result[0], "_loaded_state")
