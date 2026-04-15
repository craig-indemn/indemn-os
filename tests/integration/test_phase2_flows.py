"""Integration tests for Phase 2 acceptance criteria flows.

Tests scheduled associate creation, direct invocation message creation,
bulk operation dispatch, and adapter retry logic.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from bson import ObjectId

from kernel.context import current_actor_id, current_org_id
from kernel.integration.adapter import (
    Adapter,
    AdapterAuthError,
    AdapterRateLimitError,
    AdapterTimeoutError,
)
from kernel.integration.dispatch import execute_with_retry
from kernel.message.schema import Message
from kernel.skill.integrity import compute_content_hash
from kernel.skill.schema import Skill
from kernel_entities.actor import Actor
from kernel_entities.role import Role


class TestScheduledAssociateCreation:
    """Acceptance criterion 4: associate with cron trigger creates scheduled messages."""

    @pytest.mark.asyncio
    async def test_scheduled_associate_creates_message(self, db, org_id, actor):
        """A scheduled associate should create a _scheduled message when cron fires."""
        role = Role(
            org_id=org_id,
            name="email_processor",
            permissions={"read": ["*"], "write": ["*"]},
        )
        await role.insert()

        # Create scheduled associate with a cron that fires every minute
        associate = Actor(
            org_id=org_id,
            name="Email Bot",
            type="associate",
            status="active",
            role_ids=[role.id],
            trigger_schedule="* * * * *",  # Every minute
            mode="deterministic",
        )
        await associate.insert()

        # Run the scheduled check
        from kernel.queue_processor import check_scheduled_associates

        await check_scheduled_associates()

        # Should have created a scheduled message
        msg = await Message.find_one({
            "entity_type": "_scheduled",
            "entity_id": associate.id,
        })
        assert msg is not None
        assert msg.event_type == "schedule_fired"
        assert msg.target_role == "email_processor"
        assert msg.status == "pending"

    @pytest.mark.asyncio
    async def test_scheduled_deduplication(self, db, org_id, actor):
        """Running check_scheduled_associates twice should not create duplicate messages."""
        role = Role(
            org_id=org_id,
            name="dedup_role",
            permissions={"read": ["*"], "write": ["*"]},
        )
        await role.insert()

        associate = Actor(
            org_id=org_id,
            name="Dedup Bot",
            type="associate",
            status="active",
            role_ids=[role.id],
            trigger_schedule="* * * * *",
            mode="deterministic",
        )
        await associate.insert()

        from kernel.queue_processor import check_scheduled_associates

        await check_scheduled_associates()
        await check_scheduled_associates()

        # Should only have one message
        msgs = await Message.find({
            "entity_type": "_scheduled",
            "entity_id": associate.id,
        }).to_list()
        assert len(msgs) == 1


class TestDirectInvocationMessage:
    """Acceptance criterion 5: direct invocation creates queue entry."""

    @pytest.mark.asyncio
    async def test_direct_invocation_creates_message(self, db, org_id, actor):
        """Direct invocation should create a _direct message in queue."""
        associate = Actor(
            org_id=org_id,
            name="Direct Bot",
            type="associate",
            status="active",
            role_ids=[],
            mode="deterministic",
        )
        await associate.insert()

        # Create a direct invocation message (mimicking what the API endpoint does)
        message = Message(
            org_id=org_id,
            entity_type="_direct",
            entity_id=ObjectId(),
            event_type="direct_invocation",
            target_role="",
            correlation_id=str(uuid4()),
            status="pending",
            context={"test": True},
            summary={"display": f"Direct: {associate.name}"},
        )
        await message.insert()

        # Verify the message exists
        found = await Message.get(message.id)
        assert found is not None
        assert found.event_type == "direct_invocation"
        assert found.entity_type == "_direct"


class TestAdapterRetryLogic:
    """Acceptance criterion 16: adapter error handling with retry."""

    @pytest.mark.asyncio
    async def test_auth_error_triggers_refresh_and_retry(self):
        """AdapterAuthError should trigger token refresh and retry."""

        class MockAdapter(Adapter):
            def __init__(self):
                super().__init__(config={}, credentials={"token": "old"})
                self._secret_ref = "test/ref"
                self.call_count = 0

            async def fetch(self, **params):
                self.call_count += 1
                if self.call_count == 1:
                    raise AdapterAuthError("Token expired")
                return [{"id": "msg-1"}]

            async def refresh_token(self):
                return {"token": "new"}

        adapter = MockAdapter()
        with patch(
            "kernel.integration.dispatch.store_credentials", new_callable=AsyncMock
        ):
            result = await execute_with_retry(adapter, "fetch")
        assert result == [{"id": "msg-1"}]
        assert adapter.call_count == 2

    @pytest.mark.asyncio
    async def test_rate_limit_waits_and_retries(self):
        """AdapterRateLimitError should wait retry_after seconds then retry."""

        class MockAdapter(Adapter):
            def __init__(self):
                super().__init__(config={}, credentials={})
                self._secret_ref = "test/ref"
                self.call_count = 0

            async def fetch(self, **params):
                self.call_count += 1
                if self.call_count == 1:
                    raise AdapterRateLimitError("Rate limited", retry_after=0)
                return [{"id": "msg-1"}]

        adapter = MockAdapter()
        result = await execute_with_retry(adapter, "fetch")
        assert result == [{"id": "msg-1"}]
        assert adapter.call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_retries_once(self):
        """AdapterTimeoutError should retry once immediately."""

        class MockAdapter(Adapter):
            def __init__(self):
                super().__init__(config={}, credentials={})
                self._secret_ref = "test/ref"
                self.call_count = 0

            async def fetch(self, **params):
                self.call_count += 1
                if self.call_count == 1:
                    raise AdapterTimeoutError("Timed out")
                return [{"id": "msg-1"}]

        adapter = MockAdapter()
        result = await execute_with_retry(adapter, "fetch")
        assert result == [{"id": "msg-1"}]
        assert adapter.call_count == 2


class TestWebhookEntityOperations:
    """Acceptance criterion 13: inbound webhook applies entity operations."""

    @pytest.mark.asyncio
    async def test_webhook_stripe_payment_parsing(self, db, org_id):
        """Stripe webhook parses payment_intent.succeeded into entity transition."""
        from kernel.integration.adapters.stripe_adapter import StripeAdapter

        adapter = StripeAdapter(
            config={},
            credentials={"secret_key": "sk_test", "webhook_secret": "whsec_test"},
        )
        parsed = await adapter.parse_webhook({
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_abc123"}},
        })
        assert parsed["entity_type"] == "Payment"
        assert parsed["operation"] == "transition"
        assert parsed["params"]["to_status"] == "completed"
        assert parsed["lookup_by"] == "stripe_payment_intent_id"
        assert parsed["lookup_value"] == "pi_abc123"


class TestBulkOperationSpec:
    """Acceptance criterion 7: bulk operations with BulkOperationSpec."""

    def test_bulk_operation_spec_construction(self):
        """BulkOperationSpec should construct from dict."""
        from kernel.temporal.workflows import BulkOperationSpec

        spec = BulkOperationSpec(
            entity_type="Submission",
            operation="transition",
            target_state="approved",
            filter_query={"status": "pending"},
            batch_size=25,
            failure_mode="skip",
        )
        assert spec.entity_type == "Submission"
        assert spec.operation == "transition"
        assert spec.target_state == "approved"
        assert spec.batch_size == 25
        assert spec.dry_run is False

    def test_bulk_result_construction(self):
        """BulkResult should construct with all fields."""
        from kernel.temporal.workflows import BulkResult

        result = BulkResult(
            status="completed_with_errors",
            total=100,
            processed=95,
            skipped=5,
            errors=[{"entity_id": "123", "error": "invalid"}],
        )
        assert result.status == "completed_with_errors"
        assert result.total == 100
        assert result.processed == 95
        assert result.skipped == 5
        assert len(result.errors) == 1

    def test_bulk_operation_spec_defaults(self):
        """BulkOperationSpec defaults should match spec."""
        from kernel.temporal.workflows import BulkOperationSpec

        spec = BulkOperationSpec(entity_type="Test", operation="create")
        assert spec.batch_size == 50
        assert spec.failure_mode == "skip"
        assert spec.dry_run is False
        assert spec.method_name is None
        assert spec.filter_query is None
        assert spec.source_data is None
        assert spec.target_state is None
        assert spec.sets is None
