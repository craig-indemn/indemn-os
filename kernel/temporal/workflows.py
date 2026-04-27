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
    from indemn_os.types import AgentExecutionInput

    from kernel.temporal.activities import (
        claim_message,
        complete_message,
        fail_message,
        load_actor,
        preview_bulk_operation,
        process_bulk_batch,
        process_human_decision,
    )


@workflow.defn
class ProcessMessageWorkflow:
    """Generic claim → dispatch to harness.

    Claims the message, loads the associate's actor to find the Runtime,
    then dispatches the work to the Runtime's task queue where the harness
    worker picks it up. Harness owns completion and failure reporting
    via `indemn queue complete` / `indemn queue fail`.
    """

    @workflow.run
    async def run(self, message_id: str, associate_id: str) -> dict:
        # Version gate for backward-compatible changes [G-77]
        workflow.patched("v2-enhanced-error-handling")
        workflow.patched("v3-harness-dispatch")

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

        # Activity 2: Load actor to get runtime_id for dispatch routing
        actor = await workflow.execute_activity(
            load_actor,
            args=[associate_id],
            start_to_close_timeout=timedelta(seconds=10),
        )
        runtime_id = actor.get("runtime_id")
        if not runtime_id:
            await workflow.execute_activity(
                fail_message,
                args=[message_id, "no_runtime_configured"],
                start_to_close_timeout=timedelta(seconds=30),
            )
            return {"status": "failed", "reason": "no_runtime"}

        # Activity 3: Dispatch to harness worker on runtime-specific queue
        # Activity name is a string — the definition lives in the harness, not kernel.
        # Harness owns completion (`indemn queue complete`) and failure (`indemn queue fail`).
        agent_input = AgentExecutionInput(
            message_id=message_id,
            associate_id=associate_id,
            entity_type=claimed.get("entity_type", ""),
            entity_id=claimed.get("entity_id", ""),
            correlation_id=claimed.get("correlation_id", ""),
            depth=claimed.get("depth", 0),
        )
        result = await workflow.execute_activity(
            "process_with_associate",
            agent_input,
            task_queue=f"runtime-{runtime_id}",
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(seconds=90),
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                initial_interval=timedelta(seconds=5),
                non_retryable_error_types=[
                    "PermanentProcessingError",
                    "SkillTamperError",
                    "PermissionError",
                    "CLIError",
                    "ValidationError",
                ],
            ),
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
    org_id: Optional[str] = None


@dataclass
class BulkResult:
    """Counts surfaced from BulkExecuteWorkflow (Bug #24).

    matched   = how many entities matched the filter / source_data length.
                A bulk-delete with no matches has matched == 0, which the
                operator needs to see — previously this looked indistinguishable
                from a successful delete in the status endpoint.
    succeeded = how many save_tracked()/delete_one calls actually committed.
    errored   = how many entities raised StateMachine/Validation/Permission
                errors (= len(errors)). The full per-entity error records
                live in `errors` for diagnosis.
    """

    status: str  # completed | completed_no_match | completed_with_errors | failed | dry_run
    matched: int
    succeeded: int
    errored: int
    errors: list


def summarize_bulk_status(matched: int, errors: list) -> str:
    """Pick the right terminal status string given the counts (Bug #24).

    Extracted from BulkExecuteWorkflow.run so unit tests can exercise the
    status-determination logic without spinning up a workflow environment.
    """
    if matched == 0:
        return "completed_no_match"
    if errors:
        return "completed_with_errors"
    return "completed"


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

        succeeded = 0
        all_errors: list = []
        matched = 0

        while True:
            batch_result = await workflow.execute_activity(
                process_bulk_batch,
                args=[spec_dict, succeeded + len(all_errors)],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=5),
                    non_retryable_error_types=["BulkAbortError"],
                ),
            )

            succeeded += batch_result["batch_processed"]
            matched = batch_result.get("total_count", matched)
            all_errors.extend(batch_result.get("errors", []))

            if batch_result["done"]:
                break

            workflow.logger.info(f"Bulk progress: succeeded={succeeded} matched={matched}")

        # Distinguish "did nothing because filter matched no entities" from
        # "completed with errors" from "completed cleanly" — Bug #24.
        return {
            "status": summarize_bulk_status(matched, all_errors),
            "matched": matched,
            "succeeded": succeeded,
            "errored": len(all_errors),
            "errors": all_errors,
        }
