"""JSON-safe serialization for API responses.

Pydantic v2 cannot serialize bson.ObjectId. Beanie v1.27 does not override
model_dump() to handle this. This module provides a single function that
all API routes use to convert entities to JSON-safe dicts.
"""

from datetime import date, datetime
from decimal import Decimal

from bson import ObjectId


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
