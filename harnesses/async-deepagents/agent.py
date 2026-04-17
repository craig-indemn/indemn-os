"""deepagents agent builder for the async runtime.

Uses deepagents' built-in execute via the backend. No custom tools.
4 middleware modules for async (Todo, Filesystem, Subagents, Summarization).
HITL middleware excluded for async — handoffs use message emission.
"""

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from harness_common.backend import build_backend


def build_agent(associate: dict, skills: list[str], llm_config: dict):
    """Construct the agent from merged LLM config + loaded skills.

    llm_config is the three-layer merge result:
    {**runtime.llm_config, **associate.llm_config, **deployment.llm_override}
    """
    import os

    model_id = llm_config.pop("model", "anthropic:claude-sonnet-4-6")

    # Vertex AI needs project + location
    if "vertexai" in model_id:
        llm_config.setdefault("project", os.environ.get("GCP_PROJECT_ID", ""))
        llm_config.setdefault("location", os.environ.get("GCP_LOCATION", "us-central1"))

    system_prompt = associate.get("prompt", "") + "\n\n---\n\n" + "\n\n".join(skills)

    return create_deep_agent(
        model=init_chat_model(model_id, **llm_config),
        system_prompt=system_prompt,
        backend=build_backend(),
    )
