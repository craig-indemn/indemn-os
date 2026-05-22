"""Verify the AI-406 conftest fixtures resolve correctly (Task 1.10.5)."""

import pytest

pytestmark = pytest.mark.asyncio


async def test_client_fixture_resolves(client):
    """client fixture yields an httpx.AsyncClient bound to the FastAPI app."""
    from httpx import AsyncClient

    assert isinstance(client, AsyncClient)


async def test_sample_runtime_fixture_resolves(sample_runtime):
    """sample_runtime yields an inserted Runtime in active status."""
    from kernel_entities.runtime import Runtime

    assert isinstance(sample_runtime, Runtime)
    assert sample_runtime.status == "active"
    assert sample_runtime.id is not None


async def test_sample_associate_fixture_resolves(sample_associate):
    """sample_associate yields an inserted Actor of type=associate."""
    from kernel_entities.actor import Actor

    assert isinstance(sample_associate, Actor)
    assert sample_associate.type == "associate"
    assert sample_associate.id is not None


async def test_sample_deployment_fixture_resolves(sample_deployment):
    """sample_deployment yields an inserted Deployment in active status with
    acts_as=session_actor and parameter_schema requiring actor_id."""
    from kernel_entities.deployment import Deployment

    assert isinstance(sample_deployment, Deployment)
    assert sample_deployment.status == "active"
    assert sample_deployment.acts_as == "session_actor"
    assert "actor_id" in sample_deployment.parameter_schema.get("required", [])
    assert sample_deployment.id is not None


async def test_paused_deployment_fixture_resolves(paused_deployment):
    """paused_deployment yields an inserted Deployment in paused status with
    acts_as=associate_self."""
    from kernel_entities.deployment import Deployment

    assert isinstance(paused_deployment, Deployment)
    assert paused_deployment.status == "paused"
    assert paused_deployment.acts_as == "associate_self"
