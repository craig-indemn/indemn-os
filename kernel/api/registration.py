"""Auto-register API routes from entity definitions.

Every entity type gets CRUD + transition + @exposed methods + capability routes.
This is the self-evidence property: define an entity, its API exists.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from kernel.auth.middleware import check_permission, get_current_actor
from kernel.context import current_org_id


def _fire_dispatch(created_messages):
    """Fire-and-forget optimistic dispatch after save_tracked commits."""
    if created_messages:
        from kernel.message.dispatch import optimistic_dispatch

        asyncio.create_task(optimistic_dispatch(created_messages))


def register_entity_routes(app, entity_name: str, entity_cls: type):
    """Register CRUD + transition + @exposed method + capability routes."""
    slug = entity_name.lower() + "s"
    router = APIRouter(prefix=f"/api/{slug}", tags=[entity_name])

    @router.get("/")
    async def list_entities(
        limit: int = Query(20, le=100),
        offset: int = 0,
        status: Optional[str] = None,
        sort: str = "-created_at",
        actor=Depends(get_current_actor),
    ):
        check_permission(actor, entity_name, "read")
        filter_doc = {}
        if status:
            filter_doc["status"] = status
        entities = await entity_cls.find_scoped(filter_doc).skip(offset).limit(limit).to_list()
        return [e.model_dump() for e in entities]

    @router.get("/{entity_id}")
    async def get_entity(entity_id: str, actor=Depends(get_current_actor)):
        check_permission(actor, entity_name, "read")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)
        return entity.model_dump()

    @router.post("/")
    async def create_entity(data: dict, actor=Depends(get_current_actor)):
        check_permission(actor, entity_name, "write")
        entity = entity_cls(org_id=current_org_id.get(), **data)
        created_messages = await entity.save_tracked(method="create")
        _fire_dispatch(created_messages)
        return entity.model_dump()

    @router.put("/{entity_id}")
    async def update_entity(entity_id: str, data: dict, actor=Depends(get_current_actor)):
        check_permission(actor, entity_name, "write")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)
        for key, value in data.items():
            if key not in ("id", "_id", "org_id", "version"):
                setattr(entity, key, value)
        created_messages = await entity.save_tracked()
        _fire_dispatch(created_messages)
        return entity.model_dump()

    @router.post("/{entity_id}/transition")
    async def transition_entity(
        entity_id: str,
        data: dict = {},
        actor=Depends(get_current_actor),
    ):
        check_permission(actor, entity_name, "write")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)
        to = data.get("to")
        reason = data.get("reason")
        if not to:
            raise HTTPException(400, "'to' state is required")
        entity.transition_to(to, reason)
        created_messages = await entity.save_tracked(method="transition")
        _fire_dispatch(created_messages)
        return entity.model_dump()

    # Register @exposed methods as additional routes
    for attr_name in dir(entity_cls):
        attr = getattr(entity_cls, attr_name, None)
        if attr and getattr(attr, "_exposed", False):
            method_name = attr._exposed_name
            _register_exposed_route(router, entity_cls, entity_name, method_name, attr)

    # Register capability-activated methods
    for cap_activation in getattr(entity_cls, "_activated_capabilities", []):
        cap_name = (
            cap_activation.capability
            if hasattr(cap_activation, "capability")
            else cap_activation.get("capability", "")
        )
        _register_capability_route(router, entity_cls, entity_name, cap_name, cap_activation)

    # Register generic evaluate-rules route (works without capability activation)
    _register_evaluate_rules_route(router, entity_cls, entity_name)

    # Register per-entity bulk endpoint
    _register_bulk_route(router, entity_name)

    # Register integration dispatch route
    _register_integration_route(router, entity_cls, entity_name)

    app.include_router(router)


def _register_exposed_route(router, entity_cls, entity_name, method_name, method):
    """Register an @exposed method as POST /api/{entities}/{id}/{method_name}"""

    @router.post(f"/{{entity_id}}/{method_name.replace('_', '-')}")
    async def exposed_method(
        entity_id: str, data: dict = {}, actor=Depends(get_current_actor)
    ):
        check_permission(actor, entity_name, "write")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)
        result = await method(entity, **data)
        return result


def _register_capability_route(router, entity_cls, entity_name, cap_name, activation):
    """Register a capability as POST /api/{entities}/{id}/{cap-name}?auto=true"""

    @router.post(f"/{{entity_id}}/{cap_name.replace('_', '-')}")
    async def capability_method(
        entity_id: str,
        auto: bool = False,
        data: dict = {},
        actor=Depends(get_current_actor),
    ):
        check_permission(actor, entity_name, "write")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)
        if auto:
            from kernel.capability.registry import get_capability

            capability_fn = get_capability(cap_name)
            config = (
                activation.config
                if hasattr(activation, "config")
                else activation.get("config", {})
            )
            result = await capability_fn(entity, config, entity.org_id)
            # If not needs_reasoning, apply the result and save
            if not result.get("needs_reasoning"):
                for field, value in result.get("result", {}).items():
                    setattr(entity, field, value)
                await entity.save_tracked(
                    method=cap_name,
                    method_metadata={"rule_evaluation": result.get("rule_evaluation")},
                )
            return result
        else:
            # Manual invocation — just apply provided data
            for field, value in data.items():
                setattr(entity, field, value)
            await entity.save_tracked(method=cap_name)
            return entity.model_dump()


def _register_evaluate_rules_route(router, entity_cls, entity_name: str):
    """Register POST /api/{entities}/{id}/evaluate-rules?capability=X&auto=true.

    Generic rules evaluation that works without explicit capability activation.
    Used for patterns like health scoring where rules exist but the entity
    doesn't have the capability formally activated.
    """

    @router.post("/{entity_id}/evaluate-rules")
    async def evaluate_rules_route(
        entity_id: str,
        capability: str = "auto_classify",
        auto: bool = False,
        actor=Depends(get_current_actor),
    ):
        check_permission(actor, entity_name, "write")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)

        if not auto:
            return {"error": "auto=true required"}

        from kernel.capability.registry import get_capability

        capability_fn = get_capability(capability)
        result = await capability_fn(entity, {}, entity.org_id)

        if not result.get("needs_reasoning"):
            for field, value in result.get("result", {}).items():
                setattr(entity, field, value)
            await entity.save_tracked(
                method=capability,
                method_metadata={
                    "rule_evaluation": result.get("rule_evaluation"),
                },
            )
        return result


def _register_integration_route(router, entity_cls, entity_name: str):
    """Register POST /api/{entities}/{id}/integration/{method} — adapter dispatch."""

    @router.post("/{entity_id}/integration/{method_name}")
    async def integration_method(
        entity_id: str,
        method_name: str,
        data: dict = {},
        actor=Depends(get_current_actor),
    ):
        """Execute an adapter method for this entity's integration."""
        check_permission(actor, entity_name, "write")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)

        system_type = data.get("system_type")
        if not system_type:
            raise HTTPException(400, "system_type is required")

        from kernel.integration.dispatch import execute_with_retry, get_adapter

        adapter = await get_adapter(system_type)
        result = await execute_with_retry(adapter, method_name, **data.get("params", {}))

        return {"status": "ok", "result": result}


def _register_bulk_route(router, entity_name: str):
    """Register POST /api/{entities}/bulk — starts BulkExecuteWorkflow."""
    from uuid import uuid4

    @router.post("/bulk")
    async def start_bulk(spec: dict, actor=Depends(get_current_actor)):
        check_permission(actor, entity_name, "write")
        spec["entity_type"] = entity_name
        from kernel.temporal.client import get_temporal_client
        from kernel.temporal.workflows import BulkExecuteWorkflow

        client = await get_temporal_client()
        if not client:
            raise HTTPException(503, "Temporal not available")
        workflow_id = f"bulk-{uuid4().hex[:12]}"
        await client.start_workflow(
            BulkExecuteWorkflow.run,
            args=[spec],
            id=workflow_id,
            task_queue="indemn-kernel",
        )
        return {"workflow_id": workflow_id, "status": "started"}
