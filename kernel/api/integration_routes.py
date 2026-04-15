"""Integration management API endpoints.

Provides credential management, connectivity testing, and adapter upgrade
endpoints that are beyond auto-generated CRUD.
"""

from fastapi import APIRouter, Depends, HTTPException

from kernel.auth.middleware import check_permission, get_current_actor
from kernel.integration.credentials import fetch_credentials, store_credentials
from kernel.integration.registry import get_adapter_class
from kernel_entities.integration import Integration

integration_mgmt_router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@integration_mgmt_router.post("/{integration_id}/set-credentials")
async def set_credentials(
    integration_id: str,
    data: dict,
    actor=Depends(get_current_actor),
):
    """Store credentials in Secrets Manager for an integration."""
    check_permission(actor, "Integration", "write")
    integration = await Integration.get(integration_id)
    if not integration:
        raise HTTPException(404, "Integration not found")

    credentials = data.get("credentials", {})
    if not credentials:
        raise HTTPException(400, "No credentials provided")

    await store_credentials(integration.secret_ref, credentials)
    return {"status": "credentials_stored", "integration_id": integration_id}


@integration_mgmt_router.post("/{integration_id}/rotate-credentials")
async def rotate_credentials(
    integration_id: str,
    actor=Depends(get_current_actor),
):
    """Rotate credentials (provider-specific)."""
    check_permission(actor, "Integration", "write")
    integration = await Integration.get(integration_id)
    if not integration:
        raise HTTPException(404, "Integration not found")

    credentials = await fetch_credentials(integration.secret_ref)
    adapter_cls = get_adapter_class(integration.provider, integration.provider_version)
    adapter = adapter_cls(config=integration.config, credentials=credentials)

    try:
        new_credentials = await adapter.refresh_token()
        await store_credentials(integration.secret_ref, new_credentials)
        return {"status": "credentials_rotated", "integration_id": integration_id}
    except NotImplementedError:
        raise HTTPException(400, f"Provider {integration.provider} does not support rotation")


@integration_mgmt_router.post("/{integration_id}/test")
async def test_integration(
    integration_id: str,
    actor=Depends(get_current_actor),
):
    """Test connectivity by calling a read-only adapter method."""
    check_permission(actor, "Integration", "read")
    integration = await Integration.get(integration_id)
    if not integration:
        raise HTTPException(404, "Integration not found")

    credentials = await fetch_credentials(integration.secret_ref)
    adapter_cls = get_adapter_class(integration.provider, integration.provider_version)
    adapter = adapter_cls(config=integration.config, credentials=credentials)

    try:
        result = await adapter.fetch(limit=1)
        return {"status": "connected", "sample_count": len(result)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@integration_mgmt_router.post("/{integration_id}/upgrade")
async def upgrade_integration(
    integration_id: str,
    data: dict,
    actor=Depends(get_current_actor),
):
    """Upgrade adapter version (e.g., outlook v2 → v3). [G-30]"""
    check_permission(actor, "Integration", "write")
    integration = await Integration.get(integration_id)
    if not integration:
        raise HTTPException(404, "Integration not found")

    to_version = data.get("to_version")
    dry_run = data.get("dry_run", True)

    if not to_version:
        raise HTTPException(400, "to_version is required")

    # Verify target adapter exists
    try:
        get_adapter_class(integration.provider, to_version)
    except Exception:
        raise HTTPException(400, f"No adapter for {integration.provider}:{to_version}")

    if dry_run:
        return {
            "status": "dry_run",
            "current_version": integration.provider_version,
            "target_version": to_version,
        }

    integration.provider_version = to_version
    await integration.save_tracked(method="upgrade", method_metadata={"to_version": to_version})
    return {"status": "upgraded", "version": to_version}
