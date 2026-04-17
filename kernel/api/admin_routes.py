"""Admin routes — entity definitions, seed, org management.

Provides controlled access to infrastructure entities that are
excluded from auto-generated CRUD for security.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from kernel.api.serialize import to_dict
from kernel.auth.middleware import check_permission, get_current_actor
from kernel.context import current_org_id
from kernel.entity.definition import CapabilityActivation, EntityDefinition, FieldDefinition

admin_router = APIRouter(tags=["admin"])


# --- Entity Definitions ---


@admin_router.get("/api/entitydefinitions")
async def list_entity_definitions(actor=Depends(get_current_actor)):
    """List entity definitions for the current org."""
    defs = await EntityDefinition.find({"org_id": current_org_id.get()}).to_list()
    return [to_dict(d) for d in defs]


@admin_router.post("/api/entitydefinitions")
async def create_entity_definition(request: Request, data: dict, actor=Depends(get_current_actor)):
    """Create an entity definition and register it immediately."""
    check_permission(actor, "EntityDefinition", "write")
    defn = EntityDefinition(org_id=current_org_id.get(), **data)
    await defn.insert()

    # Register the entity class and API routes at runtime — no restart needed
    from kernel.db import register_domain_entity

    await register_domain_entity(defn, app=request.app)

    return to_dict(defn)


@admin_router.put("/api/entitydefinitions/{name}/enable-capability")
async def enable_capability(
    request: Request, name: str, data: dict, actor=Depends(get_current_actor),
):
    """Enable a kernel capability on an entity type.

    Body: {"capability": "stale_check", "config": {...}}
    """
    check_permission(actor, "EntityDefinition", "write")
    org_id = current_org_id.get()

    defn = await EntityDefinition.find_one({"name": name, "org_id": org_id})
    if not defn:
        raise HTTPException(404, f"Entity definition '{name}' not found")

    capability = data.get("capability")
    config = data.get("config", {})
    if not capability:
        raise HTTPException(400, "capability is required")

    # Check if already activated
    updated = False
    for cap in defn.activated_capabilities:
        if cap.capability == capability:
            cap.config = config
            updated = True
            break

    if not updated:
        defn.activated_capabilities.append(
            CapabilityActivation(capability=capability, config=config)
        )

    from datetime import datetime, timezone

    defn.updated_at = datetime.now(timezone.utc)
    defn.version += 1
    await defn.save()

    # Re-register entity class so capability routes are available immediately
    from kernel.db import register_domain_entity

    await register_domain_entity(defn, app=request.app)

    return {"status": "updated" if updated else "enabled", "capability": capability}


@admin_router.put("/api/entitydefinitions/{name}/modify")
async def modify_entity_definition(
    name: str, data: dict, actor=Depends(get_current_actor)
):
    """Modify an entity definition — add/remove fields.

    Body: {"add_fields": {"field_name": {...}}, "remove_fields": ["field_name"]}
    """
    check_permission(actor, "EntityDefinition", "write")
    org_id = current_org_id.get()

    defn = await EntityDefinition.find_one({"name": name, "org_id": org_id})
    if not defn:
        raise HTTPException(404, f"Entity definition '{name}' not found")

    added = []
    removed = []

    add_fields = data.get("add_fields", {})
    for field_name, field_spec in add_fields.items():
        defn.fields[field_name] = FieldDefinition(**field_spec)
        added.append(field_name)

    remove_fields = data.get("remove_fields", [])
    for field_name in remove_fields:
        if field_name in defn.fields:
            del defn.fields[field_name]
            removed.append(field_name)

    if added or removed:
        from datetime import datetime, timezone

        defn.updated_at = datetime.now(timezone.utc)
        defn.version += 1
        await defn.save()

    return {"status": "modified", "added": added, "removed": removed}


# --- Role Management ---


@admin_router.post("/api/_platform/role/add-watch")
async def role_add_watch(data: dict, actor=Depends(get_current_actor)):
    """Append a watch definition to an existing role by name."""
    from kernel_entities.role import Role, WatchDefinition

    role_name = data.get("role_name")
    watch_data = data.get("watch")
    if not role_name or not watch_data:
        raise HTTPException(400, "role_name and watch required")

    org_id = current_org_id.get()
    role = await Role.find_one({"name": role_name, "org_id": org_id})
    if not role:
        raise HTTPException(404, f"Role '{role_name}' not found")

    watch = WatchDefinition(**watch_data)
    role.watches.append(watch)
    await role.save_tracked(actor_id=str(actor.id), method="add_watch")

    return {
        "role": role_name,
        "watches_count": len(role.watches),
    }


# --- Actor Management ---


@admin_router.post("/api/_platform/actor/add-role")
async def actor_add_role(data: dict, actor=Depends(get_current_actor)):
    """Add a role to an actor by email. Resolves role name to ID."""
    from kernel_entities import Actor, Role

    email = data.get("email")
    role_name = data.get("role_name")
    if not email or not role_name:
        raise HTTPException(400, "email and role_name required")

    org_id = current_org_id.get()
    target = await Actor.find_one({"email": email, "org_id": org_id})
    if not target:
        raise HTTPException(404, f"Actor with email '{email}' not found")

    role = await Role.find_one({"name": role_name, "org_id": org_id})
    if not role:
        raise HTTPException(404, f"Role '{role_name}' not found")

    if role.id not in target.role_ids:
        target.role_ids.append(role.id)
        await target.save_tracked(actor_id=str(actor.id), method="add_role")

    return {"actor_id": str(target.id), "role_added": role_name}


@admin_router.post("/api/_platform/actor/add-auth")
async def actor_add_auth(data: dict, actor=Depends(get_current_actor)):
    """Add an authentication method to an actor by email."""
    from kernel_entities import Actor

    email = data.get("email")
    method = data.get("method")
    if not email or not method:
        raise HTTPException(400, "email and method required")

    target = await Actor.find_one({"email": email, "org_id": current_org_id.get()})
    if not target:
        raise HTTPException(404, f"Actor with email '{email}' not found")

    # Check not already added
    for existing in target.authentication_methods:
        if existing.get("method") == method:
            return {"actor_id": str(target.id), "status": "already_exists"}

    target.authentication_methods.append({"method": method, "enabled": True})
    await target.save_tracked(actor_id=str(actor.id), method="add_auth")

    return {"actor_id": str(target.id), "method_added": method}


# --- Service Tokens ---


@admin_router.post("/api/_platform/service-token")
async def create_service_token(data: dict, actor=Depends(get_current_actor)):
    """Create a service token for a Runtime's harness.

    Per G1.2: generates a long-lived opaque token, creates an associate Actor
    as the Runtime's service identity, stores the hashed token on the Actor,
    creates a Session (type=associate_service). Returns the raw token ONCE.
    """
    from bson import ObjectId

    from kernel.auth.session_manager import create_session
    from kernel.auth.token import generate_service_token, hash_token
    from kernel_entities.actor import Actor
    from kernel_entities.runtime import Runtime

    runtime_id = data.get("runtime_id")
    if not runtime_id:
        raise HTTPException(400, "runtime_id is required")

    runtime = await Runtime.get(ObjectId(runtime_id))
    if not runtime:
        raise HTTPException(404, f"Runtime {runtime_id} not found")

    # Create an associate Actor as the Runtime's service identity
    service_actor = Actor(
        org_id=runtime.org_id,
        name=f"runtime-service:{runtime.name}",
        type="associate",
        status="active",
        runtime_id=runtime.id,
        authentication_methods=[],
    )
    await service_actor.insert()

    # Generate service token, store hash on actor
    raw_token = generate_service_token()
    service_actor.authentication_methods.append({
        "type": "token",
        "token_hash": hash_token(raw_token),
        "usage": "associate_service",
    })
    await service_actor.save_tracked(
        actor_id=str(actor.id), method="create_service_token"
    )

    # Create a long-lived session (associate_service — no refresh, no expiry rotation)
    session, _jwt = await create_session(
        service_actor,
        auth_method="token",
        session_type="associate_service",
        expire_minutes=525960,  # ~1 year for dev; production uses token auth directly
    )

    return {
        "service_token": raw_token,
        "actor_id": str(service_actor.id),
        "runtime_id": runtime_id,
        "session_id": str(session.id),
        "note": "Store this token securely. It will not be shown again.",
    }


# --- Platform Seed ---


@admin_router.post("/api/_platform/seed")
async def platform_seed(data: dict = {}):
    """Load seed data from the configured seed directory into a target org."""
    from kernel.seed import load_seed_data

    org_id = data.get("org_id")
    if not org_id:
        raise HTTPException(400, "org_id is required")
    seed_dir = data.get("seed_dir", "seed")
    await load_seed_data(org_id=org_id, seed_dir=seed_dir)
    return {"status": "seeded", "seed_dir": seed_dir, "org_id": org_id}


# --- Org Management ---


@admin_router.post("/api/_platform/org/clone")
async def org_clone(data: dict, actor=Depends(get_current_actor)):
    """Clone an organization's configuration to a new org."""
    from kernel.api.org_lifecycle import clone_org

    source_org_slug = data.get("source_org_slug")
    target_name = data.get("target_org_name")
    if not source_org_slug or not target_name:
        raise HTTPException(400, "source_org_slug and target_org_name required")

    # Resolve slug to org_id
    source_org_id = await _resolve_org_slug(source_org_slug)
    result = await clone_org(source_org_id, target_name)
    return result


