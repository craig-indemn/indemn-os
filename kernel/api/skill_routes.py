"""Skill approval workflow API — submit, approve, reject.

Skills go through a review lifecycle: active → pending_review → active/deprecated.
"""

from fastapi import APIRouter, Depends, HTTPException

from kernel.auth.middleware import check_permission, get_current_actor
from kernel.skill.schema import Skill

skill_router = APIRouter(prefix="/api/skills", tags=["skills"])


@skill_router.post("/{skill_id}/submit-for-review")
async def submit_skill_for_review(skill_id: str, actor=Depends(get_current_actor)):
    """Submit a skill update for review. Transitions status to pending_review."""
    skill = await Skill.get_scoped(skill_id)
    if not skill:
        raise HTTPException(404)
    skill.status = "pending_review"
    await skill.save_tracked(actor_id=str(actor.id), method="submit_for_review")
    return {"status": "pending_review"}


@skill_router.post("/{skill_id}/approve")
async def approve_skill(skill_id: str, actor=Depends(get_current_actor)):
    """Approve a skill. Requires admin or skill-approver role."""
    check_permission(actor, "Skill", "write")
    skill = await Skill.get_scoped(skill_id)
    if not skill or skill.status != "pending_review":
        raise HTTPException(400, "Skill not pending review")
    skill.status = "active"
    await skill.save_tracked(actor_id=str(actor.id), method="approve")
    return {"status": "active"}
