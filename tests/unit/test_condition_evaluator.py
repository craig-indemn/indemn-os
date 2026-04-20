"""Unit tests for the condition evaluator — the single condition language."""

from datetime import datetime, timedelta, timezone

import pytest

from kernel.watch.evaluator import evaluate_condition

# --- Basic operators ---


def test_equals():
    cond = {"field": "status", "op": "equals", "value": "active"}
    assert evaluate_condition(cond, {"status": "active"})
    assert not evaluate_condition(cond, {"status": "pending"})


def test_not_equals():
    cond = {"field": "status", "op": "not_equals", "value": "active"}
    assert evaluate_condition(cond, {"status": "pending"})


def test_contains():
    cond = {"field": "subject", "op": "contains", "value": "POL"}
    assert evaluate_condition(cond, {"subject": "Re: POL-123"})
    assert not evaluate_condition(cond, {"subject": "Hello"})


def test_starts_with():
    cond = {"field": "email", "op": "starts_with", "value": "admin"}
    assert evaluate_condition(cond, {"email": "admin@example.com"})


def test_ends_with():
    cond = {"field": "email", "op": "ends_with", "value": "@usli.com"}
    assert evaluate_condition(cond, {"email": "quotes@usli.com"})
    assert not evaluate_condition(cond, {"email": "quotes@hiscox.com"})


def test_gt_gte_lt_lte():
    data = {"score": 85}
    assert evaluate_condition({"field": "score", "op": "gt", "value": 80}, data)
    assert evaluate_condition({"field": "score", "op": "gte", "value": 85}, data)
    assert evaluate_condition({"field": "score", "op": "lt", "value": 90}, data)
    assert evaluate_condition({"field": "score", "op": "lte", "value": 85}, data)
    assert not evaluate_condition({"field": "score", "op": "gt", "value": 85}, data)


def test_in_operator():
    vals = ["enterprise", "strategic"]
    cond = {"field": "tier", "op": "in", "value": vals}
    assert evaluate_condition(cond, {"tier": "enterprise"})
    assert not evaluate_condition(cond, {"tier": "standard"})


def test_not_in():
    cond = {"field": "tier", "op": "not_in", "value": ["enterprise"]}
    assert evaluate_condition(cond, {"tier": "standard"})


def test_matches_regex():
    cond = {"field": "subject", "op": "matches", "value": r"POL-\d+"}
    assert evaluate_condition(cond, {"subject": "Re: POL-123 Update"})
    assert not evaluate_condition(cond, {"subject": "Hello World"})


def test_exists():
    cond = {"field": "email", "op": "exists", "value": True}
    assert evaluate_condition(cond, {"email": "a@b.com"})
    assert not evaluate_condition(cond, {"name": "test"})


# --- Null/missing field handling ---


def test_missing_field_returns_none():
    cond = {"field": "nonexistent", "op": "equals", "value": "x"}
    assert not evaluate_condition(cond, {"status": "active"})


def test_none_field_gt():
    cond = {"field": "score", "op": "gt", "value": 5}
    assert not evaluate_condition(cond, {"score": None})


# --- Nested fields ---


def test_nested_field():
    data = {"config": {"threshold": 85}}
    cond = {"field": "config.threshold", "op": "gte", "value": 85}
    assert evaluate_condition(cond, data)


def test_deeply_nested():
    data = {"a": {"b": {"c": "found"}}}
    cond = {"field": "a.b.c", "op": "equals", "value": "found"}
    assert evaluate_condition(cond, data)


# --- Logical composition ---


def test_all():
    data = {"status": "active", "tier": "enterprise"}
    cond = {
        "all": [
            {"field": "status", "op": "equals", "value": "active"},
            {"field": "tier", "op": "equals", "value": "enterprise"},
        ]
    }
    assert evaluate_condition(cond, data)


def test_all_fails_if_any_false():
    data = {"status": "active", "tier": "standard"}
    cond = {
        "all": [
            {"field": "status", "op": "equals", "value": "active"},
            {"field": "tier", "op": "equals", "value": "enterprise"},
        ]
    }
    assert not evaluate_condition(cond, data)


def test_any():
    data = {"tier": "standard"}
    cond = {
        "any": [
            {"field": "tier", "op": "equals", "value": "enterprise"},
            {"field": "tier", "op": "equals", "value": "standard"},
        ]
    }
    assert evaluate_condition(cond, data)


def test_not():
    data = {"status": "active"}
    cond = {"not": {"field": "status", "op": "equals", "value": "suspended"}}
    assert evaluate_condition(cond, data)


def test_nested_composition():
    """all + any + not combined."""
    data = {
        "status": "active",
        "tier": "enterprise",
        "health": "at_risk",
    }
    cond = {
        "all": [
            {"field": "status", "op": "equals", "value": "active"},
            {
                "any": [
                    {"field": "tier", "op": "equals", "value": "enterprise"},
                    {"field": "tier", "op": "equals", "value": "strategic"},
                ]
            },
            {
                "not": {
                    "field": "health",
                    "op": "equals",
                    "value": "healthy",
                }
            },
        ]
    }
    assert evaluate_condition(cond, data)


# --- Temporal operators ---


def test_older_than():
    old_time = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)).isoformat()
    cond = {
        "field": "last_activity",
        "op": "older_than",
        "value": "7d",
    }
    assert evaluate_condition(cond, {"last_activity": old_time})


def test_within():
    recent = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
    cond = {
        "field": "last_activity",
        "op": "within",
        "value": "24h",
    }
    assert evaluate_condition(cond, {"last_activity": recent})


def test_older_than_none_field():
    cond = {
        "field": "last_activity",
        "op": "older_than",
        "value": "7d",
    }
    assert not evaluate_condition(cond, {"last_activity": None})


# --- Unknown operator ---


def test_unknown_operator_raises():
    with pytest.raises(ValueError, match="Unknown operator"):
        evaluate_condition({"field": "x", "op": "banana", "value": "y"}, {"x": "y"})
