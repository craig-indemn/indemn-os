"""Correlation ID management.

The correlation_id links related operations across the system:
- Entity save → watch evaluation → message creation → associate processing
- All share the same correlation_id = OTEL trace_id

Set in context.py via current_correlation_id.
Propagated through save_tracked() → messages → API headers → nested saves.
"""

from uuid import uuid4

from kernel.context import current_correlation_id


def get_or_create_correlation_id() -> str:
    """Get the current correlation ID or create a new one."""
    cid = current_correlation_id.get()
    if cid is None:
        cid = str(uuid4())
        current_correlation_id.set(cid)
    return cid
