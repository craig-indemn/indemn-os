"""JSON-safe serialization for API responses.

Pydantic v2 cannot serialize bson.ObjectId. Beanie v1.27 does not override
model_dump() to handle this. This module provides a single function that
all API routes use to convert entities to JSON-safe dicts.

`to_dict` is the unconditional (no policy) serializer. `serialize_for_profile`
wraps it with per-field truncation policy driven by the entity's
`content_size_hint` metadata + the chosen profile (see context_profile.py).
"""

from datetime import date, datetime
from decimal import Decimal

from bson import ObjectId

from kernel.api.context_profile import apply_cap, cap_for


def to_dict(entity) -> dict:
    """Serialize any entity (kernel or domain) to a JSON-safe dict.

    Uses model_dump(by_alias=True) to get the raw data (Beanie uses _id alias),
    then recursively converts ObjectId, datetime, etc. to JSON-safe types.
    """
    # model_dump(by_alias=True) is the Beanie-native dump method.
    # We catch any serialization errors and fall back to __dict__.
    try:
        raw = entity.model_dump(by_alias=True)
    except Exception:
        # Fallback: use __dict__ for edge cases
        raw = {k: v for k, v in entity.__dict__.items() if not k.startswith("_")}
    return _convert(raw)


def serialize_for_profile(entity_cls, entity, profile: str = "raw") -> dict:
    """Serialize an entity to a JSON-safe dict, applying per-field
    truncation per the chosen profile.

    Per-field policy comes from `entity_cls._field_definitions[fname]
    .content_size_hint`. The mapping from hint → byte cap lives in
    `kernel/api/context_profile.py::PROFILE_CAPS` and varies per profile.

    Kernel entities (no FieldDefinition) short-circuit to `to_dict(entity)`
    unchanged — they have no per-field policy by design. This preserves
    rich kernel-entity payloads (Trace.outputs etc.) and matches the
    architectural principle "policy lives on the entity definition."

    Profile `raw` (default) applies no caps and is a no-op over `to_dict`.
    Unknown profile silently falls back to no caps via `cap_for`.

    The truncation marker (see context_profile.TRUNCATION_MARKER_TEMPLATE)
    points to the `raw` profile as the escape hatch for full content.
    """
    raw = to_dict(entity)

    # Kernel entities have no FieldDefinition metadata — return as-is.
    # This is the explicit "policy lives on the entity definition" branch.
    field_definitions = getattr(entity_cls, "_field_definitions", None)
    if not field_definitions:
        return raw

    for fname, value in list(raw.items()):
        if not isinstance(value, str):
            continue
        fdef = field_definitions.get(fname)
        hint = fdef.content_size_hint if fdef is not None else None
        cap = cap_for(hint, profile)
        raw[fname] = apply_cap(value, cap)
    return raw


def _convert(obj):
    """Recursively convert non-JSON-serializable types."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _convert(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert(v) for v in obj]
    if isinstance(obj, tuple):
        return [_convert(v) for v in obj]
    if isinstance(obj, set):
        return [_convert(v) for v in obj]
    return obj
