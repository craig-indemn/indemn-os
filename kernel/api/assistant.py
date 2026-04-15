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

    async def generate():
        import anthropic

        client = anthropic.AsyncAnthropic()

        # Build context-aware system prompt
        system_prompt = (
            "You are the user's assistant in the Indemn OS. "
            "You can help the user understand and operate on their entities, "
            "queue items, and system state. "
            f"The user is viewing: {data.context}. "
            f"The user's name is: {actor.name}."
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
