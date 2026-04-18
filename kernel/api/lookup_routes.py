"""Lookup management API — CRUD + import.

Provides controlled access to lookup tables used by the rules engine.
"""

from fastapi import APIRouter, Depends, HTTPException

from kernel.api.serialize import to_dict
from kernel.auth.middleware import check_permission, get_current_actor
from kernel.context import current_org_id
from kernel.rule.lookup import Lookup

lookup_router = APIRouter(prefix="/api/lookups", tags=["lookups"])


@lookup_router.get("/")
async def list_lookups(actor=Depends(get_current_actor)):
    """List all lookups for the current org."""
    lookups = await Lookup.find({"org_id": current_org_id.get()}).to_list()
    return [to_dict(lk) for lk in lookups]


@lookup_router.get("/{name}")
async def get_lookup(name: str, actor=Depends(get_current_actor)):
    """Get a lookup by name."""
    lookup = await Lookup.find_one({"name": name, "org_id": current_org_id.get()})
    if not lookup:
        raise HTTPException(404, f"Lookup '{name}' not found")
    return to_dict(lookup)


@lookup_router.post("/")
async def create_or_update_lookup(data: dict, actor=Depends(get_current_actor)):
    """Create or update a lookup table."""
    check_permission(actor, "Lookup", "write")
    name = data.get("name")
    if not name:
        raise HTTPException(400, "name is required")

    org_id = current_org_id.get()
    existing = await Lookup.find_one({"name": name, "org_id": org_id})

    if existing:
        existing.data = data.get("data", {})
        await existing.save()
        return {"status": "updated", "name": name}
    else:
        lookup = Lookup(
            org_id=org_id, name=name, data=data.get("data", {}),
            created_by=str(actor.id),
        )
        await lookup.insert()
        return {"status": "created", "name": name}
