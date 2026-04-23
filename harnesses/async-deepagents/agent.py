"""deepagents agent builder for the async runtime.

Uses deepagents' built-in execute via the backend. No custom tools.
Skills loaded via deepagents' progressive disclosure — metadata in prompt,
full content loaded on demand via read_file. Same pattern as chat harness.
"""

import os

from deepagents import create_deep_agent
from harness_common.backend import build_backend
from langchain.chat_models import init_chat_model

DEFAULT_PROMPT = (
    "You are an Indemn OS Associate.\n\n"
    "CRITICAL: You MUST use the execute tool to run `indemn` CLI commands "
    "for ALL entity operations. This is how you interact with the OS.\n\n"
    "Examples:\n"
    "  execute('indemn email get <id>')\n"
    "  execute('indemn company list')\n"
    "  execute('indemn email update <id> --data \\'...\\'')\n"
    "  execute('indemn email transition <id> --to classified')\n"
    "  execute('indemn contact create --data \\'...\\'')\n\n"
    "RULES:\n"
    "- ALWAYS use the execute tool for entity operations. "
    "NEVER use write_file to store results. "
    "NEVER use task subagents for entity lookups.\n"
    "- Read your skills for correct CLI syntax and field names.\n"
    "- Lead with the action. Be concise.\n"
)


def build_agent(associate: dict, skill_paths: list[str], llm_config: dict):
    """Construct the agent from merged LLM config + skill file paths.

    skill_paths: relative paths to SKILL.md files on the backend filesystem.
    deepagents loads skill metadata into the prompt and the agent reads
    full content on demand via read_file (progressive disclosure).
    """
    model_id = llm_config.pop("model", "anthropic:claude-sonnet-4-6")

    # Vertex AI needs project + location
    if "vertexai" in model_id:
        llm_config.setdefault("project", os.environ.get("GCP_PROJECT_ID", ""))
        llm_config.setdefault("location", os.environ.get("GCP_LOCATION", "us-central1"))

    system_prompt = associate.get("prompt", "") or DEFAULT_PROMPT

    return create_deep_agent(
        model=init_chat_model(model_id, **llm_config),
        system_prompt=system_prompt,
        backend=build_backend(),
        skills=skill_paths if skill_paths else None,
    )
