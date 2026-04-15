"""Webhook endpoint — generic inbound handler.

Validates via adapter, parses into entity operations, applies via save_tracked().
Entity operations go through the state machine for enforcement and event emission.
"""

import logging

import orjson
from fastapi import APIRouter, HTTPException, Request

from kernel.context import current_actor_id, current_org_id
from kernel.db import ENTITY_REGISTRY
from kernel.integration.credentials import fetch_credentials
from kernel.integration.registry import get_adapter_class
from kernel_entities.integration import Integration

logger = logging.getLogger(__name__)

webhook_router = APIRouter(tags=["webhooks"])


@webhook_router.post("/webhook/{provider}/{integration_id}")
async def handle_webhook(provider: str, integration_id: str, request: Request):
    """Generic webhook handler.

    Validates via adapter, applies entity operations via save_tracked().
    """
    # Load integration
    integration = await Integration.get(integration_id)
    if not integration or integration.provider != provider:
        raise HTTPException(404, "Integration not found")
    if integration.status != "active":
        raise HTTPException(400, "Integration is not active")

    # Get adapter
    credentials = await fetch_credentials(integration.secret_ref)
    adapter_cls = get_adapter_class(integration.provider, integration.provider_version)
    adapter = adapter_cls(config=integration.config, credentials=credentials)

    # Validate webhook signature
    body_bytes = await request.body()
    headers = dict(request.headers)

    try:
        valid = await adapter.validate_webhook(headers, body_bytes)
    except NotImplementedError:
        raise HTTPException(400, f"Adapter {provider} does not support inbound webhooks")

    if not valid:
        raise HTTPException(401, "Invalid webhook signature")

    # Parse webhook into entity operations
    body_json = orjson.loads(body_bytes)
    parsed = await adapter.parse_webhook(body_json)

    # Set org context for entity operations
    current_org_id.set(integration.org_id)
    current_actor_id.set(f"webhook:{provider}")

    # Apply entity operations
    entity_cls = ENTITY_REGISTRY.get(parsed["entity_type"])
    if not entity_cls:
        raise HTTPException(400, f"Unknown entity type: {parsed['entity_type']}")

    entity = await entity_cls.find_one({
        parsed["lookup_by"]: parsed["lookup_value"],
        "org_id": integration.org_id,
    })

    if parsed["operation"] == "create":
        new_entity = entity_cls(org_id=integration.org_id, **parsed["params"])
        await new_entity.save_tracked(
            actor_id=f"webhook:{provider}",
            method="webhook_create",
        )
        return {"status": "ok"}

    if not entity:
        raise HTTPException(
            404,
            f"{parsed['entity_type']} not found: "
            f"{parsed['lookup_by']}={parsed['lookup_value']}",
        )

    if parsed["operation"] == "transition":
        entity.transition_to(parsed["params"]["to_status"])
        await entity.save_tracked(
            actor_id=f"webhook:{provider}",
            method=f"webhook_{parsed['operation']}",
            method_metadata={"webhook_event": body_json.get("type")},
        )
    elif parsed["operation"] == "update":
        for field, value in parsed["params"].items():
            setattr(entity, field, value)
        await entity.save_tracked(
            actor_id=f"webhook:{provider}",
            method=f"webhook_{parsed['operation']}",
        )

    return {"status": "ok"}
