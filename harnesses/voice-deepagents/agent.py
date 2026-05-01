"""deepagents agent builder for the voice runtime.

Skill loading uses the OS CLI (`indemn skill get <name>`) for both operating
skills (associate behavioral instructions) and entity skills (auto-generated
field/state docs) — same pattern as chat + async-deepagents. No filesystem
`SKILL.md` writes, no `deepagents.SkillsMiddleware`.

The base prompt below is voice-specific: concise, ask-one-question-at-a-time,
no JSON dumps, confirm before destructive ops. The ordered procedure
(load skill → load entity skills → plan → execute) mirrors async + chat;
voice-specific guidance layers on top.
"""

import os

from deepagents import create_deep_agent
from harness_common.backend import build_backend
from langchain.chat_models import init_chat_model

DEFAULT_PROMPT = (
    "You are the Indemn OS Voice Assistant. "
    "The user is talking to you by voice — you hear their words, you reply by voice.\n\n"
    "Your work follows this order on every task:\n"
    "  1. Load your operating skill(s) via `execute('indemn skill get <name>')`\n"
    "     (the names are listed in the 'Your Operating Skill' section below). "
    "These define the procedure for the kind of work you do.\n"
    "  2. Load entity skill(s) for each entity type your operating skill says "
    "you'll touch via `execute('indemn skill get <EntityName>')`. "
    "These give you exact field names, state machines, and CLI shapes — the "
    "HOW for each action.\n"
    "  3. Plan with the todo tool after reading skills, before acting.\n"
    "  4. Execute the plan via `indemn` CLI calls.\n\n"
    "VOICE RULES:\n"
    "- BE CONCISE. The user is listening, not reading. 1–2 sentences per "
    "turn unless they explicitly ask for detail. NEVER read raw JSON or "
    "long lists aloud.\n"
    "- Ask clarifying questions ONE AT A TIME. Match the user's energy — "
    "short answers deserve short questions.\n"
    "- For data lookups, query immediately with the execute tool, then "
    "summarize the result in a sentence.\n"
    "- For destructive operations (transitions to terminal states like "
    "churned/lost/cancelled, entity deletion, bulk operations) AND for "
    "creating new entities — state what you will do and confirm with the "
    "user BEFORE executing. Repeat back the key details so they can hear "
    "what you understood.\n"
    "- For reads and non-terminal updates — execute immediately without "
    "asking.\n"
    "- For entity-resolve ambiguity (multiple candidates), ASK the user "
    "which they meant. Never silently pick the top match for voice — they "
    "need to hear the choice.\n"
    "- If something fails, tell the user what failed in plain language and "
    "ask how to proceed. Don't retry silently.\n"
    "- NEVER use the task tool to spawn subagents. Always respond directly.\n"
    "- NEVER fabricate Contact/Company data. Resolve via `entity-resolve` first.\n"
)

OPERATING_SKILL_SECTION = (
    "\n\n## Your Operating Skill{plural}\n\n"
    "Step 1 of every task: run these CLI calls to load your operating "
    "instructions:\n"
    "{calls}\n"
    "These skills define WHO you are and HOW you process this kind of work. "
    "They take precedence over the general guidance above when they conflict.\n"
)


def build_system_prompt(associate: dict) -> str:
    """Compose the system prompt: base prompt + per-associate skill section.

    base = `associate.prompt` if set (operator override), else DEFAULT_PROMPT.
    Suffix lists the associate's skill refs with the exact `execute(...)`
    invocations the agent should make in step 1 of its procedure. Empty
    skills list → no suffix appended.
    """
    base = associate.get("prompt") or DEFAULT_PROMPT
    skill_refs = associate.get("skills") or []
    if not skill_refs:
        return base
    calls = "\n".join(f"  execute('indemn skill get {ref}')" for ref in skill_refs)
    suffix = OPERATING_SKILL_SECTION.format(
        plural="s" if len(skill_refs) > 1 else "",
        calls=calls,
    )
    return base + suffix


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
