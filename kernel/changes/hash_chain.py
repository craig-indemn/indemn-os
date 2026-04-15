"""Sequential hash chain for tamper-evident audit trail.

Each change record includes a SHA-256 hash of its content + the previous record's hash.
Tampering with any record breaks the chain. Verification is a CLI command.
"""

import hashlib

import orjson


def compute_hash(record) -> str:
    """SHA-256 hash of the record content for tamper evidence."""

    def _serialize_changes(changes):
        result = []
        for c in changes:
            d = c.model_dump()
            # Convert non-JSON-serializable values to strings
            for key in ("old_value", "new_value"):
                val = d.get(key)
                if val is not None and not isinstance(val, (str, int, float, bool, list, dict)):
                    d[key] = str(val)
            result.append(d)
        return result

    # Two normalizations for MongoDB round-trip consistency:
    # 1. strftime instead of isoformat — MongoDB returns naive datetimes,
    #    Python creates aware ones. isoformat differs (+00:00 vs none).
    # 2. Truncate microseconds to milliseconds — MongoDB drops the last 3 digits.
    ts = record.timestamp.replace(
        microsecond=(record.timestamp.microsecond // 1000) * 1000
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

    last = await ChangeRecord.find(
        {"org_id": org_id},
        session=session,
    ).sort("-timestamp").limit(1).to_list()
    return last[0].current_hash if last else None
