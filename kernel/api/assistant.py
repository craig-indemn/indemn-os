"""Assistant API — streaming LLM endpoint. [G-59]

The default assistant runs with the user's own session JWT.
Every action is audited as "actor {user.id} via default_associate".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from kernel.auth.middleware import get_current_actor

assistant_router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class AssistantMessageRequest(BaseModel):
    content: str
    context: dict = {}


@assistant_router.post("/message")
async def assistant_message(
    data: AssistantMessageRequest,
    actor=Depends(get_current_actor),
):
    """Process an assistant message with streaming response. [G-59]

    The default assistant inherits the user's session JWT — same permissions,
    audit as 'actor via default_associate'.
    """
    # Load entity skills for the user's roles [G-59]
    skills = await _load_skills_for_roles(actor.role_ids)

    async def generate():
        import anthropic

        client = anthropic.AsyncAnthropic()

        # Context-aware system prompt with skills [G-59]
        system_prompt = (
            "You are the user's assistant in the Indemn OS. "
            "You can execute any CLI command the user has permission for "
            "via the same API (using the user's JWT). "
            f"The user is viewing: {data.context}.\n\n"
            f"Available operations:\n{skills}"
        )

        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": data.content}],
        ) as stream:
            async for text in stream.text_stream:
                yield text

    return StreamingResponse(generate(), media_type="text/plain")


async def _load_skills_for_roles(role_ids: list) -> str:
    """Load skill descriptions for the actor's roles. [G-59]

    Returns a formatted string of available operations.
    """
    from kernel.db import ENTITY_REGISTRY
    from kernel.skill.schema import Skill
    from kernel_entities.role import Role

    roles = await Role.find({"_id": {"$in": role_ids}}).to_list()

    # Gather entity operations the user can access
    operations = []
    for name, cls in ENTITY_REGISTRY.items():
        # Check if any role grants access
        for role in roles:
            read = role.permissions.get("read", [])
            write = role.permissions.get("write", [])
            if "*" in read or name in read:
                operations.append(f"- {name}: list, get, search")
            if "*" in write or name in write:
                sm = getattr(cls, "_state_machine", None)
                caps = getattr(cls, "_activated_capabilities", [])
                ops = ["create", "update"]
                if sm:
                    ops.append("transition")
                for cap in caps:
                    cap_name = (
                        cap.capability
                        if hasattr(cap, "capability")
                        else cap.get("capability", "")
                    )
                    ops.append(cap_name)
                operations.append(f"- {name}: {', '.join(ops)}")
                break

    # Load skills if any exist
    try:
        skills = await Skill.find({}).to_list(length=50)
        for skill in skills:
            operations.append(f"- Skill '{skill.name}': {skill.description or ''}")
    except Exception:
        pass

    return "\n".join(operations) if operations else "No operations available"
