"""deepagents agent builder for the async runtime.

Skill loading uses the OS CLI (`indemn skill get <name>`) for both operating
skills (associate behavioral instructions) and entity skills (auto-generated
field/state docs) — the same surface the agent already uses for everything
else. No filesystem `SKILL.md` writes, no `deepagents.SkillsMiddleware`.

Rationale: our skills are 1-per-associate, not the "many skills, agent
dynamically chooses" pattern that progressive-disclosure-via-filesystem was
designed for. Loading via CLI is symmetric with entity-skill loading the
agent already does, the OS API gives us tamper-evident hash verification +
always-fresh-on-GET, and we eliminate the path-resolution + YAML-escape
class of bugs surfaced as Bug #35 (Sessions 11–12).
"""

import os
from typing import Callable

from deepagents import create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langgraph.checkpoint.memory import MemorySaver
from harness_common.backend import build_backend
from langchain.chat_models import init_chat_model
from langchain_core.messages import ToolMessage


class ExecuteErrorStatusMiddleware(AgentMiddleware):
    """Fix deepagents execute tool returning status='success' on errors.

    The FilesystemMiddleware's execute tool returns plain strings for
    errors (exit_code != 0, FileNotFoundError, etc.). LangGraph wraps
    these as ToolMessage(status='success'). This middleware intercepts
    execute results and sets status='error' when the output indicates
    failure — making errors visible in LangSmith traces and to the agent.
    """

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        result = handler(request)
        tool_name = request.tool_call.get("name", "") if isinstance(request.tool_call, dict) else getattr(request.tool_call, "name", "")
        if tool_name == "execute" and isinstance(result, ToolMessage):
            content = result.content if isinstance(result.content, str) else str(result.content)
            if "[Command failed with exit code" in content or "Error executing command" in content:
                result.status = "error"
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable,
    ) -> ToolMessage:
        result = await handler(request)
        tool_name = request.tool_call.get("name", "") if isinstance(request.tool_call, dict) else getattr(request.tool_call, "name", "")
        if tool_name == "execute" and isinstance(result, ToolMessage):
            content = result.content if isinstance(result.content, str) else str(result.content)
            if "[Command failed with exit code" in content or "Error executing command" in content:
                result.status = "error"
        return result

DEFAULT_PROMPT = (
    "You are an Indemn OS Associate.\n\n"
    "Your context contains two sections:\n"
    "- <skill> — your operating instructions. Follow them.\n"
    "- <entity> — the entity you are processing.\n\n"
    "Your work follows this order:\n"
    "  1. Read your skill in the context — it defines your procedure.\n"
    "  2. Follow the skill's instructions. If it says to run a rules\n"
    "     check first (e.g., `auto-classify --auto`), do that BEFORE\n"
    "     loading entity skills. If rules handle the work, you're done.\n"
    "  3. Only if the skill requires full reasoning: load entity\n"
    "     skill(s) via `execute('indemn skill get <EntityName>')` for each\n"
    "     entity type you'll touch.\n"
    "  4. Use the todo tool to plan every step your skill prescribes.\n"
    "  5. Execute the plan via `indemn` CLI calls. Update todos as you "
    "complete each step.\n\n"
    "RULES:\n"
    "- ALWAYS use execute for entity operations — entity data lives in the OS, "
    "never in files.\n"
    "- Your skill in the context takes precedence over these general guidelines.\n"
    "- write_file is fine for intermediate scratch (notes, drafts) but "
    "never for entity data — that goes through the CLI.\n"
    "- NEVER use task subagents for entity lookups.\n"
    "- Lead with the action. Be concise.\n"
)


def build_system_prompt(associate: dict) -> str:
    """Compose the system prompt.

    base = `associate.prompt` if set (operator override), else DEFAULT_PROMPT.
    Skills are included in the user message context, not the system prompt.
    """
    return associate.get("prompt") or DEFAULT_PROMPT


def build_agent(
    associate: dict,
    llm_config: dict,
    activity_id: str | None = None,
):
    """Construct the agent from merged LLM config + per-associate system prompt.

    activity_id: per-invocation identifier passed to build_backend so the
    sandbox is scoped per-activity, preventing cross-invocation tool-cache
    leaks (Bug #3 from os-bugs-and-shakeout).
    """
    model_id = llm_config.pop("model", "anthropic:claude-sonnet-4-6")

    if "vertexai" in model_id:
        llm_config.setdefault("project", os.environ.get("GCP_PROJECT_ID", ""))
        llm_config.setdefault("location", os.environ.get("GCP_LOCATION", "us-central1"))

    return create_deep_agent(
        model=init_chat_model(model_id, **llm_config),
        system_prompt=build_system_prompt(associate),
        backend=build_backend(activity_id=activity_id),
        middleware=[ExecuteErrorStatusMiddleware()],
        checkpointer=MemorySaver(),
    )