@admin_router.get("/api/_platform/org/diff")
async def org_diff(org_a: str = None, org_b: str = None, actor=Depends(get_current_actor)):
    """Diff configuration between two orgs."""
    from kernel.api.org_lifecycle import diff_org_configs

    if not org_a or not org_b:
        raise HTTPException(400, "org_a and org_b slug params required")
    org_a_id = await _resolve_org_slug(org_a)
    org_b_id = await _resolve_org_slug(org_b)
    return await diff_org_configs(org_a_id, org_b_id)


@admin_router.get("/api/_platform/org/export")
async def org_export(org: str = None, actor=Depends(get_current_actor)):
    """Export an org's configuration."""
    from kernel.api.org_lifecycle import export_org_config

    if not org:
        org_id = current_org_id.get()
    else:
        org_id = await _resolve_org_slug(org)
    return await export_org_config(org_id)


@admin_router.post("/api/_platform/org/import")
async def org_import(data: dict, actor=Depends(get_current_actor)):
    """Import configuration into a new org."""
    from kernel.api.org_lifecycle import import_org_config

    target_name = data.get("target_org_name")
    config = data.get("config")
    if not target_name or not config:
        raise HTTPException(400, "target_org_name and config required")
    return await import_org_config(target_name, config)


@admin_router.post("/api/_platform/org/deploy")
async def org_deploy(data: dict, actor=Depends(get_current_actor)):
    """Deploy configuration from source to target org."""
    from kernel.api.org_lifecycle import deploy_org

    source_slug = data.get("source_org_slug")
    target_slug = data.get("target_org_slug")
    dry_run = data.get("dry_run", True)
    if not source_slug or not target_slug:
        raise HTTPException(400, "source_org_slug and target_org_slug required")
    source_id = await _resolve_org_slug(source_slug)
    target_id = await _resolve_org_slug(target_slug)
    return await deploy_org(source_id, target_id, dry_run=dry_run)


