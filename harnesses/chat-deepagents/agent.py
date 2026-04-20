"""deepagents agent builder for the chat runtime.

Same pattern as async agent builder — deepagents + backend + three-layer config.
Chat harness uses all 5 middleware (including HITL for real-time human approval).
"""

import os

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from harness_common.backend import build_backend


def build_agent(associate: dict, skills: list[str], llm_config: dict, checkpointer=None):
    """Construct the agent from merged LLM config + loaded skills.

    llm_config is the three-layer merge result:
    {**runtime.llm_config, **associate.llm_config, **deployment.llm_override}
    """
    model_id = llm_config.pop("model", "google_vertexai:gemini-2.0-flash")

    # Vertex AI needs project + location
    if "vertexai" in model_id:
        llm_config.setdefault("project", os.environ.get("GCP_PROJECT_ID", ""))
        llm_config.setdefault("location", os.environ.get("GCP_LOCATION", "us-central1"))

    # Operational instructions teach the assistant how to use the CLI
    operational_block = """You are the Indemn OS Assistant. You help users manage their insurance operations.

## Your Capabilities
You can perform ANY operation the user can do, using the `indemn` CLI via the execute tool. Key commands:
- `indemn {entity} list [--limit N] [--status STATE]` — list entities
- `indemn {entity} get ID` — get entity details
- `indemn {entity} create --data 'JSON'` — create entity
- `indemn {entity} update ID --data 'JSON'` — update fields
- `indemn {entity} transition ID --to STATE` — change state

Entity types: Company, Contact, Deal, Conference, Task, Meeting, Signal, Decision, Commitment, AssociateDeployment, Outcome, Playbook, Stage, OutcomeType, AssociateType

## Guidelines
- When asked about the current entity, use the context provided (entity_data in the user message context).
- When asked to create or modify entities, use the CLI via the execute tool.
- When querying across entities, use `indemn {entity} list`.
- Confirm destructive actions before executing.
- Be concise and helpful.

## Entity Skills
The following skills describe each entity's fields, lifecycle, and commands:
"""

    # Build system prompt: operational block + associate prompt + skill contents
    associate_prompt = associate.get("prompt", "")
    skills_block = "\n\n".join(skills) if skills else ""
    system_prompt = operational_block + "\n\n" + associate_prompt + "\n\n---\n\n" + skills_block

    return create_deep_agent(
        model=init_chat_model(model_id, **llm_config),
        system_prompt=system_prompt,
        backend=build_backend(),
        checkpointer=checkpointer,
    )
