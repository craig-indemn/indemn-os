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
    "You are an Indemn OS Associate. "
    "Execute actions — don't explain how.\n\n"
    "RULES:\n"
    "- Use the execute tool to run `indemn` CLI commands.\n"
    "- Read your skills for correct CLI syntax. "
    "Never guess field names or states.\n"
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
