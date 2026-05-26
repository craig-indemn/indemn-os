"""deepagents agent builder for the chat runtime.

Skill loading uses the OS CLI (`indemn skill get <name>`) for both operating
skills (associate behavioral instructions) and entity skills (auto-generated
field/state docs) — the same surface the agent already uses for everything
else. No filesystem `SKILL.md` writes, no `deepagents.SkillsMiddleware`.

Mirrors the async-deepagents pattern (commit `7281b83`). Same rationale:
our skills are 1-per-associate, not the "many skills, agent dynamically
chooses" pattern progressive-disclosure-via-filesystem was designed for.
Loading via CLI is symmetric with entity-skill loading the agent already
does, the OS API gives us tamper-evident hash verification + always-
fresh-on-GET, and we eliminate the path-resolution + YAML-escape class
of bugs surfaced as Bug #35.
"""

import os

from deepagents import create_deep_agent
from harness_common.backend import build_backend
from langchain.chat_models import init_chat_model

DEFAULT_PROMPT = (
    "You are an Indemn OS Assistant talking with a user via real-time chat.\n\n"
    "Your conversation contains:\n"
    "- <skill> SystemMessage(s): your operating instructions\n"
    "- <deployment_context> SystemMessage: surface-specific context\n"
    "  (who the user is, what page they're on, what data scope you have)\n"
    "- The conversation history\n\n"
    "Each user turn:\n"
    "  1. Read your <skill> — your procedure\n"
    "  2. Read <deployment_context> — your scope for this conversation\n"
    "  3. Load entity skill(s) via execute('indemn skill get <EntityName>') if you need them\n"
    "  4. Respond. Use execute for CLI actions.\n\n"
    "RULES:\n"
    "- Be helpful and concise\n"
    "- Use execute for entity operations\n"
    "- NEVER fabricate — query first\n"
    "- For destructive operations (deletes, terminal transitions, new-entity creation):\n"
    "  confirm with the user FIRST\n"
    "- Your skill takes precedence over these guidelines\n"
)


def build_system_prompt(associate: dict) -> str:
    """Compose the system prompt.

    Phase 4 (AI-407 §15.5): the operating skill no longer gets appended as a
    CLI-call directive to the system prompt — it arrives as a <skill>
    SystemMessage at session start (composed by ChatSession.compose_initial_messages,
    Task 2.9). build_system_prompt now just returns the base prompt:
    `associate.prompt` if set (operator override), else DEFAULT_PROMPT.
    """
    return associate.get("prompt") or DEFAULT_PROMPT


def build_agent(
    associate: dict,
    llm_config: dict,
    checkpointer=None,
):
    """Construct the agent from merged LLM config + per-associate system prompt.

    Skills are not pre-loaded into the agent. The system prompt directs the
    agent to call `execute('indemn skill get <name>')` on turn 1 to fetch
    operating + entity skill content as tool results.
    """
    model_id = llm_config.pop("model", "google_vertexai:gemini-3-flash-preview")

    # Vertex AI needs project + location
    if "vertexai" in model_id:
        llm_config.setdefault("project", os.environ.get("GCP_PROJECT_ID", ""))
        llm_config.setdefault("location", os.environ.get("GCP_LOCATION", "us-central1"))

    return create_deep_agent(
        model=init_chat_model(model_id, **llm_config),
        system_prompt=build_system_prompt(associate),
        backend=build_backend(),
        checkpointer=checkpointer,
        subagents=[],
    )
