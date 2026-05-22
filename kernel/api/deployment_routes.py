"""Custom routes for the Deployment entity beyond the auto-generated CRUD.

`/api/deployments/{id}/public` is the surface-safe view that the embed.js SDK
fetches to render a chat/voice widget. Returns only the fields the surface
needs to render and open a session — excludes `llm_override`, `static_parameters`
(may contain secrets), and org internals.

Per §10.7 threat model: Deployment IDs are semi-public. The auth gate at
`/sessions` enforces actual access; do NOT add secrecy / rate-limit layers
on `/public` — that would break the embed-snippet model the SDK depends on.
"""

from bson import ObjectId
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from kernel_entities.brand_assets import BrandAssets
from kernel_entities.deployment import Deployment
from kernel_entities.runtime import Runtime
from kernel_entities.surface_config import SurfaceConfig

deployment_router = APIRouter(prefix="/api/deployments", tags=["Deployment"])

# Runtime.kind uses LONG-FORM (realtime_chat / realtime_voice / etc.) because
# Runtime tracks both channel and class (realtime vs async). The /public endpoint
# returns SHORT-FORM channel_kind to match SurfaceConfig.channel_kind's enum and
# the embed.js SDK routing logic (chat/voice/slack/teams/sms/email). Map here
# so consumers downstream see one canonical short-form value.
_RUNTIME_KIND_TO_CHANNEL = {
    "realtime_chat": "chat",
    "realtime_voice": "voice",
    "realtime_sms": "sms",
    "realtime_slack": "slack",
    "realtime_teams": "teams",
    "realtime_email": "email",
    "async_worker": "async",  # defensive — /public shouldn't be called on async
}


@deployment_router.get("/{deployment_id}/public")
async def get_deployment_public(deployment_id: str):
    """Return the surface-safe view of a Deployment for the embed.js SDK.

    Uses `JSONResponse` (NOT `HTTPException`) for error bodies so the
    surface-facing contract is a flat `{"error": ..., ...}` dict rather than
    FastAPI's wrapped `{"detail": {...}}`. The SDK + downstream tests assert
    on top-level keys.
    """
    try:
        oid = ObjectId(deployment_id)
    except Exception:
        return JSONResponse({"error": "invalid_id"}, status_code=400)

    deployment = await Deployment.get(oid)
    if not deployment:
        return JSONResponse(
            {"error": "not_found", "resource": "deployment"}, status_code=404
        )

    if deployment.status != "active":
        return JSONResponse(
            {"error": "deployment_not_active", "status": deployment.status},
            status_code=409,
        )

    # Resolve Runtime + endpoint
    runtime = await Runtime.get(deployment.runtime_id)
    if not runtime:
        return JSONResponse({"error": "runtime_missing"}, status_code=500)

    channel_kind = _RUNTIME_KIND_TO_CHANNEL.get(runtime.kind, runtime.kind)
    runtime_endpoint = (
        runtime.transport_config.get("endpoint_url") if runtime.transport_config else None
    )

    # Resolve SurfaceConfig summary (if any)
    surface_config_summary = None
    if deployment.surface_config_id:
        sc = await SurfaceConfig.get(deployment.surface_config_id)
        if sc:
            brand_assets_summary = None
            if sc.brand_assets_id:
                ba = await BrandAssets.get(sc.brand_assets_id)
                if ba:
                    brand_assets_summary = {
                        "primary_color": ba.primary_color,
                        "secondary_color": ba.secondary_color,
                        "accent_color": ba.accent_color,
                        "font_family_heading": ba.font_family_heading,
                        "font_family_body": ba.font_family_body,
                        "logo_url": ba.logo_url,
                    }
            surface_config_summary = {
                "vendor": sc.vendor,
                "channel_kind": sc.channel_kind,
                "config": sc.config,
                "brand_assets": brand_assets_summary,
            }

    return {
        "_id": str(deployment.id),
        "channel_kind": channel_kind,
        "runtime_endpoint": runtime_endpoint,
        "surface_config": surface_config_summary,
        "greeting": deployment.greeting,
        "parameter_schema": deployment.parameter_schema,
        "acts_as": deployment.acts_as,
        "allowed_origins": deployment.allowed_origins,
        # resumption_config is consumed by embed.js SDK (Phase 4 Task 4A.2's
        # DeploymentPublic interface) so the SDK can present
        # "this session is resumable for N hours" UX. Including here matches
        # the SDK's expected shape.
        "resumption_config": deployment.resumption_config,
    }
