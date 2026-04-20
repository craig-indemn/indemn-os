"""Integration management API endpoints.

Provides credential management, connectivity testing, and adapter upgrade
endpoints that are beyond auto-generated CRUD.
"""

from fastapi import APIRouter, Depends, HTTPException

from kernel.auth.middleware import check_permission, get_current_actor
from kernel.db import ENTITY_REGISTRY
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


@integration_mgmt_router.post("/health-check")
async def integration_health_check(
    data: dict = {},
    actor=Depends(get_current_actor),
):
    """Check integration connectivity. Tests each adapter and updates last_checked_at."""
    from datetime import datetime, timezone

    from kernel.context import current_org_id
    from kernel.integration.dispatch import get_adapter

    Integration = ENTITY_REGISTRY.get("Integration")
    if not Integration:
        return {"error": "Integration entity not registered"}

    filter_doc = {"org_id": current_org_id.get()}
    if data.get("system_type"):
        filter_doc["system_type"] = data["system_type"]
    if data.get("status"):
        filter_doc["status"] = data["status"]

    integrations = await Integration.find(filter_doc).to_list(length=100)
    results = []

    for integ in integrations:
        try:
            adapter = await get_adapter(
                integ.system_type,
                actor_id=str(integ.owner_id) if integ.owner_type == "actor" else None,
                org_id=str(integ.org_id),
            )
            if hasattr(adapter, "test"):
                test_result = await adapter.test()
                integ.last_checked_at = datetime.now(timezone.utc)
                integ.last_error = None
                await integ.save_tracked(actor_id=str(actor.id), method="health_check")
                results.append(
                    {
                        "id": str(integ.id),
                        "name": integ.name,
                        "system_type": integ.system_type,
                        "provider": integ.provider,
                        "status": "healthy",
                        "detail": test_result,
                    }
                )
            else:
                results.append(
                    {
                        "id": str(integ.id),
                        "name": integ.name,
                        "system_type": integ.system_type,
                        "status": "no_test_method",
                    }
                )
        except Exception as e:
            integ.last_checked_at = datetime.now(timezone.utc)
            integ.last_error = str(e)[:500]
            try:
                await integ.save_tracked(actor_id=str(actor.id), method="health_check")
            except Exception:
                pass
            results.append(
                {
                    "id": str(integ.id),
                    "name": integ.name,
                    "system_type": integ.system_type,
                    "status": "error",
                    "error": str(e)[:500],
                }
            )

    return {
        "checked": len(results),
        "healthy": sum(1 for r in results if r["status"] == "healthy"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }
