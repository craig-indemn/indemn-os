"""Integration tests for GET /api/deployments/{id}/public (AI-404-1).

Per §9 + §10.3: the embed.js SDK fetches this endpoint to retrieve the
surface-safe field subset for rendering the chat/voice widget. The endpoint
is non-authed-or-soft-authed (Deployment IDs are semi-public per §10.7
threat model "deployment_id enumeration" row — auth gate is at /sessions,
not via /public secrecy).
"""

import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio


async def test_public_returns_safe_fields(client, db, org_id, sample_deployment):
    """Returns the surface-safe field subset only."""
    response = await client.get(f"/api/deployments/{sample_deployment.id}/public")

    assert response.status_code == 200
    body = response.json()

    # MUST contain — surface needs these to render + open session
    assert "channel_kind" in body
    assert "runtime_endpoint" in body
    assert "surface_config" in body  # summary (may be None if no surface_config_id)
    assert "greeting" in body
    assert "parameter_schema" in body
    assert "acts_as" in body
    assert "allowed_origins" in body
    assert "_id" in body  # deployment_id is semi-public per §10.7

    # MUST NOT contain — protect operator secrets / internals
    assert "llm_override" not in body
    assert "static_parameters" not in body  # could carry secrets
    assert "org_id" not in body


async def test_public_returns_404_for_unknown_id(client, db, org_id):
    """Non-existent deployment_id → 404 with flat error body."""
    response = await client.get(f"/api/deployments/{ObjectId()}/public")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert body["resource"] == "deployment"


async def test_public_rejects_paused_deployment(client, db, org_id, paused_deployment):
    """Paused Deployments reject /public (they reject sessions anyway)."""
    response = await client.get(f"/api/deployments/{paused_deployment.id}/public")
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "deployment_not_active"
    assert body["status"] == "paused"


async def test_public_returns_400_for_malformed_id(client, db, org_id):
    """Non-hex deployment_id → 400 with flat error body."""
    response = await client.get("/api/deployments/not-a-real-objectid/public")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_id"
