"""Auto-register API routes from entity definitions.

Every entity type gets CRUD + transition + @exposed methods + capability routes.
This is the self-evidence property: define an entity, its API exists.

Bug #29 (os-bugs-and-shakeout): replacing an entity definition (modify field
types, change enum values, etc.) used to leave the OLD route closures in
`app.router.routes`. FastAPI matches the first registered route, so write
operations kept validating against the stale class — silent correctness
bug. The fix: `_evict_routes_for_prefix()` removes any route whose path
matches `/api/{slug}` or starts with `/api/{slug}/` before
`app.include_router()` re-adds the new ones. This makes live
entity-definition iteration safe without a process restart.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from kernel.api.serialize import to_dict
from kernel.auth.middleware import check_permission, get_current_actor
from kernel.context import current_org_id
from kernel.db import ENTITY_REGISTRY


def _evict_routes_for_prefix(app, prefix: str) -> int:
    """Remove all routes from `app.router.routes` whose path matches `prefix`
    or starts with `prefix + "/"`. Returns the number of evicted routes.

    `prefix` is the unslashed entity prefix, e.g. `/api/companys`. The
    matched routes include `/api/companys/`, `/api/companys/{entity_id}`,
    `/api/companys/bulk`, etc. Other prefixes (`/api/companies` — different
    entity, or `/api/companys2` — different entity that happens to start
    with the same letters) are NOT matched because the check requires a
    `/` separator after the prefix.
    """
    suffix_marker = prefix + "/"
    kept = []
    evicted = 0
    for r in app.router.routes:
        path = getattr(r, "path", None)
        if path is not None and (path == prefix or path.startswith(suffix_marker)):
            evicted += 1
            continue
        kept.append(r)
    app.router.routes = kept
    return evicted


def _fire_dispatch(created_messages):
    """Fire-and-forget optimistic dispatch after save_tracked commits."""
    if created_messages:
        from kernel.message.dispatch import optimistic_dispatch

        asyncio.create_task(optimistic_dispatch(created_messages))


async def _resolve_relationship_dict_inputs(
    entity_cls, entity_name: str, data: dict
) -> dict:
    """Reject (or auto-resolve) dict-shaped values for relationship fields (Bug #9).

    LLMs routinely pass `{"company": {"name": "Acme"}}` for an ObjectId-typed
    relationship field instead of the canonical `{"company": "<24-char hex>"}`.
    Pre-fix the kernel raised a Pydantic `is_instance_of` error and the
    associate's message dead-lettered. This helper catches that case at the
    API boundary so the caller (CLI / harness / UI) gets a 400 with a shape
    hint and can self-correct, rather than the message disappearing into the
    dead-letter queue.

    Two paths:

    - **Default (no opt-in):** raise HTTPException 400 with the shape hint
      and a pointer to `entity-resolve` so the caller can look up the
      relationship target's _id.

    - **Opt-in via `auto_resolve=true` on the FieldDefinition:** call the
      target entity's `entity_resolve` capability with the dict as the
      candidate. Auto-link only when there's exactly ONE candidate at score
      1.0 — anything else (zero, multiple at 1.0, or only fuzzy matches) is
      surfaced as 400 so ambiguity is never silently picked. This preserves
      Bug #31's "never auto-pick" contract on entity_resolve.

    Idempotent: non-dict values pass through unchanged. Fields that don't
    appear in `data` are skipped.
    """
    relationship_targets = getattr(entity_cls, "_relationship_targets", {}) or {}
    auto_resolve_fields = getattr(entity_cls, "_auto_resolve_fields", set()) or set()

    for field_name, target_name in relationship_targets.items():
        if field_name not in data:
            continue
        value = data[field_name]
        if not isinstance(value, dict):
            continue  # String hex, ObjectId, None, etc. handled elsewhere

        if field_name not in auto_resolve_fields:
            raise HTTPException(
                400,
                f"Field '{field_name}' on {entity_name} is a relationship to "
                f"{target_name!r} — pass the target entity's `_id` (24-char hex "
                f"string), not a dict. Got: {value!r}. Resolve the target first "
                f"via `indemn {target_name.lower()} list --filter '...'` or "
                f"`indemn {target_name.lower()} entity-resolve --data "
                f"'{{\"candidate\": ...}}'`, then pass the returned _id.",
            )

        # auto_resolve path: dispatch to entity_resolve capability on the target.
        target_cls = ENTITY_REGISTRY.get(target_name)
        if target_cls is None:
            raise HTTPException(
                400,
                f"Field '{field_name}' has auto_resolve=true but its target "
                f"entity type {target_name!r} is not registered in this org. "
                f"Pass _id directly or register the target entity first.",
            )
        # Find entity_resolve config on the target's activated capabilities.
        cap_config = None
        for cap in getattr(target_cls, "_activated_capabilities", []) or []:
            cap_name = cap.capability if hasattr(cap, "capability") else cap.get("capability")
            if cap_name == "entity_resolve":
                cap_config = cap.config if hasattr(cap, "config") else cap.get("config")
                break
        if cap_config is None:
            raise HTTPException(
                400,
                f"Field '{field_name}' has auto_resolve=true but {target_name!r} "
                f"does not have entity_resolve activated. Activate it via "
                f"`indemn entity enable {target_name} entity_resolve --config "
                f"'{{\"strategies\": [...]}}'` or pass _id directly.",
            )
        from kernel.capability.registry import get_capability

        resolve_fn = get_capability("entity_resolve")
        result = await resolve_fn(
            target_cls,
            cap_config,
            current_org_id.get(),
            params={"candidate": value, "limit": 5},
        )
        candidates = result.get("candidates", []) or []
        exact = [c for c in candidates if c.get("score") == 1.0]

        if len(exact) == 1:
            from bson import ObjectId as _OId

            data[field_name] = _OId(exact[0]["_id"])
            continue

        # Surface ambiguity / no-match honestly — Bug #31 contract.
        if not candidates:
            raise HTTPException(
                400,
                f"Field '{field_name}': auto_resolve found no {target_name!r} "
                f"matches for {value!r}. Pass _id directly, or create the "
                f"{target_name} entity first.",
            )
        if len(exact) > 1:
            ambiguous = [
                {"_id": c["_id"], "summary": c.get("summary", {})} for c in exact
            ]
            raise HTTPException(
                400,
                f"Field '{field_name}': auto_resolve found {len(exact)} "
                f"{target_name!r} candidates at score 1.0 — ambiguous, refusing "
                f"to pick. Pass _id directly. Candidates: {ambiguous}",
            )
        # Only fuzzy matches.
        fuzzy_top = [
            {
                "_id": c["_id"],
                "score": c["score"],
                "summary": c.get("summary", {}),
            }
            for c in candidates[:3]
        ]
        raise HTTPException(
            400,
            f"Field '{field_name}': auto_resolve found {len(candidates)} fuzzy "
            f"{target_name!r} candidates (no score 1.0) for {value!r}. "
            f"Pass _id directly. Top candidates: {fuzzy_top}",
        )

    return data


def _coerce_objectid_fields(entity_cls, data: dict) -> dict:
    """Convert string values to ObjectId for fields typed as objectid.

    JSON payloads carry ObjectId values as strings. The entity model expects
    bson.ObjectId instances. Inspects Pydantic field annotations to find
    ObjectId-typed fields (including Optional[ObjectId] and list[ObjectId]).
    """
    import typing

    from bson import ObjectId as OId

    for field_name, field_info in entity_cls.model_fields.items():
        if field_name not in data:
            continue
        annotation = field_info.annotation
        # Unwrap Optional
        origin = getattr(annotation, "__origin__", None)
        args = getattr(annotation, "__args__", ())
        if origin is typing.Union and type(None) in args:
            annotation = next(a for a in args if a is not type(None))
            origin = getattr(annotation, "__origin__", None)
            args = getattr(annotation, "__args__", ())

        if annotation is OId:
            if isinstance(data[field_name], str):
                data[field_name] = OId(data[field_name])
        elif origin is list and args and args[0] is OId:
            data[field_name] = [OId(v) if isinstance(v, str) else v for v in data[field_name]]
    return data


def _parse_list_filter(entity_cls, entity_name: str, filter_json: str) -> dict:
    """Thin wrapper around the shared filter safelist for the list endpoint.

    Kept as a named function for back-compat with existing tests + import
    sites; the actual parsing logic lives in `kernel/api/_filter_safelist.py`
    and is shared with the per-entity bulk route.
    """
    from kernel.api._filter_safelist import parse_filter

    return parse_filter(entity_cls, entity_name, filter_json)


def register_entity_routes(app, entity_name: str, entity_cls: type):
    """Register CRUD + transition + @exposed method + capability routes."""
    slug = entity_name.lower() + "s"
    router = APIRouter(prefix=f"/api/{slug}", tags=[entity_name])

    @router.get("/")
    async def list_entities(
        limit: int = Query(20, le=100),
        offset: int = 0,
        status: Optional[str] = None,
        search: Optional[str] = None,
        filter: Optional[str] = Query(
            None,
            description=(
                'JSON object filtering by entity fields, e.g. '
                '{"company":"69eb95f2...","status":"classified"}. '
                "Equality match only; ObjectId fields auto-coerce hex strings."
            ),
        ),
        sort: str = "-created_at",
        actor=Depends(get_current_actor),
    ):
        check_permission(actor, entity_name, "read")
        filter_doc = {}
        if status:
            # Resolve the actual state field name (e.g., "stage" for Company)
            state_field = getattr(entity_cls, "_state_field_name", None) or "status"
            filter_doc[state_field] = status
        if search:
            # Search by name or title field (case-insensitive regex)
            import re

            pattern = re.escape(search)
            filter_doc["$or"] = [
                {"name": {"$regex": pattern, "$options": "i"}},
                {"title": {"$regex": pattern, "$options": "i"}},
            ]
        if filter:
            # Per-field equality filter — validates field names against the
            # entity definition + coerces ObjectId hex strings.
            user_filter = _parse_list_filter(entity_cls, entity_name, filter)
            for field_name, value in user_filter.items():
                # Don't let `filter` clobber `status`/`search`-derived clauses;
                # if a caller passes both, the dedicated params take precedence
                # (callers should use one or the other, not both).
                if field_name not in filter_doc:
                    filter_doc[field_name] = value
        entities = await entity_cls.find_scoped(filter_doc).skip(offset).limit(limit).to_list()
        return [to_dict(e) for e in entities]

    @router.get("/{entity_id}")
    async def get_entity(
        entity_id: str,
        depth: int = Query(1, ge=1, le=5),
        include_related: bool = Query(False),
        actor=Depends(get_current_actor),
    ):
        check_permission(actor, entity_name, "read")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)
        result = to_dict(entity)

        # Resolve related entities per depth parameter
        if include_related and depth >= 2:
            from kernel.message.emit import _build_context

            context = await _build_context(entity, depth=depth, session=None)
            result["_related"] = context.get("related_entities", [])

        return result

    @router.post("/")
    async def create_entity(data: dict, actor=Depends(get_current_actor)):
        check_permission(actor, entity_name, "write")
        data = await _resolve_relationship_dict_inputs(entity_cls, entity_name, data)
        data = _coerce_objectid_fields(entity_cls, data)
        entity = entity_cls(org_id=current_org_id.get(), **data)
        created_messages = await entity.save_tracked(method="create")
        _fire_dispatch(created_messages)
        result = to_dict(entity)

        # Post-creation hook: generate setup token for human actors [U-01]
        if entity_name == "Actor" and data.get("type") == "human":
            from kernel.auth.jwt import generate_magic_link_token

            setup_token = generate_magic_link_token(entity, purpose="setup")
            result["setup_token"] = setup_token

        return result

    @router.put("/{entity_id}")
    async def update_entity(entity_id: str, data: dict, actor=Depends(get_current_actor)):
        check_permission(actor, entity_name, "write")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)
        # Reject state field changes — must go through /transition endpoint
        state_field = getattr(entity_cls, "_state_field_name", None) or "status"
        if getattr(entity_cls, "_state_machine", None) and state_field in data:
            raise HTTPException(
                400,
                f"Cannot set '{state_field}' via update. "
                f"Use POST /{slug}/{{id}}/transition instead.",
            )
        data = await _resolve_relationship_dict_inputs(entity_cls, entity_name, data)
        data = _coerce_objectid_fields(entity_cls, data)
        for key, value in data.items():
            if key not in ("id", "_id", "org_id", "version"):
                setattr(entity, key, value)
        created_messages = await entity.save_tracked()
        _fire_dispatch(created_messages)
        return to_dict(entity)

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
        return to_dict(entity)

    @router.post("/{entity_id}/reprocess")
    async def reprocess_entity(
        entity_id: str,
        data: dict = {},
        actor=Depends(get_current_actor),
    ):
        """Re-emit a message for an existing entity to one role's queue.

        Bug #10: when a watch is added to a role, only future entity changes
        fire it. This endpoint backfills against existing entities — operator
        names the role, the kernel emits one message scoped to that role's
        watch. The receiving actor sees the same shape it would have seen
        organically (same context_depth, same scope resolution), with
        event_metadata.reprocess=true marking it as a backfill.

        Body: {"role": "<role_name>", "event_type": "created" (default)}.
        Read permission is sufficient — reprocess doesn't mutate the entity,
        it just emits a message. Mutating the entity is the receiving
        actor's job and is gated by THAT role's write permissions.
        """
        check_permission(actor, entity_name, "read")
        entity = await entity_cls.get_scoped(entity_id)
        if not entity:
            raise HTTPException(404)
        role_name = data.get("role")
        if not role_name:
            raise HTTPException(400, "'role' is required (the role whose watch should fire)")
        event_type = data.get("event_type", "created")

        from kernel.message.reprocess import ReprocessError, reprocess_for_role

        try:
            message = await reprocess_for_role(entity, role_name, event_type)
        except ReprocessError as e:
            raise HTTPException(400, str(e))

        _fire_dispatch([message])
        return {
            "message_id": str(message.id),
            "entity_type": entity_name,
            "entity_id": str(entity.id),
            "role": role_name,
            "event_type": event_type,
            "correlation_id": message.correlation_id,
            "causation_id": message.causation_id,
        }

    # Register @exposed methods as additional routes
    for attr_name in dir(entity_cls):
        if attr_name.startswith("_"):
            continue
        attr = getattr(entity_cls, attr_name, None)
        if attr and getattr(attr, "_exposed", False):
            method_name = attr._exposed_name
            _register_exposed_route(router, entity_cls, entity_name, method_name, attr)

    # Register capability-activated methods
    from kernel.capability import COLLECTION_LEVEL_CAPABILITIES as _COLLECTION_LEVEL_CAPABILITIES
    for cap_activation in getattr(entity_cls, "_activated_capabilities", []):
        cap_name = (
            cap_activation.capability
            if hasattr(cap_activation, "capability")
            else cap_activation.get("capability", "")
        )
        if cap_name not in _COLLECTION_LEVEL_CAPABILITIES:
            _register_capability_route(router, entity_cls, entity_name, cap_name, cap_activation)

    # Register collection-level capabilities (no entity_id — creates entities)
    for cap_activation in getattr(entity_cls, "_activated_capabilities", []):
        cap_name = (
            cap_activation.capability
            if hasattr(cap_activation, "capability")
            else cap_activation.get("capability", "")
        )
        if cap_name in _COLLECTION_LEVEL_CAPABILITIES:
            _register_collection_capability_route(
                router, entity_cls, entity_name, cap_name, cap_activation
            )

    # Register generic evaluate-rules route (works without capability activation)
    _register_evaluate_rules_route(router, entity_cls, entity_name)

    # Register per-entity bulk endpoint
    _register_bulk_route(router, entity_name)

    # Register integration dispatch route
    _register_integration_route(router, entity_cls, entity_name)

    # Bug #29: evict stale routes from a prior registration of this entity
    # before include_router (which appends, not replaces). Without this,
    # write operations keep validating against the stale dynamic class.
    _evict_routes_for_prefix(app, f"/api/{slug}")
    app.include_router(router)


def _register_exposed_route(router, entity_cls, entity_name, method_name, method):
    """Register an @exposed method as POST /api/{entities}/{id}/{method_name}"""

    @router.post(f"/{{entity_id}}/{method_name.replace('_', '-')}")
    async def exposed_method(entity_id: str, data: dict = {}, actor=Depends(get_current_actor)):
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
                activation.config if hasattr(activation, "config") else activation.get("config", {})
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
            return to_dict(entity)


def _register_collection_capability_route(router, entity_cls, entity_name, cap_name, activation):
    """Register collection-level capability: POST /api/{entities}/{cap-name} (no entity_id).

    For capabilities like fetch_new that create entities rather than operating on existing ones.
    FastAPI matches fixed paths before parameterized paths on the same router.
    """

    @router.post(f"/{cap_name.replace('_', '-')}")
    async def collection_capability(data: dict = {}, actor=Depends(get_current_actor)):
        check_permission(actor, entity_name, "write")
        from kernel.capability.registry import get_capability

        capability_fn = get_capability(cap_name)
        config = (
            activation.config if hasattr(activation, "config") else activation.get("config", {})
        )
        result = await capability_fn(entity_cls, config, current_org_id.get(), params=data)
        return result


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
        spec["org_id"] = str(current_org_id.get())

        # Validate filter_query at the API boundary so callers get 400 with
        # field-level error detail BEFORE the workflow is even started, instead
        # of an opaque workflow failure to chase down. The activity re-runs
        # parse_filter with the raw dict to produce typed values for MongoDB
        # — bson.ObjectId / datetime don't cross the Temporal boundary cleanly,
        # so we keep the typed coercion out of workflow input. (Bug #23.)
        if spec.get("filter_query") is not None:
            from kernel.api._filter_safelist import parse_filter

            entity_cls = ENTITY_REGISTRY.get(entity_name)
            if entity_cls is not None:
                parse_filter(entity_cls, entity_name, spec["filter_query"])

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
