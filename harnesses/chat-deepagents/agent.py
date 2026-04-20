"""deepagents agent builder for the chat runtime.

Same pattern as async agent builder — deepagents + backend + three-layer config.
Chat harness uses all 5 middleware (including HITL for real-time human approval).
"""

import os

from deepagents import create_deep_agent
from harness_common.backend import build_backend
from langchain.chat_models import init_chat_model


def build_agent(associate: dict, skills: list[str], llm_config: dict, checkpointer=None):
    """Construct the agent from merged LLM config + loaded skills.

    llm_config is the three-layer merge result:
    {**runtime.llm_config, **associate.llm_config, **deployment.llm_override}
    """
    model_id = llm_config.pop("model", "google_vertexai:gemini-3-flash-preview")

    # Vertex AI needs project + location
    if "vertexai" in model_id:
        llm_config.setdefault("project", os.environ.get("GCP_PROJECT_ID", ""))
        llm_config.setdefault("location", os.environ.get("GCP_LOCATION", "us-central1"))

    # Build system prompt from associate config + loaded skills.
    # Skills are auto-generated from entity definitions and document every
    # field, lifecycle state, and CLI command. No hardcoded command lists —
    # the skills ARE the documentation.
    associate_prompt = associate.get("prompt", "") or (
        "You are the Indemn OS Assistant. "
        "Execute actions — don't explain how.\n\n"
        "RULES:\n"
        "- When asked about data, query it immediately "
        "with the execute tool and `indemn` CLI commands.\n"
        "- When [UI Context] includes entity_data, use it "
        "directly — don't re-fetch what you already have.\n"
        "- Lead with the answer or result. "
        "Be concise. Tables and lists over paragraphs.\n"
        "- For destructive operations (transitions to terminal "
        "states like churned/lost/cancelled, entity deletion, "
        "bulk operations), state what you will do and ask for "
        "confirmation BEFORE executing.\n"
        "- For reads, non-terminal transitions, and updates "
        "— execute immediately without asking.\n"
        "- Use your entity skills for correct CLI syntax. "
        "Never guess field names or states.\n"
        "- If context is insufficient to resolve a reference, "
        "ask one clarifying question.\n"
    )
    skills_block = "\n\n---\n\n".join(skills) if skills else ""
    system_prompt = associate_prompt + "\n\n" + skills_block

    return create_deep_agent(
        model=init_chat_model(model_id, **llm_config),
        system_prompt=system_prompt,
        backend=build_backend(),
        checkpointer=checkpointer,
    )
