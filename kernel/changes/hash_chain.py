"""Sequential hash chain for tamper-evident audit trail.

Each change record includes a SHA-256 hash of its content + the previous record's hash.
Tampering with any record breaks the chain. Verification is a CLI command.
"""

import hashlib

import orjson


def compute_hash(record) -> str:
    """SHA-256 hash of the record content for tamper evidence."""
    content = orjson.dumps(
        {
            "entity_type": record.entity_type,
            "entity_id": str(record.entity_id),
            "change_type": record.change_type,
            "actor_id": record.actor_id,
            "timestamp": record.timestamp.isoformat(),
            "changes": [c.model_dump() for c in record.changes],
            "previous_hash": record.previous_hash,
        },
        option=orjson.OPT_SORT_KEYS,
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
