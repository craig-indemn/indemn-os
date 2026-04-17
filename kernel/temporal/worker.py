"""Temporal worker — entry point for associate and kernel workflows.

Runs: python -m kernel.temporal.worker

Processes associate workflows (ProcessMessage, HumanReview) and
kernel workflows (BulkExecute) with OTEL tracing.
"""

import asyncio
import logging
from datetime import timedelta

from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker

from kernel.db import init_database
from kernel.observability.logging import setup_logging
from kernel.observability.tracing import init_tracing
from kernel.temporal.activities import (
    claim_message,
    complete_message,
    fail_message,
    load_actor,
    load_entity_context,
    preview_bulk_operation,
    process_bulk_batch,
    process_human_decision,
)
from kernel.temporal.client import get_temporal_client
from kernel.temporal.workflows import (
    BulkExecuteWorkflow,
    HumanReviewWorkflow,
    ProcessMessageWorkflow,
)

logger = logging.getLogger(__name__)


async def main():
    setup_logging()
    init_tracing()
    await init_database()

    client = await get_temporal_client()
    if not client:
        logger.error("Temporal client not available. Set TEMPORAL_ADDRESS.")
        return

    # [G-19] OTEL TracingInterceptor on every workflow + activity
    interceptors = [TracingInterceptor()]

    worker = Worker(
        client,
        task_queue="indemn-kernel",
        workflows=[
            ProcessMessageWorkflow,
            HumanReviewWorkflow,
            BulkExecuteWorkflow,
        ],
        activities=[
            claim_message,
            load_actor,
            load_entity_context,
            process_human_decision,
            complete_message,
            fail_message,
            process_bulk_batch,
            preview_bulk_operation,
        ],
        interceptors=interceptors,
        # [G-23] Production configuration
        max_concurrent_activities=20,
        max_concurrent_workflow_tasks=10,
        graceful_shutdown_timeout=timedelta(seconds=30),
    )

    logger.info("Temporal worker started on task queue 'indemn-kernel'")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
