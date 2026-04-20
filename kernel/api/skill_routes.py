"""Skill CRUD + approval workflow API.

Skills are markdown documents: entity skills (auto-generated) or associate
skills (authored). Full CRUD plus review lifecycle.
"""

from fastapi import APIRouter, Depends, HTTPException

from kernel.api.serialize import to_dict
from kernel.auth.middleware import check_permission, get_current_actor
from kernel.context import current_org_id
from kernel.skill.integrity import compute_content_hash, verify_content_hash
from kernel.skill.schema import Skill

skill_router = APIRouter(prefix="/api/skills", tags=["skills"])


@skill_router.get("/")
async def list_skills(
    type: str = None,
    status: str = "active",
    actor=Depends(get_current_actor),
):
    """List skills for the current org."""
    filter_doc = {"org_id": current_org_id.get()}
    if type:
        filter_doc["type"] = type
    if status:
        filter_doc["status"] = status
    skills = await Skill.find(filter_doc).to_list()
    return [to_dict(s) for s in skills]


@skill_router.get("/{skill_id}")
async def get_skill(skill_id: str, actor=Depends(get_current_actor)):
    """Get a skill by ID."""
    from bson import ObjectId

    skill = await Skill.find_one({"_id": ObjectId(skill_id), "org_id": current_org_id.get()})
    if not skill:
        raise HTTPException(404, "Skill not found")
    if not verify_content_hash(skill.content, skill.content_hash):
        raise HTTPException(
            status_code=409,
            detail=f"Skill '{skill.name}' content hash mismatch",
        )
    return to_dict(skill)


@skill_router.get("/by-name/{name}")
async def get_skill_by_name(name: str, actor=Depends(get_current_actor)):
    """Get a skill by name."""
    skill = await Skill.find_one({"name": name, "org_id": current_org_id.get(), "status": "active"})
    if not skill:
        raise HTTPException(404, f"Skill '{name}' not found")
    if not verify_content_hash(skill.content, skill.content_hash):
        raise HTTPException(
            status_code=409,
            detail=f"Skill '{skill.name}' content hash mismatch",
        )
    return to_dict(skill)


@skill_router.post("/")
async def create_skill(data: dict, actor=Depends(get_current_actor)):
    """Create a new skill (entity or associate)."""
    check_permission(actor, "Skill", "write")
    name = data.get("name")
    content = data.get("content", "")
    skill_type = data.get("type", "associate")

    if not name:
        raise HTTPException(400, "name is required")
    if not content:
        raise HTTPException(400, "content is required")

    org_id = current_org_id.get()

    # Check for duplicate name
    existing = await Skill.find_one(
        {"name": name, "org_id": org_id, "status": {"$ne": "deprecated"}}
    )
    if existing:
        raise HTTPException(409, f"Skill '{name}' already exists")

    skill = Skill(
        org_id=org_id,
        name=name,
        type=skill_type,
        entity_type=data.get("entity_type"),
        content=content,
        content_hash=compute_content_hash(content),
        created_by=str(actor.id),
    )
    await skill.insert()
    return to_dict(skill)


@skill_router.put("/{skill_id}")
async def update_skill(skill_id: str, data: dict, actor=Depends(get_current_actor)):
    """Update a skill's content. Increments version, recomputes hash."""
    check_permission(actor, "Skill", "write")
    from bson import ObjectId

    skill = await Skill.find_one({"_id": ObjectId(skill_id), "org_id": current_org_id.get()})
    if not skill:
        raise HTTPException(404, "Skill not found")

    content = data.get("content")
    if content:
        skill.content = content
        skill.content_hash = compute_content_hash(content)
        skill.version += 1

    if "name" in data:
        skill.name = data["name"]
    if "entity_type" in data:
        skill.entity_type = data["entity_type"]

    from datetime import datetime, timezone

    skill.updated_at = datetime.now(timezone.utc)
    await skill.save()
    return to_dict(skill)


@skill_router.post("/{skill_id}/submit-for-review")
async def submit_skill_for_review(skill_id: str, actor=Depends(get_current_actor)):
    """Submit a skill update for review. Transitions status to pending_review."""
    from bson import ObjectId

    skill = await Skill.find_one({"_id": ObjectId(skill_id), "org_id": current_org_id.get()})
    if not skill:
        raise HTTPException(404)
    skill.status = "pending_review"
    await skill.save()
    return {"status": "pending_review"}


@skill_router.post("/{skill_id}/approve")
async def approve_skill(skill_id: str, actor=Depends(get_current_actor)):
    """Approve a skill. Requires admin or skill-approver role."""
    check_permission(actor, "Skill", "write")
    from bson import ObjectId

    skill = await Skill.find_one({"_id": ObjectId(skill_id), "org_id": current_org_id.get()})
    if not skill or skill.status != "pending_review":
        raise HTTPException(400, "Skill not pending review")
    skill.status = "active"
    await skill.save()
    return {"status": "active"}


@skill_router.post("/{skill_id}/deprecate")
async def deprecate_skill(skill_id: str, actor=Depends(get_current_actor)):
    """Deprecate a skill."""
    check_permission(actor, "Skill", "write")
    from bson import ObjectId

    skill = await Skill.find_one({"_id": ObjectId(skill_id), "org_id": current_org_id.get()})
    if not skill:
        raise HTTPException(404)
    skill.status = "deprecated"
    await skill.save()
    return {"status": "deprecated"}
