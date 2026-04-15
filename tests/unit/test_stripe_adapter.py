"""Tests for Stripe adapter — webhook parsing."""

import pytest

from kernel.integration.adapter import AdapterValidationError
from kernel.integration.adapters.stripe_adapter import StripeAdapter


class TestStripeWebhookParsing:
    def setup_method(self):
        self.adapter = StripeAdapter(
            config={},
            credentials={"secret_key": "sk_test_fake", "webhook_secret": "whsec_fake"},
        )

    @pytest.mark.asyncio
    async def test_payment_succeeded(self):
        body = {
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_123"}},
        }
        result = await self.adapter.parse_webhook(body)
        assert result["entity_type"] == "Payment"
        assert result["lookup_by"] == "stripe_payment_intent_id"
        assert result["lookup_value"] == "pi_123"
        assert result["operation"] == "transition"
        assert result["params"]["to_status"] == "completed"

    @pytest.mark.asyncio
    async def test_payment_failed(self):
        body = {
            "type": "payment_intent.payment_failed",
            "data": {"object": {"id": "pi_456"}},
        }
        result = await self.adapter.parse_webhook(body)
        assert result["params"]["to_status"] == "failed"

    @pytest.mark.asyncio
    async def test_unhandled_event_raises(self):
        body = {"type": "customer.created", "data": {"object": {"id": "cus_789"}}}
        with pytest.raises(AdapterValidationError, match="Unhandled Stripe event"):
            await self.adapter.parse_webhook(body)
