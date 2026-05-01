"""deepagents agent builder for the voice runtime.

Same shape as chat-deepagents/agent.py — deepagents.create_deep_agent +
backend + three-layer config — only the DEFAULT_PROMPT differs to
account for voice-channel constraints (concise, ask-one-question-at-a-
time, no JSON dumps, confirm before destructive ops).

Skills load via CLI in the agent loop (per indemn-os main commit 7281b83):
the system prompt directs the agent to `execute('indemn skill get <name>')`
on turn 1; skill content arrives as a tool result and stays in the
agent's message history. Symmetric with how the agent loads entity
skills + everything else in the OS.
"""

import os

from deepagents import create_deep_agent
from harness_common.backend import build_backend
from langchain.chat_models import init_chat_model

DEFAULT_PROMPT = (
    "You are the Indemn OS Voice Assistant. "
    "The user is talking to you by voice — you hear their words, you reply by voice.\n\n"
    "RULES:\n"
    "- BE CONCISE. The user is listening, not reading. 1-2 sentences per turn unless "
    "they explicitly ask for detail. NEVER read raw JSON or long lists aloud.\n"
    "- Ask clarifying questions ONE AT A TIME. Match the user's energy — short answers "
    "deserve short questions.\n"
    "- For data lookups, query immediately with the execute tool and `indemn` CLI "
    "commands, then summarize the result in a sentence.\n"
    "- For destructive operations (transitions to terminal states like churned/lost/"
    "cancelled, entity deletion, bulk operations) AND for creating new entities — "
    "state what you will do and confirm with the user BEFORE executing. Repeat back "
    "the key details so they can hear what you understood.\n"
    "- For reads and non-terminal updates — execute immediately without asking.\n"
    "- Read your entity skills for correct CLI syntax. Never guess field names or states.\n"
    "- For entity-resolve ambiguity (multiple candidates), ASK the user which they "
    "meant. Never silently pick the top match for voice — they need to hear the choice.\n"
    "- If something fails, tell the user what failed in plain language and ask how to "
    "proceed. Don't retry silently.\n"
    "- NEVER use the task tool to spawn subagents. Always respond directly.\n"
    "- NEVER fabricate Contact/Company data. Resolve via `entity-resolve` first.\n"
)


def build_agent(
    associate: dict,
    skill_paths: list[str],
    llm_config: dict,
    checkpointer=None,
):
    """Construct the agent from merged LLM config + skill file paths.

    Mirrors chat-deepagents/agent.py::build_agent — the deepagents agent
    instance is the same shape; only the system_prompt and the I/O
    transport differ. Skill metadata is loaded into the prompt; the
    agent reads full content on demand via read_file (deepagents'
    progressive disclosure) — same pattern as chat.

    skill_paths: relative paths to SKILL.md files on the backend
    filesystem. Pass [] to skip the deepagents skills layer.
    """
    model_id = llm_config.pop("model", "google_vertexai:gemini-3-flash-preview")

    # Vertex AI needs project + location.
    if "vertexai" in model_id:
        llm_config.setdefault("project", os.environ.get("GCP_PROJECT_ID", ""))
        llm_config.setdefault("location", os.environ.get("GCP_LOCATION", "us-central1"))

    system_prompt = associate.get("prompt", "") or DEFAULT_PROMPT

    return create_deep_agent(
        model=init_chat_model(model_id, **llm_config),
        system_prompt=system_prompt,
        backend=build_backend(),
        checkpointer=checkpointer,
        skills=skill_paths if skill_paths else None,
        subagents=[],
    )
