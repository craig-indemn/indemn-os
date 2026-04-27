"""Field-aware MongoDB filter safelist for the list endpoint and bulk routes.

Translates JSON filter input (from CLI/UI/harnesses) into a MongoDB-ready
filter dict with three safety layers:

1. Field-name safelist — every top-level key must be a defined field on the
   target entity (`entity_cls.model_fields`). Unknown fields raise 400 with
   the actual known fields, so a typo doesn't silently match nothing.

2. Operator allowlist — operator dicts (`{"$in": [...]}`, `{"$gte": ...}`)
   are validated against a fixed allowlist. Unknown operators raise 400.
   Logical composition (`$or`/`$and`/`$not`) is intentionally deferred —
   they require nesting field-name safelists and aren't on the path of any
   current bug.

3. Per-field type coercion — string values for ObjectId or datetime fields
   are coerced to typed values, plus extended-JSON shapes (`{"$oid": ...}`,
   `{"$date": ...}`) are recognized. This is the actual root cause of
   Bug #23: MongoDB is type-strict, so a string `"69eb..."` is not equal
   to `ObjectId("69eb...")` and a string `"2026-04-23T..."` is not >= a
   stored datetime. Coercion happens for plain values AND for operator
   operands ($ne, $gt/$gte/$lt/$lte) and list elements ($in/$nin).

Used by `kernel/api/registration.py` (list endpoint + per-entity bulk route).
Returns a MongoDB-ready dict; raises HTTPException 400 with self-correcting
error messages on invalid input.
"""
from __future__ import annotations

import typing
from datetime import datetime
from typing import Any

import orjson
from bson import ObjectId
from fastapi import HTTPException

# Operators callers can pass through. Logical composition deferred.
_ALLOWED_OPERATORS = frozenset(
    {"$in", "$nin", "$ne", "$gt", "$gte", "$lt", "$lte", "$exists"}
)
_LIST_OPERATORS = frozenset({"$in", "$nin"})
_BOOL_OPERATORS = frozenset({"$exists"})


def _unwrap_optional(annotation):
    """If annotation is Optional[X] / Union[X, None], return X. Else return annotation as-is.

    Pydantic models routinely declare `Optional[ObjectId]` for nullable
    relationship fields; without unwrapping we'd treat them as Union and
    miss the coercion.
    """
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if origin is typing.Union and type(None) in args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _is_objectid_field(annotation) -> bool:
    return _unwrap_optional(annotation) is ObjectId


def _is_datetime_field(annotation) -> bool:
    return _unwrap_optional(annotation) is datetime


def _parse_iso_datetime(raw: str) -> datetime:
    """Parse an ISO 8601 string. Tolerates trailing 'Z' (UTC) on Python <3.11."""
    if raw.endswith("Z"):
        return datetime.fromisoformat(raw[:-1] + "+00:00")
    return datetime.fromisoformat(raw)


_EXTENDED_JSON_KEYS = frozenset({"$oid", "$date"})


def _is_extended_json_shape(value: Any) -> bool:
    """True if `value` is a single-key dict whose key is `$oid` or `$date`.

    These are type-tags, not operators — they get unwrapped to typed values
    before the operator-dispatch branch fires.
    """
    return (
        isinstance(value, dict)
        and len(value) == 1
        and next(iter(value)) in _EXTENDED_JSON_KEYS
    )


def _coerce_extended_json(value: Any) -> Any:
    """Recognize MongoDB extended-JSON shapes. Returns typed value or input as-is.

    {"$oid": "<24-char hex>"}    -> bson.ObjectId
    {"$date": "<ISO 8601 str>"} -> datetime

    Only triggers on single-key dicts whose key is `$oid` or `$date`. Won't
    collide with legitimate dict-typed field values that happen to contain
    those keys alongside others.
    """
    if isinstance(value, dict) and len(value) == 1:
        if "$oid" in value:
            try:
                return ObjectId(value["$oid"])
            except Exception as e:
                raise HTTPException(400, f"Invalid $oid: {value['$oid']!r} ({e})")
        if "$date" in value:
            raw = value["$date"]
            if not isinstance(raw, str):
                raise HTTPException(
                    400, f"$date must be an ISO 8601 string, got: {raw!r}"
                )
            try:
                return _parse_iso_datetime(raw)
            except Exception as e:
                raise HTTPException(400, f"Invalid $date: {raw!r} ({e})")
    return value


def _coerce_value_for_field(field_name: str, annotation, value: Any) -> Any:
    """Coerce a leaf value to match the field's MongoDB-stored type.

    - null passes through (matches docs where the field is null)
    - extended-JSON shapes are unwrapped to typed values first
    - ObjectId fields: 24-char hex string -> bson.ObjectId
    - datetime fields: ISO 8601 string -> datetime
    - other types: pass through (MongoDB type-strict equality applies)
    """
    if value is None:
        return None
    value = _coerce_extended_json(value)
    if isinstance(value, str):
        if _is_objectid_field(annotation):
            try:
                return ObjectId(value)
            except Exception:
                raise HTTPException(
                    400,
                    f"Field '{field_name}' is an ObjectId; expected a 24-char hex "
                    f'string or {{"$oid": ...}}, got: {value!r}',
                )
        if _is_datetime_field(annotation):
            try:
                return _parse_iso_datetime(value)
            except Exception:
                raise HTTPException(
                    400,
                    f"Field '{field_name}' is a datetime; expected an ISO 8601 "
                    f'string or {{"$date": ...}}, got: {value!r}',
                )
    return value


