"""Unit tests for stale_check capability."""

import pytest

from kernel.capability.stale_check import stale_check


class _MockEntity:
    """Minimal entity mock for capability testing."""

    def __init__(self, data: dict):
        self._data = data
        for k, v in data.items():
            setattr(self, k, v)

    def model_dump(self, by_alias=False):
        return dict(self._data)


@pytest.mark.asyncio
async def test_stale_check_match():
    """Conditions met → returns matched with sets."""
    entity = _MockEntity({"is_overdue": False, "status": "open", "due_date": "2020-01-01"})
    config = {
        "when": {"field": "status", "op": "equals", "value": "open"},
        "sets_field": "is_overdue",
        "sets_value": True,
    }
    result = await stale_check(entity, config, org_id="test")
    assert result["needs_reasoning"] is False
    assert result["matched"] is True
    assert result["result"] == {"is_overdue": True}


@pytest.mark.asyncio
async def test_stale_check_no_match():
    """Conditions not met → returns empty result."""
    entity = _MockEntity({"status": "completed"})
    config = {
        "when": {"field": "status", "op": "equals", "value": "open"},
        "sets_field": "is_overdue",
        "sets_value": True,
    }
    result = await stale_check(entity, config, org_id="test")
    assert result["needs_reasoning"] is False
    assert result["matched"] is False
    assert result["result"] == {}


@pytest.mark.asyncio
async def test_stale_check_missing_config():
    """Missing config → returns not matched."""
    entity = _MockEntity({"status": "open"})
    result = await stale_check(entity, {}, org_id="test")
    assert result["matched"] is False
    assert result["reason"] == "missing_config"


@pytest.mark.asyncio
async def test_stale_check_all_conditions():
    """Multiple conditions with 'all' operator."""
    entity = _MockEntity({"status": "open", "followup_count": 3})
    config = {
        "when": {
            "all": [
                {"field": "status", "op": "equals", "value": "open"},
                {"field": "followup_count", "op": "gte", "value": 2},
            ]
        },
        "sets_field": "is_stale",
        "sets_value": True,
    }
    result = await stale_check(entity, config, org_id="test")
    assert result["matched"] is True
    assert result["result"] == {"is_stale": True}


@pytest.mark.asyncio
async def test_stale_check_all_conditions_partial_fail():
    """One condition in 'all' fails → no match."""
    entity = _MockEntity({"status": "closed", "followup_count": 3})
    config = {
        "when": {
            "all": [
                {"field": "status", "op": "equals", "value": "open"},
                {"field": "followup_count", "op": "gte", "value": 2},
            ]
        },
        "sets_field": "is_stale",
        "sets_value": True,
    }
    result = await stale_check(entity, config, org_id="test")
    assert result["matched"] is False
