"""Tests for get_audit_completeness_boundary — Session-35 Decision D2.

The boundary mechanism is what Stage C eval reconstruction (sub-piece 12 D-J)
uses to gate which entities get audit-grounded reconstruction (post-boundary)
vs which are skipped entirely (pre-boundary per D18).

Per D2:
- Boundary = min(timestamp) across create-type ChangeRecords with non-empty changes
- Self-discovering at kernel startup
- Cached in process

Tests pin:
- Pre-Stage-A state (no qualifying records) → boundary is None
- Post-Stage-A state (qualifying records exist) → boundary is the minimum timestamp
- Cache: first call queries, subsequent calls return cached value (no re-query)
- reset_cache() forces re-derivation (test affordance)
- Helper queries the right aggregation pipeline (source pin)
- API route exposes boundary via /api/_platform/audit/completeness-boundary
"""

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_boundary_cache():
    """Reset the boundary cache before each test."""
    from kernel.changes import boundary
    boundary.reset_cache()
    yield
    boundary.reset_cache()


@pytest.mark.asyncio
async def test_boundary_returns_none_when_no_qualifying_records():
    """Pre-Stage-A-deploy state: no create records with non-empty changes → None."""
    from kernel.changes.boundary import get_audit_completeness_boundary

    with patch("kernel.changes.collection.ChangeRecord") as MockCR:
        agg_cursor = MagicMock()
        agg_cursor.to_list = AsyncMock(return_value=[])  # empty result
        coll = MagicMock(aggregate=MagicMock(return_value=agg_cursor))
        MockCR.get_motor_collection = MagicMock(return_value=coll)

        result = await get_audit_completeness_boundary()

    assert result is None


@pytest.mark.asyncio
async def test_boundary_returns_min_timestamp_when_records_exist():
    """Post-Stage-A-deploy: returns the minimum timestamp."""
    from kernel.changes.boundary import get_audit_completeness_boundary

    earliest = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)

    with patch("kernel.changes.collection.ChangeRecord") as MockCR:
        agg_cursor = MagicMock()
        agg_cursor.to_list = AsyncMock(return_value=[{"_id": None, "min_ts": earliest}])
        coll = MagicMock(aggregate=MagicMock(return_value=agg_cursor))
        MockCR.get_motor_collection = MagicMock(return_value=coll)

        result = await get_audit_completeness_boundary()

    assert result == earliest


@pytest.mark.asyncio
async def test_boundary_uses_correct_aggregation_pipeline():
    """Pipeline matches create + non-empty changes; groups for min(timestamp)."""
    from kernel.changes.boundary import get_audit_completeness_boundary

    with patch("kernel.changes.collection.ChangeRecord") as MockCR:
        agg_cursor = MagicMock()
        agg_cursor.to_list = AsyncMock(return_value=[])
        aggregate_mock = MagicMock(return_value=agg_cursor)
        coll = MagicMock(aggregate=aggregate_mock)
        MockCR.get_motor_collection = MagicMock(return_value=coll)

        await get_audit_completeness_boundary()

    pipeline_arg = aggregate_mock.call_args[0][0]
    # First stage: $match on change_type=create + changes != []
    assert pipeline_arg[0] == {
        "$match": {"change_type": "create", "changes": {"$ne": []}}
    }
    # Second stage: $group for min(timestamp)
    assert pipeline_arg[1] == {
        "$group": {"_id": None, "min_ts": {"$min": "$timestamp"}}
    }


@pytest.mark.asyncio
async def test_boundary_caches_after_first_call():
    """First call queries MongoDB; subsequent calls return cached value."""
    from kernel.changes.boundary import get_audit_completeness_boundary

    earliest = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)

    with patch("kernel.changes.collection.ChangeRecord") as MockCR:
        agg_cursor = MagicMock()
        agg_cursor.to_list = AsyncMock(return_value=[{"_id": None, "min_ts": earliest}])
        aggregate_mock = MagicMock(return_value=agg_cursor)
        coll = MagicMock(aggregate=aggregate_mock)
        MockCR.get_motor_collection = MagicMock(return_value=coll)

        r1 = await get_audit_completeness_boundary()
        r2 = await get_audit_completeness_boundary()
        r3 = await get_audit_completeness_boundary()

    assert r1 == r2 == r3 == earliest
    # Only ONE aggregate call across 3 invocations
    aggregate_mock.assert_called_once()


@pytest.mark.asyncio
async def test_boundary_caches_none_too():
    """The 'no qualifying records' result (None) is also cached — no repeated queries."""
    from kernel.changes.boundary import get_audit_completeness_boundary

    with patch("kernel.changes.collection.ChangeRecord") as MockCR:
        agg_cursor = MagicMock()
        agg_cursor.to_list = AsyncMock(return_value=[])
        aggregate_mock = MagicMock(return_value=agg_cursor)
        coll = MagicMock(aggregate=aggregate_mock)
        MockCR.get_motor_collection = MagicMock(return_value=coll)

        r1 = await get_audit_completeness_boundary()
        r2 = await get_audit_completeness_boundary()

    assert r1 is None
    assert r2 is None
    aggregate_mock.assert_called_once()


@pytest.mark.asyncio
async def test_boundary_reset_cache_forces_requery():
    """reset_cache() clears the cached value; next call re-derives."""
    from kernel.changes import boundary
    from kernel.changes.boundary import get_audit_completeness_boundary

    ts1 = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 5, 28, 11, 0, 0, tzinfo=timezone.utc)  # earlier — simulates a backfill

    with patch("kernel.changes.collection.ChangeRecord") as MockCR:
        agg_cursor1 = MagicMock()
        agg_cursor1.to_list = AsyncMock(return_value=[{"_id": None, "min_ts": ts1}])
        agg_cursor2 = MagicMock()
        agg_cursor2.to_list = AsyncMock(return_value=[{"_id": None, "min_ts": ts2}])
        aggregate_mock = MagicMock(side_effect=[agg_cursor1, agg_cursor2])
        coll = MagicMock(aggregate=aggregate_mock)
        MockCR.get_motor_collection = MagicMock(return_value=coll)

        r1 = await get_audit_completeness_boundary()
        boundary.reset_cache()
        r2 = await get_audit_completeness_boundary()

    assert r1 == ts1
    assert r2 == ts2
    assert aggregate_mock.call_count == 2


def test_boundary_helper_signature():
    """Shape pin: get_audit_completeness_boundary() -> Optional[datetime]."""
    from kernel.changes.boundary import get_audit_completeness_boundary, reset_cache

    assert inspect.iscoroutinefunction(get_audit_completeness_boundary)
    sig = inspect.signature(get_audit_completeness_boundary)
    assert len(sig.parameters) == 0  # no args
    # reset_cache is a sync test affordance
    assert not inspect.iscoroutinefunction(reset_cache)


def test_audit_completeness_boundary_route_registered():
    """Source pin: /api/_platform/audit/completeness-boundary route exists in admin_routes."""
    from kernel.api import admin_routes

    src = inspect.getsource(admin_routes)
    assert "/api/_platform/audit/completeness-boundary" in src
    assert "audit_completeness_boundary" in src
    assert "get_audit_completeness_boundary" in src


def test_audit_completeness_boundary_cli_command_registered():
    """Source pin: indemn audit completeness-boundary CLI command exists."""
    from indemn_os import audit_commands

    src = inspect.getsource(audit_commands)
    assert "completeness-boundary" in src
    assert "/api/_platform/audit/completeness-boundary" in src