def _parse_operator_value(field_name: str, annotation, op_dict: dict) -> dict:
    """Validate an operator dict against the safelist + coerce operand types.

    Raises 400 on unknown operators, wrong operand shapes, or nested
    operator dicts. Returns a MongoDB-ready operator dict.
    """
    parsed: dict = {}
    for op, operand in op_dict.items():
        if op not in _ALLOWED_OPERATORS:
            allowed = ", ".join(sorted(_ALLOWED_OPERATORS))
            raise HTTPException(
                400,
                f"Operator {op!r} on field {field_name!r} is not in the safelist. "
                f"Allowed operators: {allowed}",
            )
        if op in _BOOL_OPERATORS:
            if not isinstance(operand, bool):
                raise HTTPException(
                    400,
                    f"Operator {op!r} on field {field_name!r} requires a boolean "
                    f"operand, got: {operand!r}",
                )
            parsed[op] = operand
            continue
        if op in _LIST_OPERATORS:
            if not isinstance(operand, list):
                raise HTTPException(
                    400,
                    f"Operator {op!r} on field {field_name!r} requires a list "
                    f"operand, got: {operand!r}",
                )
            parsed[op] = [
                _coerce_value_for_field(field_name, annotation, item)
                for item in operand
            ]
            continue
        # Scalar operators ($ne, $gt, $gte, $lt, $lte): coerce operand per field type.
        # Extended-JSON ({"$oid"/"$date": ...}) is allowed because it's a typed
        # value, not a nested operator. Anything else $-prefixed is a malformed
        # nested operator and gets rejected.
        if (
            isinstance(operand, dict)
            and not _is_extended_json_shape(operand)
            and any(isinstance(k, str) and k.startswith("$") for k in operand)
        ):
            raise HTTPException(
                400,
                f"Nested operator dicts on field {field_name!r} are not supported "
                f"(operator {op!r} got operator-dict operand).",
            )
        parsed[op] = _coerce_value_for_field(field_name, annotation, operand)
    return parsed


def parse_filter(entity_cls, entity_name: str, filter_input: Any) -> dict:
    """Parse and validate a JSON filter against the entity's schema.

    Accepts either:
      - a JSON string (from `?filter=...` query parameter)
      - an already-parsed dict (from a bulk operation's `filter_query`)
      - None / empty (returns {})

    Returns a MongoDB-ready filter dict with field-name safelisting,
    operator allowlist, and per-field type coercion applied.

    Raises HTTPException 400 with self-correcting error messages on any
    validation failure — never silently drops or no-matches.
    """
    if filter_input is None:
        return {}
    if isinstance(filter_input, str):
        try:
            user_filter = orjson.loads(filter_input)
        except Exception as e:
            raise HTTPException(400, f"Invalid JSON in filter: {e}")
    else:
        user_filter = filter_input

    if not isinstance(user_filter, dict):
        raise HTTPException(
            400, 'filter must be a JSON object (e.g. {"field": "value"})'
        )
    if not user_filter:
        return {}

    valid_fields = set(entity_cls.model_fields.keys())
    parsed: dict = {}
    for field_name, value in user_filter.items():
        # Top-level $-prefixed keys would be logical operators ($or/$and/$not)
        # which are intentionally not in scope. Reject explicitly so the error
        # message doesn't blame a "field" name.
        if isinstance(field_name, str) and field_name.startswith("$"):
            raise HTTPException(
                400,
                f"Top-level operator {field_name!r} is not supported. "
                f"Use field-level filters (e.g. {{\"field\": {{\"$in\": [...]}}}}).",
            )
        if field_name not in valid_fields:
            sample = ", ".join(sorted(valid_fields)[:10])
            raise HTTPException(
                400,
                f"Unknown field '{field_name}' on {entity_name}. "
                f"Known fields include: {sample}"
                + ("..." if len(valid_fields) > 10 else ""),
            )
        annotation = entity_cls.model_fields[field_name].annotation
        # Route between three shapes:
        #   1. Extended-JSON tag at leaf ({"$oid": ...} / {"$date": ...})
        #      -> coerce to typed value (handled by _coerce_value_for_field)
        #   2. Pure operator dict (all keys $-prefixed, e.g. {"$in": [...]})
        #      -> validate against safelist and coerce operands
        #   3. Plain value, including mixed-key dicts like {"$x": 1, "y": 2}
        #      -> coerce per field type (lets embedded-doc equality work; a
        #      malformed mixed-key dict would error in MongoDB itself, but the
        #      safelist's job is field-and-operator gate, not query syntax check)
        if (
            isinstance(value, dict)
            and value
            and not _is_extended_json_shape(value)
            and all(isinstance(k, str) and k.startswith("$") for k in value)
        ):
            parsed[field_name] = _parse_operator_value(field_name, annotation, value)
        else:
            parsed[field_name] = _coerce_value_for_field(field_name, annotation, value)
    return parsed
