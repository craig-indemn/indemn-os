"""Credential rotation — provider-specific rotation with audit.

Rotates credentials for an integration:
1. Adapter generates new credentials (if supported)
2. New credentials stored in Secrets Manager
3. Cache invalidated
4. Integration entity updated and audited
"""

from datetime import datetime, timezone

from kernel.integration.credentials import invalidate_cached_credentials, store_credentials
from kernel.integration.dispatch import get_adapter
from kernel_entities.integration import Integration


async def rotate_credentials(integration_id: str, actor_id: str) -> dict:
    """Rotate credentials for an integration."""
    integration = await Integration.get(integration_id)
    if not integration:
        raise ValueError(f"Integration {integration_id} not found")

    adapter = await get_adapter(integration.system_type)

    # Provider-specific rotation (if supported)
    if hasattr(adapter, "rotate_credentials"):
        new_creds = await adapter.rotate_credentials()
        await store_credentials(integration.secret_ref, new_creds)
    else:
        raise ValueError(f"Adapter {integration.provider} does not support automatic rotation")

    # Invalidate cache
    invalidate_cached_credentials(integration.secret_ref)

    # Audit
    integration.last_checked_at = datetime.now(timezone.utc)
    await integration.save_tracked(
        actor_id=actor_id,
        method="rotate_credentials",
    )

    return {"status": "rotated", "integration": integration.name}
