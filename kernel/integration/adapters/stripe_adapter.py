"""Stripe adapter — payment processing + webhook handling.

Uses the Stripe Python SDK. Sync SDK calls run in executor. [G-31]
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import stripe

from kernel.integration.adapter import (
    Adapter,
    AdapterValidationError,
)
from kernel.integration.registry import register_adapter


class StripeAdapter(Adapter):
    """Stripe payment adapter — outbound charges + inbound webhooks."""

    def __init__(self, config, credentials):
        super().__init__(config, credentials)
        stripe.api_key = credentials["secret_key"]

    async def test(self) -> dict:
        """Test Stripe connectivity by retrieving the account."""
        try:
            account = await asyncio.to_thread(stripe.Account.retrieve)
            return {"status": "ok", "account_id": account.id}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def charge(self, amount: Decimal, currency: str = "usd", **params) -> dict:
        """Create a Stripe PaymentIntent."""
        intent = await asyncio.to_thread(
            stripe.PaymentIntent.create,
            amount=int(amount * 100),
            currency=currency,
            metadata=params.get("metadata", {}),
        )
        return {
            "payment_intent_id": intent.id,
            "client_secret": intent.client_secret,
            "status": intent.status,
        }

    async def validate_webhook(self, headers: dict, body: bytes) -> bool:
        """Validate Stripe webhook signature."""
        sig = headers.get("stripe-signature", "")
        try:
            stripe.Webhook.construct_event(
                body, sig, self.credentials["webhook_secret"],
            )
            return True
        except stripe.error.SignatureVerificationError:
            return False

    async def parse_webhook(self, body: dict) -> dict:
        """Parse Stripe webhook into entity operations."""
        event_type = body.get("type", "")
        obj = body.get("data", {}).get("object", {})

        if event_type == "payment_intent.succeeded":
            return {
                "entity_type": "Payment",
                "lookup_by": "stripe_payment_intent_id",
                "lookup_value": obj["id"],
                "operation": "transition",
                "params": {
                    "to_status": "completed",
                    "charged_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        elif event_type == "payment_intent.payment_failed":
            return {
                "entity_type": "Payment",
                "lookup_by": "stripe_payment_intent_id",
                "lookup_value": obj["id"],
                "operation": "transition",
                "params": {"to_status": "failed"},
            }
        else:
            raise AdapterValidationError(f"Unhandled Stripe event: {event_type}")


register_adapter("stripe", "v1", StripeAdapter)
