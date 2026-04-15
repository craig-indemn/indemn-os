"""Temporal workflows — durable execution wrappers.

ProcessMessageWorkflow: generic claim → process → complete for all associates.
HumanReviewWorkflow: waits for human decision via Temporal signals.
BulkExecuteWorkflow: batched entity operations with progress tracking.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from kernel.temporal.activities import (
        claim_message,
        complete_message,
        fail_message,
        load_entity_context,
        preview_bulk_operation,
        process_bulk_batch,
        process_human_decision,
        process_with_associate,
    )


@workflow.defn
class ProcessMessageWorkflow:
    """Generic claim → process → complete.

    Used by all associates regardless of role or skill.
    The skill is the source of truth — this workflow is a durability wrapper.
    """

    @workflow.run
    async def run(self, message_id: str, associate_id: str) -> dict:
        # Version gate for backward-compatible changes [G-77]
        workflow.patched("v2-enhanced-error-handling")

        # Activity 1: Claim the message from the queue
        claimed = await workflow.execute_activity(
            claim_message,
            args=[message_id, associate_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                initial_interval=timedelta(seconds=1),
            ),
        )
        if not claimed:
            return {"status": "already_claimed"}

        # Activity 2: Load entity context (fresh from MongoDB)
        context = await workflow.execute_activity(
            load_entity_context,
            args=[message_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                initial_interval=timedelta(seconds=2),
            ),
        )

        # Activity 3: Process (the associate does its work)
        try:
            result = await workflow.execute_activity(
                process_with_associate,
                args=[message_id, associate_id, context],
                start_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=5),
                    backoff_coefficient=2.0,
                    maximum_interval=timedelta(seconds=60),
                    non_retryable_error_types=[
                        "PermanentProcessingError",
                        "SkillTamperError",
                        "PermissionError",
                    ],
                ),
            )
        except Exception as e:
            # Activity 4a: Fail the message
            await workflow.execute_activity(
                fail_message,
                args=[message_id, str(e)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise

        # Activity 4b: Complete the message
        await workflow.execute_activity(
            complete_message,
            args=[message_id, result],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        return result


@workflow.defn
class HumanReviewWorkflow:
    """Workflow that waits for a human decision.

    Used for watches targeting human roles where the work requires
    a deliberate decision (approve/reject/escalate), not just processing.
    """

    def __init__(self):
        self._decision: Optional[dict] = None

    @workflow.signal
    async def submit_decision(self, decision: dict):
        """Signal handler — human makes their decision via UI/CLI.

        Decision: {"action": "approve"|"reject"|"escalate",
                   "reason": "...", "data": {...}}"""
        self._decision = decision

    @workflow.run
    async def run(self, message_id: str, escalation_hours: int = 48) -> dict:
        # Claim the message
        claimed = await workflow.execute_activity(
            claim_message,
            args=[message_id, "000000000000000000000000"],  # Sentinel ObjectId for human review
            start_to_close_timeout=timedelta(seconds=30),
        )
        if not claimed:
            return {"status": "already_claimed"}

        # Wait for human decision OR escalation timeout
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(hours=escalation_hours),
            )
        except asyncio.TimeoutError:
            await workflow.execute_activity(
                fail_message,
                args=[message_id, f"No decision within {escalation_hours} hours — escalated"],
                start_to_close_timeout=timedelta(seconds=30),
            )
            return {"status": "escalated", "reason": "timeout"}

        # Human made a decision — process it
        result = await workflow.execute_activity(
            process_human_decision,
            args=[message_id, self._decision],
            start_to_close_timeout=timedelta(minutes=5),
        )

        await workflow.execute_activity(
            complete_message,
            args=[message_id, result],
            start_to_close_timeout=timedelta(seconds=30),
        )

        return result


# --- Bulk operations ---


@dataclass
class BulkOperationSpec:
    entity_type: str
    operation: str  # "create", "transition", "method", "update", "delete"
    method_name: Optional[str] = None
    filter_query: Optional[dict] = None
    source_data: Optional[list] = None
    batch_size: int = 50
    failure_mode: str = "skip"  # "skip" or "abort"
    dry_run: bool = False
    target_state: Optional[str] = None
    sets: Optional[dict] = None


@dataclass
class BulkResult:
    status: str  # completed, completed_with_errors, failed
    total: int
    processed: int
    skipped: int
    errors: list


@workflow.defn
class BulkExecuteWorkflow:
    """Generic bulk operation workflow.

    bulk_operation_id = temporal_workflow_id [G-61] — deliberate coupling.
    """

    @workflow.run
    async def run(self, spec_dict: dict) -> dict:
        spec = BulkOperationSpec(**spec_dict)

        if spec.dry_run:
            preview = await workflow.execute_activity(
                preview_bulk_operation,
                args=[spec_dict],
                start_to_close_timeout=timedelta(minutes=2),
            )
            return {"status": "dry_run", "preview": preview}

        processed = 0
        total_errors = []
        total_count = 0

        while True:
            batch_result = await workflow.execute_activity(
                process_bulk_batch,
                args=[spec_dict, processed],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=5),
                    non_retryable_error_types=["BulkAbortError"],
                ),
            )

            processed += batch_result["batch_processed"]
            total_count = batch_result.get("total_count", total_count)
            total_errors.extend(batch_result.get("errors", []))

            if batch_result["done"]:
                break

            workflow.logger.info(f"Bulk progress: {processed}/{total_count}")

        status = "completed"
        if total_errors:
            status = "completed_with_errors"

        result = BulkResult(
            status=status,
            total=total_count,
            processed=processed,
            skipped=len(total_errors),
            errors=total_errors,
        )

        return {
            "status": result.status,
            "total": result.total,
            "processed": result.processed,
            "skipped": result.skipped,
            "errors": result.errors,
        }
