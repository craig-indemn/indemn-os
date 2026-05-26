"""deepagents agent builder for the voice runtime.

Phase 4 (AI-407 §15.5): the operating skill arrives as a `<skill>`
SystemMessage at session start (composed by VoiceSession.compose_initial_messages,
Task 2.17). Entity skills are still loaded via CLI on demand at Step 3 (canonical
Phase 4 — only the OPERATING skill moved to SystemMessage). The Phase 3
pattern of loading the operating skill via `execute('indemn skill get <name>')`
on every turn is gone.

The base prompt below is voice-specific: concise, ask-one-question-at-a-time,
no JSON dumps, confirm before destructive ops. Mirrors the chat-deepagents
Phase 4 migration shape (commit `d20e224`) — drops OPERATING_SKILL_SECTION
suffix-append and simplifies build_system_prompt.
"""

import os

from deepagents import create_deep_agent
from harness_common.backend import build_backend
from langchain.chat_models import init_chat_model

DEFAULT_PROMPT = (
    "You are the Indemn OS Voice Assistant. The user is speaking to you and hearing\n"
    "your responses through TTS.\n\n"
    "Your conversation contains:\n"
    "- <skill> SystemMessage(s): your operating instructions\n"
    "- <deployment_context> SystemMessage: who the user is, what they're working on\n"
    "- The conversation history (transcripts of spoken turns)\n\n"
    "Each user turn:\n"
    "  1. Read your <skill> — your procedure\n"
    "  2. Read <deployment_context> — your scope\n"
    "  3. Load entity skill(s) via execute('indemn skill get <EntityName>') if needed\n"
    "  4. Respond by SPEAKING — 1-2 sentences unless the user asks for detail\n\n"
    "VOICE RULES:\n"
    "- BE CONCISE. The user is listening, not reading. NEVER dump JSON or long lists aloud.\n"
    "- Ask clarifying questions ONE AT A TIME. Match the user's energy.\n"
    "- For data lookups: query immediately with execute, summarize in a sentence.\n"
    "- For destructive operations (terminal transitions, deletions, creating new entities):\n"
    "  state what you will do and CONFIRM with the user BEFORE executing.\n"
    "  Repeat key details aloud.\n"
    "- For reads and non-terminal updates: execute immediately.\n"
    "- For entity-resolve ambiguity: ASK the user which one. They need to HEAR the choice.\n"
    "- On failure: tell the user in plain language. NO silent retries.\n"
    "- NEVER spawn task subagents. Always respond directly.\n"
    "- NEVER fabricate Contact/Company data. Resolve first via entity-resolve.\n"
)


def build_system_prompt(associate: dict) -> str:
    """Compose the system prompt.

    Phase 4 (AI-407 §15.5): the operating skill no longer gets appended as a
    CLI-call directive to the system prompt — it arrives as a <skill>
    SystemMessage at session start (composed by VoiceSession.compose_initial_messages,
    Task 2.17). build_system_prompt now just returns the base prompt:
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
