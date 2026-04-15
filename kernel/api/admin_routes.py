"""Admin routes — entity definitions, seed, org management.

Provides controlled access to infrastructure entities that are
excluded from auto-generated CRUD for security.
"""

from fastapi import APIRouter, Depends, HTTPException

from kernel.auth.middleware import check_permission, get_current_actor
from kernel.context import current_org_id
from kernel.entity.definition import EntityDefinition

admin_router = APIRouter(tags=["admin"])


# --- Entity Definitions ---


@admin_router.get("/api/entitydefinitions")
async def list_entity_definitions(actor=Depends(get_current_actor)):
    """List entity definitions for the current org."""
    defs = await EntityDefinition.find({"org_id": current_org_id.get()}).to_list()
    return [d.model_dump(mode="json") for d in defs]


@admin_router.post("/api/entitydefinitions")
async def create_entity_definition(data: dict, actor=Depends(get_current_actor)):
    """Create an entity definition."""
    check_permission(actor, "EntityDefinition", "write")
    defn = EntityDefinition(org_id=current_org_id.get(), **data)
    await defn.insert()
    return defn.model_dump(mode="json")


# --- Platform Seed ---


@admin_router.post("/api/_platform/seed")
async def platform_seed(data: dict = {}):
    """Load seed data from the configured seed directory."""
    from kernel.seed import load_seed_data

    seed_dir = data.get("seed_dir", "seed")
    await load_seed_data(seed_dir)
    return {"status": "seeded", "seed_dir": seed_dir}


# --- Org Management ---


@admin_router.post("/api/_platform/org/clone")
async def org_clone(data: dict, actor=Depends(get_current_actor)):
    """Clone an organization's configuration to a new org."""
    source_org = data.get("source_org")
    target_name = data.get("target_name")
    if not source_org or not target_name:
        raise HTTPException(400, "source_org and target_name required")
    # Phase 4+ implementation
    return {"status": "not_implemented", "message": "Org clone available in Phase 4"}


@admin_router.get("/api/_platform/org/diff")
async def org_diff(source: str, target: str, actor=Depends(get_current_actor)):
    """Diff entity definitions between two orgs."""
    return {"status": "not_implemented", "message": "Org diff available in Phase 4"}


@admin_router.get("/api/_platform/org/export")
async def org_export(org_id: str, actor=Depends(get_current_actor)):
    """Export an org's entity definitions and configuration."""
    return {"status": "not_implemented", "message": "Org export available in Phase 4"}


@admin_router.post("/api/_platform/org/import")
async def org_import(data: dict, actor=Depends(get_current_actor)):
    """Import entity definitions into an org."""
    return {"status": "not_implemented", "message": "Org import available in Phase 4"}


@admin_router.post("/api/_platform/org/deploy")
async def org_deploy(data: dict, actor=Depends(get_current_actor)):
    """Deploy configuration changes to an org."""
    return {"status": "not_implemented", "message": "Org deploy available in Phase 4"}