async def _resolve_org_slug(slug: str):
    """Resolve an org slug to its ObjectId."""
    from bson import ObjectId as OId

    from kernel_entities.organization import Organization

    org = await Organization.find_one({"slug": slug})
    if not org:
        # Try as raw ObjectId
        try:
            return OId(slug)
        except Exception:
            raise HTTPException(404, f"Organization '{slug}' not found")
    return org.id


# --- Report Compare ---


@admin_router.post("/api/_platform/report/compare")
async def report_compare(data: dict, actor=Depends(get_current_actor)):
    """Compare old system data against OS entities for parallel run validation.

    Body: {
        "old_data": [{"external_id": "...", "classification": "..."}],
        "entity_type": "Email",
        "match_field": "external_id",
        "compare_fields": ["classification", "submission_id"]
    }
    """
    from kernel.db import ENTITY_REGISTRY

    old_data = data.get("old_data", [])
    entity_type = data.get("entity_type")
    match_field = data.get("match_field", "external_id")
    compare_fields = data.get("compare_fields", [])

    if not entity_type or not old_data:
        raise HTTPException(400, "entity_type and old_data required")

    entity_cls = ENTITY_REGISTRY.get(entity_type)
    if not entity_cls:
        raise HTTPException(404, f"Entity type '{entity_type}' not found")

    # Build lookup from old data
    old_by_key = {}
    for record in old_data:
        key = record.get(match_field)
        if key:
            old_by_key[key] = record

    # Load ALL OS entities for this type (to detect extras)
    os_by_key = {}
    all_entities = await entity_cls.find_scoped({}).to_list()
    for entity in all_entities:
        entity_data = entity.model_dump(by_alias=True)
        key = entity_data.get(match_field)
        if key:
            os_by_key[key] = entity_data

    # Compare
    comparisons = []
    matched = 0
    mismatched = 0
    missing_in_os = 0

    for key, old_record in old_by_key.items():
        os_record = os_by_key.get(key)
        if not os_record:
            comparisons.append({
                match_field: key,
                "status": "missing_in_os",
                "fields": {f: {"old": old_record.get(f)} for f in compare_fields},
            })
            missing_in_os += 1
            continue

        field_matches = {}
        all_match = True
        for field in compare_fields:
            old_val = old_record.get(field)
            os_val = os_record.get(field)
            if old_val is not None and os_val is not None:
                is_match = str(old_val) == str(os_val)
            else:
                is_match = old_val == os_val
            field_matches[field] = {
                "old": old_val,
                "new": os_val,
                "match": is_match,
            }
            if not is_match:
                all_match = False

        if all_match:
            matched += 1
        else:
            mismatched += 1

        comparisons.append({
            match_field: key,
            "status": "match" if all_match else "mismatch",
            "fields": field_matches,
        })

    # Detect entities in OS but not in old system
    extra_in_os = 0
    for key in os_by_key:
        if key not in old_by_key:
            extra_in_os += 1
            comparisons.append({
                match_field: key,
                "status": "extra_in_os",
            })

    return {
        "summary": {
            "total": len(old_by_key),
            "matched": matched,
            "mismatched": mismatched,
            "missing_in_os": missing_in_os,
            "extra_in_os": extra_in_os,
        },
        "comparisons": comparisons,
    }


