"""Rule CRUD API — create, list, update, delete rules and lookups.

Rules are per-org condition→action patterns for deterministic entity processing.
They're in _INFRASTRUCTURE (excluded from auto-generation) so need explicit routes.
"""

from fastapi import APIRouter, Depends, HTTPException

from kernel.api.serialize import to_dict
from kernel.auth.middleware import check_permission, get_current_actor
from kernel.context import current_org_id
from kernel.rule.schema import Rule
from kernel.rule.validation import validate_rule

rule_router = APIRouter(prefix="/api/rules", tags=["rules"])


@rule_router.get("/")
async def list_rules(
    entity_type: str = None,
    capability: str = None,
    status: str = "active",
    actor=Depends(get_current_actor),
):
    """List rules for the current org with optional filters."""
    filter_doc = {"org_id": current_org_id.get()}
    if entity_type:
        filter_doc["entity_type"] = entity_type
    if capability:
        filter_doc["capability"] = capability
    if status:
        filter_doc["status"] = status
    rules = await Rule.find(filter_doc).sort("-priority").to_list()
    return [to_dict(r) for r in rules]


@rule_router.get("/{rule_id}")
async def get_rule(rule_id: str, actor=Depends(get_current_actor)):
    """Get a rule by ID."""
    from bson import ObjectId

    rule = await Rule.find_one({"_id": ObjectId(rule_id), "org_id": current_org_id.get()})
    if not rule:
        raise HTTPException(404, "Rule not found")
    return to_dict(rule)


@rule_router.post("/")
async def create_rule(data: dict, actor=Depends(get_current_actor)):
    """Create a new rule."""
    check_permission(actor, "Rule", "write")

    entity_type = data.get("entity_type")
    capability = data.get("capability")
    action = data.get("action")
    conditions = data.get("conditions")

    if not entity_type:
        raise HTTPException(400, "entity_type is required")
    if not capability:
        raise HTTPException(400, "capability is required")
    if not action or action not in ("set_fields", "force_reasoning"):
        raise HTTPException(400, "action must be 'set_fields' or 'force_reasoning'")
    if not conditions:
        raise HTTPException(400, "conditions is required")

    rule = Rule(
        org_id=current_org_id.get(),
        entity_type=entity_type,
        capability=capability,
        name=data.get("name"),
        conditions=conditions,
        action=action,
        sets=data.get("sets"),
        forces_reasoning_reason=data.get("forces_reasoning_reason"),
        priority=data.get("priority", 100),
        status=data.get("status", "active"),
        created_by=str(actor.id),
    )

    # Validate rule before persisting
    errors = await validate_rule(rule)
    hard_errors = [e for e in errors if not e.startswith("WARNING:")]
    if hard_errors:
        raise HTTPException(422, detail=hard_errors)

    await rule.insert()
    return to_dict(rule)


@rule_router.put("/{rule_id}")
async def update_rule(rule_id: str, data: dict, actor=Depends(get_current_actor)):
    """Update a rule."""
    check_permission(actor, "Rule", "write")
    from bson import ObjectId

    rule = await Rule.find_one({"_id": ObjectId(rule_id), "org_id": current_org_id.get()})
    if not rule:
        raise HTTPException(404, "Rule not found")

    for field in (
        "name",
        "conditions",
        "action",
        "sets",
        "forces_reasoning_reason",
        "priority",
        "status",
    ):
        if field in data:
            setattr(rule, field, data[field])

    await rule.save()
    return to_dict(rule)


@rule_router.delete("/{rule_id}")
async def archive_rule(rule_id: str, actor=Depends(get_current_actor)):
    """Archive a rule (soft delete)."""
    check_permission(actor, "Rule", "write")
    from bson import ObjectId

    rule = await Rule.find_one({"_id": ObjectId(rule_id), "org_id": current_org_id.get()})
    if not rule:
        raise HTTPException(404, "Rule not found")
    rule.status = "archived"
    await rule.save()
    return {"status": "archived", "id": str(rule.id)}
