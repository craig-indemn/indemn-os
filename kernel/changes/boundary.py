"""Audit-completeness boundary mechanism (Session-35 Decision D2).

Per D2: the kernel changes collection becomes a complete append-only audit
trail post-Stage-A. Entities created BEFORE Stage A deploys have empty
`changes` arrays on their create records (the legacy state — see Session 36
pre-Stage-A snapshot: 12,612 such records). The "audit-completeness
boundary" is the timestamp at which the audit trail became complete —
defined as `min(timestamp)` across all create records with a non-empty
`changes` array.

Stage C eval reconstruction (sub-piece 12 D-J) uses this boundary to decide:

  - Entity created BEFORE boundary → SKIP evaluation entirely. No
    EvaluationResult is written. Visible coverage gap accepted; visible
    data integrity preserved (per D18).
  - Entity created ON OR AFTER boundary → reconstruct full state from the
    changes collection via per-field FieldChange replay.

The boundary is **self-discovering**: queried at kernel startup, cached
in process. No code/deploy coupling — the value reflects actual data state.
Pre-deploy: `None` (no qualifying records yet). Post-deploy (after Stage A
A2 ships): the timestamp of the first qualifying create record.

Concurrency note: cache is per-process. Multiple kernel processes (e.g.,
indemn-api + indemn-temporal-worker + indemn-runtime-async) each derive
the boundary independently at startup. They will converge to the same value
since they all read the same source-of-truth collection.

**Startup pre-warming (Session-36 Dev#2, D2 strict reading)**: long-lived
processes (indemn-api FastAPI lifespan startup at `kernel/api/app.py`;
indemn-temporal-worker `main()` at `kernel/temporal/worker.py`) call
`get_audit_completeness_boundary()` after `init_database()` to populate
the cache before serving traffic. CLI processes (short-lived) skip the
pre-warm and rely on lazy-on-first-call. Either way, the value is stable
within a single process lifetime.
"""

from datetime import datetime
from typing import Optional

_cached_boundary: Optional[datetime] = None
_cache_set: bool = False


async def get_audit_completeness_boundary() -> Optional[datetime]:
    """Return the audit-completeness boundary timestamp (cached per process).

    Boundary = `min(timestamp)` across ChangeRecords where:
      - `change_type == "create"`
      - `changes` is a non-empty array

    Returns `None` when no qualifying records exist (pre-Stage-A-A2-deploy state).

    The first call queries MongoDB; subsequent calls return the cached value.
    For tests that need to re-derive (e.g., after seeding test data), call
    `reset_cache()` first.
    """
    global _cached_boundary, _cache_set
    if _cache_set:
        return _cached_boundary

    from kernel.changes.collection import ChangeRecord

    coll = ChangeRecord.get_motor_collection()
    pipeline = [
        {"$match": {"change_type": "create", "changes": {"$ne": []}}},
        {"$group": {"_id": None, "min_ts": {"$min": "$timestamp"}}},
    ]
    result = await coll.aggregate(pipeline).to_list(length=1)

    _cached_boundary = result[0]["min_ts"] if result else None
    _cache_set = True
    return _cached_boundary


def reset_cache() -> None:
    """Reset the cached boundary value — for tests only.

    Production code MUST NOT call this. The cache is intentionally process-lived
    so the boundary doesn't move during a process's lifetime; production
    re-derivation happens on process restart.
    """
    global _cached_boundary, _cache_set
    _cached_boundary = None
    _cache_set = False
