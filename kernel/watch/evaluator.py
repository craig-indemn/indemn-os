"""Condition evaluator — the single condition language.

Shared by watches and rules. One evaluator, one syntax, one debugging surface.
JSON format with field comparisons and logical composition (all, any, not).

Condition format:
    {"field": "status", "op": "equals", "value": "active"}
    {"all": [condition, condition, ...]}
    {"any": [condition, condition, ...]}
    {"not": condition}
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any


def evaluate_condition(condition: dict, entity_data: dict) -> bool:
    """Evaluate a JSON condition against entity field values."""
    if "all" in condition:
        return all(evaluate_condition(c, entity_data) for c in condition["all"])
    if "any" in condition:
        return any(evaluate_condition(c, entity_data) for c in condition["any"])
    if "not" in condition:
        return not evaluate_condition(condition["not"], entity_data)

    field = condition["field"]
    op = condition["op"]
    expected = condition.get("value")
    actual = _get_nested_field(entity_data, field)

    operator_fn = _OPERATORS.get(op)
    if operator_fn is None:
        raise ValueError(f"Unknown operator: {op}")
    return operator_fn(actual, expected)


def _get_nested_field(data: dict, field_path: str) -> Any:
    """Get a value from nested dict using dot notation."""
    parts = field_path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _older_than(actual, duration_str: str) -> bool:
    """Check if a datetime field is older than a duration string (7d, 30m, 24h)."""
    if actual is None:
        return False
    if isinstance(actual, str):
        try:
            actual = datetime.fromisoformat(actual)
        except ValueError:
            return False
    if not isinstance(actual, datetime):
        return False

    amount = int(duration_str[:-1])
    unit = duration_str[-1]
    delta = {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }.get(unit)

    if delta is None:
        return False
    utc_now = datetime.now(timezone.utc)
    now = utc_now if actual.tzinfo else utc_now.replace(tzinfo=None)
    return actual < (now - delta)


def describe_condition(condition: dict, entity_data: dict) -> str:
    """Produce a human-readable explanation of a condition evaluation."""
    if "all" in condition:
        parts = [describe_condition(c, entity_data) for c in condition["all"]]
        passed = all(evaluate_condition(c, entity_data) for c in condition["all"])
        return f"all([{', '.join(parts)}]) → {'pass' if passed else 'fail'}"
    if "any" in condition:
        parts = [describe_condition(c, entity_data) for c in condition["any"]]
        passed = any(evaluate_condition(c, entity_data) for c in condition["any"])
        return f"any([{', '.join(parts)}]) → {'pass' if passed else 'fail'}"
    if "not" in condition:
        inner = describe_condition(condition["not"], entity_data)
        passed = not evaluate_condition(condition["not"], entity_data)
        return f"not({inner}) → {'pass' if passed else 'fail'}"

    field = condition["field"]
    op = condition["op"]
    expected = condition.get("value")
    actual = _get_nested_field(entity_data, field)
    actual_str = str(actual)[:100] if actual is not None else "null"
    passed = evaluate_condition(condition, entity_data)
    return f"{field} {op} {expected}: actual={actual_str} → {'pass' if passed else 'fail'}"


_OPERATORS = {
    "equals": lambda a, e: a == e,
    "not_equals": lambda a, e: a != e,
    "contains": lambda a, e: e in str(a) if a else False,
    "not_contains": lambda a, e: e not in str(a) if a else True,
    "starts_with": lambda a, e: str(a).startswith(e) if a else False,
    "ends_with": lambda a, e: str(a).endswith(e) if a else False,
    "gt": lambda a, e: a > e if a is not None else False,
    "gte": lambda a, e: a >= e if a is not None else False,
    "lt": lambda a, e: a < e if a is not None else False,
    "lte": lambda a, e: a <= e if a is not None else False,
    "in": lambda a, e: a in e if isinstance(e, list) else False,
    "not_in": lambda a, e: a not in e if isinstance(e, list) else True,
    "matches": lambda a, e: bool(re.search(e, str(a))) if a else False,
    "exists": lambda a, e: a is not None,
    "older_than": lambda a, e: _older_than(a, e),
    "within": lambda a, e: not _older_than(a, e) if a else False,
}
