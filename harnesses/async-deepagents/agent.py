"""deepagents agent builder for the async runtime.

Skills loaded via deepagents' progressive disclosure. The associate's own
skill is written to filesystem and passed via the skills parameter.
Entity skills are accessed on demand via execute("indemn skill get <Entity>").
"""

import os

from deepagents import create_deep_agent
from harness_common.backend import build_backend
from langchain.chat_models import init_chat_model

DEFAULT_PROMPT = (
    "You are an Indemn OS Associate.\n\n"
    "CRITICAL: You MUST use the execute tool to run `indemn` CLI commands "
    "for ALL entity operations. This is how you interact with the OS.\n\n"
    "Before working with any entity, read its skill first:\n"
    "  execute('indemn skill get Email')\n"
    "  execute('indemn skill get Company')\n"
    "This gives you the exact field names, states, and CLI commands.\n\n"
    "RULES:\n"
    "- ALWAYS use execute for entity operations.\n"
    "- ALWAYS read the entity skill before creating or updating an entity.\n"
    "- NEVER use write_file to store results.\n"
    "- NEVER use task subagents for entity lookups.\n"
    "- Lead with the action. Be concise.\n"
)


def build_agent(
    associate: dict,
    skills_lib_dir: str | None,
    llm_config: dict,
    activity_id: str | None = None,
):
    """Construct the agent from merged LLM config + skills library dir.

    skills_lib_dir: path to the per-activity skills library directory (which
    contains one subdirectory per skill, each holding its own SKILL.md).
    deepagents discovers skills by scanning this dir for subdirectories with
    a SKILL.md inside; metadata is surfaced in the system prompt and the
    agent loads full content on demand via read_file.

    activity_id: per-invocation identifier passed to build_backend so the
    sandbox is scoped per-activity, preventing cross-invocation tool-cache
    leaks (Bug #3 from os-bugs-and-shakeout).
    """
    model_id = llm_config.pop("model", "anthropic:claude-sonnet-4-6")

    if "vertexai" in model_id:
        llm_config.setdefault("project", os.environ.get("GCP_PROJECT_ID", ""))
        llm_config.setdefault("location", os.environ.get("GCP_LOCATION", "us-central1"))

    system_prompt = associate.get("prompt", "") or DEFAULT_PROMPT

    return create_deep_agent(
        model=init_chat_model(model_id, **llm_config),
        system_prompt=system_prompt,
        backend=build_backend(activity_id=activity_id),
        skills=[skills_lib_dir] if skills_lib_dir else None,
    )
