"""Bulk operations API — start BulkExecuteWorkflow via Temporal.

Entity-specific bulk endpoints are auto-registered. This provides
bulk monitoring commands (status, list, cancel).
"""

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from kernel.auth.middleware import get_current_actor
from kernel.temporal.client import get_temporal_client
from kernel.temporal.workflows import BulkExecuteWorkflow

bulk_router = APIRouter(prefix="/api/bulk", tags=["bulk"])


@bulk_router.post("/start")
async def start_bulk_operation(
    spec: dict,
    actor=Depends(get_current_actor),
):
    """Start a bulk operation workflow."""
    client = await get_temporal_client()
    if not client:
        raise HTTPException(503, "Temporal not available")

    workflow_id = f"bulk-{uuid4().hex[:12]}"
    await client.start_workflow(
        BulkExecuteWorkflow.run,
        args=[spec],
        id=workflow_id,
        task_queue="indemn-kernel",
    )

    return {"workflow_id": workflow_id, "status": "started"}


@bulk_router.get("/")
async def list_bulk_operations(
    status: str = None,
    actor=Depends(get_current_actor),
):
    """List active and recent bulk operations."""
    client = await get_temporal_client()
    if not client:
        raise HTTPException(503, "Temporal not available")

    # Query Temporal for bulk workflows
    workflows = []
    async for wf in client.list_workflows(query="WorkflowType = 'BulkExecuteWorkflow'"):
        info = {
            "workflow_id": wf.id,
            "status": wf.status.name if wf.status else "unknown",
            "start_time": str(wf.start_time) if wf.start_time else None,
        }
        if status is None or info["status"].lower() == status.lower():
            workflows.append(info)
    return workflows


@bulk_router.get("/{workflow_id}")
async def get_bulk_status(
    workflow_id: str,
    actor=Depends(get_current_actor),
):
    """Check status of a running bulk operation."""
    client = await get_temporal_client()
    if not client:
        raise HTTPException(503, "Temporal not available")

    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
        return {
            "workflow_id": workflow_id,
            "status": desc.status.name,
            "start_time": str(desc.start_time) if desc.start_time else None,
        }
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {e}")


@bulk_router.post("/{workflow_id}/cancel")
async def cancel_bulk_operation(
    workflow_id: str,
    actor=Depends(get_current_actor),
):
    """Cancel a running bulk operation at the next batch boundary."""
    client = await get_temporal_client()
    if not client:
        raise HTTPException(503, "Temporal not available")

    handle = client.get_workflow_handle(workflow_id)
    await handle.cancel()

    return {"workflow_id": workflow_id, "status": "cancel_requested"}
