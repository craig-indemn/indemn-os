"""Sequential hash chain for tamper-evident audit trail.

Each change record includes a SHA-256 hash of its content + the previous record's hash.
Tampering with any record breaks the chain. Verification is a CLI command.
"""

import hashlib
from datetime import datetime
from decimal import Decimal

import orjson
from bson import ObjectId


def _normalize_value(val):
    """Normalize a value for consistent hashing across MongoDB round-trips.

    After round-trip: datetime loses tzinfo and microsecond precision,
    ObjectId may arrive as str, Decimal becomes float.
    """
    if val is None:
        return val
    if isinstance(val, datetime):
        return val.replace(
            tzinfo=None,
            microsecond=(val.microsecond // 1000) * 1000,
        ).strftime("%Y-%m-%dT%H:%M:%S.%f")
    if isinstance(val, ObjectId):
        return str(val)
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, (list, dict)):
        return val
    # Fallback: stringify unknown types
    return str(val)


def compute_hash(record) -> str:
    """SHA-256 hash of the record content for tamper evidence."""

    def _serialize_changes(changes):
        result = []
        for c in changes:
            d = c.model_dump()
            for key in ("old_value", "new_value"):
                d[key] = _normalize_value(d.get(key))
            result.append(d)
        return result

    # Normalize timestamp for MongoDB round-trip consistency:
    # 1. Strip tzinfo — MongoDB returns naive datetimes,
    #    Python creates aware ones.
    # 2. Truncate microseconds to milliseconds — MongoDB drops the last 3 digits.
    # 3. strftime instead of isoformat — avoids +00:00 vs naive divergence.
    ts = record.timestamp.replace(
        tzinfo=None,
        microsecond=(record.timestamp.microsecond // 1000) * 1000,
    )
    content = orjson.dumps(
        {
            "entity_type": record.entity_type,
            "entity_id": str(record.entity_id),
            "change_type": record.change_type,
            "actor_id": record.actor_id,
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "changes": _serialize_changes(record.changes),
            "previous_hash": record.previous_hash,
        },
        option=orjson.OPT_SORT_KEYS,
        default=str,  # Fallback: convert non-serializable types to str
    )
    return hashlib.sha256(content).hexdigest()


async def get_previous_hash(org_id, session=None) -> str:
    """Get the hash of the most recent change record for this org."""
    from kernel.changes.collection import ChangeRecord

    last = (
        await ChangeRecord.find(
            {"org_id": org_id},
            session=session,
        )
        .sort("-timestamp")
        .limit(1)
        .to_list()
    )
    return last[0].current_hash if last else None