# --- Pipeline Metrics ---


@admin_router.get("/api/metrics/state-distribution/{entity_name}")
async def get_state_distribution(entity_name: str, actor=Depends(get_current_actor)):
    """State distribution for an entity type."""
    from kernel.capability.aggregations import state_distribution
    from kernel.db import ENTITY_REGISTRY

    cls = ENTITY_REGISTRY.get(entity_name)
    if not cls:
        raise HTTPException(404, f"Entity type '{entity_name}' not found")
    return await state_distribution(cls, actor.org_id)


@admin_router.get("/api/metrics/queue-depth")
async def get_queue_depth(actor=Depends(get_current_actor)):
    """Pending message count per role."""
    from kernel.capability.aggregations import queue_depth

    return await queue_depth(actor.org_id)


# --- Audit ---


@admin_router.get("/api/_platform/audit/verify")
async def audit_verify(
    limit: int = 1000,
    org: str = None,
    entity_type: str = None,
    actor=Depends(get_current_actor),
):
    """Verify the changes collection hash chain integrity."""
    from kernel.changes.collection import ChangeRecord
    from kernel.changes.hash_chain import compute_hash

    filter_doc = {}
    if org:
        from bson import ObjectId

        filter_doc["org_id"] = ObjectId(org)
    if entity_type:
        filter_doc["entity_type"] = entity_type

    records = (
        await ChangeRecord.find(filter_doc)
        .sort([("timestamp", 1), ("_id", 1)])
        .limit(limit)
        .to_list()
    )

    if not records:
        return {"chain_valid": True, "records_checked": 0}

    for i, record in enumerate(records):
        if i == 0:
            continue  # First record has no previous hash to check
        expected_hash = compute_hash(records[i - 1])
        if record.previous_hash != expected_hash:
            return {
                "chain_valid": False,
                "records_checked": i + 1,
                "break_at": str(record.id),
                "expected_hash": expected_hash,
                "actual_hash": record.previous_hash,
            }

    return {"chain_valid": True, "records_checked": len(records)}
