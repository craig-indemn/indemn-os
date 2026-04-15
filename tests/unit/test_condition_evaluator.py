"""Unit tests for the condition evaluator — the single condition language."""

import pytest
from datetime import datetime, timedelta, timezone

from kernel.watch.evaluator import evaluate_condition


# --- Basic operators ---

def test_equals():
    assert evaluate_condition({"field": "status", "op": "equals", "value": "active"}, {"status": "active"})
    assert not evaluate_condition({"field": "status", "op": "equals", "value": "active"}, {"status": "pending"})


def test_not_equals():
    assert evaluate_condition({"field": "status", "op": "not_equals", "value": "active"}, {"status": "pending"})


def test_contains():
    assert evaluate_condition({"field": "subject", "op": "contains", "value": "POL"}, {"subject": "Re: POL-123"})
    assert not evaluate_condition({"field": "subject", "op": "contains", "value": "POL"}, {"subject": "Hello"})


def test_starts_with():
    assert evaluate_condition({"field": "email", "op": "starts_with", "value": "admin"}, {"email": "admin@example.com"})


def test_ends_with():
    assert evaluate_condition({"field": "email", "op": "ends_with", "value": "@usli.com"}, {"email": "quotes@usli.com"})
    assert not evaluate_condition({"field": "email", "op": "ends_with", "value": "@usli.com"}, {"email": "quotes@hiscox.com"})


def test_gt_gte_lt_lte():
    data = {"score": 85}
    assert evaluate_condition({"field": "score", "op": "gt", "value": 80}, data)
    assert evaluate_condition({"field": "score", "op": "gte", "value": 85}, data)
    assert evaluate_condition({"field": "score", "op": "lt", "value": 90}, data)
    assert evaluate_condition({"field": "score", "op": "lte", "value": 85}, data)
    assert not evaluate_condition({"field": "score", "op": "gt", "value": 85}, data)


def test_in_operator():
    assert evaluate_condition({"field": "tier", "op": "in", "value": ["enterprise", "strategic"]}, {"tier": "enterprise"})
    assert not evaluate_condition({"field": "tier", "op": "in", "value": ["enterprise", "strategic"]}, {"tier": "standard"})


def test_not_in():
    assert evaluate_condition({"field": "tier", "op": "not_in", "value": ["enterprise"]}, {"tier": "standard"})


def test_matches_regex():
    assert evaluate_condition({"field": "subject", "op": "matches", "value": r"POL-\d+"}, {"subject": "Re: POL-123 Update"})
    assert not evaluate_condition({"field": "subject", "op": "matches", "value": r"POL-\d+"}, {"subject": "Hello World"})


def test_exists():
    assert evaluate_condition({"field": "email", "op": "exists", "value": True}, {"email": "a@b.com"})
    assert not evaluate_condition({"field": "email", "op": "exists", "value": True}, {"name": "test"})


# --- Null/missing field handling ---

def test_missing_field_returns_none():
    assert not evaluate_condition({"field": "nonexistent", "op": "equals", "value": "x"}, {"status": "active"})


def test_none_field_gt():
    assert not evaluate_condition({"field": "score", "op": "gt", "value": 5}, {"score": None})


# --- Nested fields ---

def test_nested_field():
    data = {"config": {"threshold": 85}}
    assert evaluate_condition({"field": "config.threshold", "op": "gte", "value": 85}, data)


def test_deeply_nested():
    data = {"a": {"b": {"c": "found"}}}
    assert evaluate_condition({"field": "a.b.c", "op": "equals", "value": "found"}, data)


# --- Logical composition ---

def test_all():
    data = {"status": "active", "tier": "enterprise"}
    cond = {"all": [
        {"field": "status", "op": "equals", "value": "active"},
        {"field": "tier", "op": "equals", "value": "enterprise"},
    ]}
    assert evaluate_condition(cond, data)


def test_all_fails_if_any_false():
    data = {"status": "active", "tier": "standard"}
    cond = {"all": [
        {"field": "status", "op": "equals", "value": "active"},
        {"field": "tier", "op": "equals", "value": "enterprise"},
    ]}
    assert not evaluate_condition(cond, data)


def test_any():
    data = {"tier": "standard"}
    cond = {"any": [
        {"field": "tier", "op": "equals", "value": "enterprise"},
        {"field": "tier", "op": "equals", "value": "standard"},
    ]}
    assert evaluate_condition(cond, data)


def test_not():
    data = {"status": "active"}
    cond = {"not": {"field": "status", "op": "equals", "value": "suspended"}}
    assert evaluate_condition(cond, data)


def test_nested_composition():
    """all + any + not combined."""
    data = {"status": "active", "tier": "enterprise", "health": "at_risk"}
    cond = {"all": [
        {"field": "status", "op": "equals", "value": "active"},
        {"any": [
            {"field": "tier", "op": "equals", "value": "enterprise"},
            {"field": "tier", "op": "equals", "value": "strategic"},
        ]},
        {"not": {"field": "health", "op": "equals", "value": "healthy"}},
    ]}
    assert evaluate_condition(cond, data)


# --- Temporal operators ---

def test_older_than():
    old_time = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)).isoformat()
    assert evaluate_condition({"field": "last_activity", "op": "older_than", "value": "7d"}, {"last_activity": old_time})


def test_within():
    recent = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
    assert evaluate_condition({"field": "last_activity", "op": "within", "value": "24h"}, {"last_activity": recent})


def test_older_than_none_field():
    assert not evaluate_condition({"field": "last_activity", "op": "older_than", "value": "7d"}, {"last_activity": None})


# --- Unknown operator ---

def test_unknown_operator_raises():
    with pytest.raises(ValueError, match="Unknown operator"):
        evaluate_condition({"field": "x", "op": "banana", "value": "y"}, {"x": "y"})
