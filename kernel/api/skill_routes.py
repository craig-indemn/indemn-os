"""Skill CRUD + approval workflow API.

Skills are markdown documents: entity skills (auto-generated) or associate
skills (authored). Full CRUD plus review lifecycle.

Entity-skill freshness (Bug — stale entity-skill rendering): improvements
to `kernel/skill/generator.py` (filter recipes, JSON-shape examples,
entity_resolve sections, etc.) only reached entity skills whose definitions
were touched after the deploy. Older entities served pre-improvement
content forever. Fix: at GET time we re-render entity skills from the
CURRENT EntityDefinition + current generator. The stored copy is treated
as a cache, not source of truth — for entity skills, the EntityDefinition
IS the source of truth and the generator IS the rendering function. Every
generator improvement now propagates to every entity immediately.

Associate skills are still authored content with tamper detection — the
content_hash check applies to them.
"""

from fastapi import APIRouter, Depends, HTTPException

from kernel.api.serialize import to_dict
from kernel.auth.middleware import check_permission, get_current_actor
from kernel.context import current_org_id
from kernel.skill.integrity import compute_content_hash, verify_content_hash
from kernel.skill.schema import Skill

skill_router = APIRouter(prefix="/api/skills", tags=["skills"])


async def _refresh_entity_skill(skill: Skill) -> Skill:
    """For entity skills, re-render content from the current EntityDefinition
    + current generator before serving. Mutates the in-memory Skill object
    (does NOT persist) so callers can serialize a fresh copy without an
    extra DB write on every read.

    For associate skills (authored content), returns the skill unchanged.
    For entity skills whose EntityDefinition has been deleted, also returns
    unchanged so the stored fallback content still reaches the caller —
    this is the only path that surfaces the stored copy.
    """
    if skill.type != "entity":
        return skill
    from kernel.entity.definition import EntityDefinition
    from kernel.skill.generator import generate_entity_skill

    defn = await EntityDefinition.find_one(
        {"name": skill.name, "org_id": skill.org_id}
    )
    if defn is None:
        return skill
    fresh = generate_entity_skill(skill.name, defn)
    if fresh != skill.content:
        skill.content = fresh
        skill.content_hash = compute_content_hash(fresh)
    return skill


@skill_router.get("/")
async def list_skills(
    type: str = None,
    status: str = "active",
    actor=Depends(get_current_actor),
):
    """List skills for the current org. Entity-skill content is regenerated
    from the current EntityDefinition so the listing never serves stale
    auto-generated content."""
    filter_doc = {"org_id": current_org_id.get()}
    if type:
        filter_doc["type"] = type
    if status:
        filter_doc["status"] = status
    skills = await Skill.find(filter_doc).to_list()
    fresh = [await _refresh_entity_skill(s) for s in skills]
    return [to_dict(s) for s in fresh]


@skill_router.get("/{skill_id}")
async def get_skill(skill_id: str, actor=Depends(get_current_actor)):
    """Get a skill by ID. Entity skills re-render from current EntityDefinition;
    associate skills serve stored content with tamper-detection."""
    from bson import ObjectId

    skill = await Skill.find_one({"_id": ObjectId(skill_id), "org_id": current_org_id.get()})
    if not skill:
        raise HTTPException(404, "Skill not found")
    skill = await _refresh_entity_skill(skill)
    if skill.type == "associate" and not verify_content_hash(skill.content, skill.content_hash):
        raise HTTPException(
            status_code=409,
            detail=f"Skill '{skill.name}' content hash mismatch",
        )
    return to_dict(skill)


@skill_router.get("/by-name/{name}")
async def get_skill_by_name(name: str, actor=Depends(get_current_actor)):
    """Get a skill by name (the path `indemn skill get <Name>` hits).

    Entity skills regenerate from the current EntityDefinition every time —
    so any improvement to the skill generator (filter recipes, JSON examples,
    entity_resolve sections, etc.) is immediately visible across every entity
    without needing to touch each definition. Associate skills still serve
    stored content with tamper detection.
    """
    skill = await Skill.find_one({"name": name, "org_id": current_org_id.get(), "status": "active"})
    if not skill:
        raise HTTPException(404, f"Skill '{name}' not found")
    skill = await _refresh_entity_skill(skill)
    if skill.type == "associate" and not verify_content_hash(skill.content, skill.content_hash):
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
