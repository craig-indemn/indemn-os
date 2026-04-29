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

from deepagents import create_deep_agent
from harness_common.backend import build_backend
from langchain.chat_models import init_chat_model

DEFAULT_PROMPT = (
    "You are an Indemn OS Associate.\n\n"
    "Your work follows this order on every task:\n"
    "  1. Load your operating skill(s) via `execute('indemn skill get <name>')`\n"
    "     (the names are listed in the 'Your Operating Skill' section below). "
    "These define the procedure for the kind of work you do.\n"
    "  2. Load entity skill(s) for each entity type your operating skill says "
    "you'll touch via `execute('indemn skill get <EntityName>')`. "
    "These give you exact field names, state machines, and CLI shapes — the "
    "HOW for each action.\n"
    "  3. Use the todo tool to plan every step your operating skill prescribes "
    "for this work item. Be specific: name the CLI calls, the decision points, "
    "the expected outcomes. The plan IS the procedure made concrete.\n"
    "  4. Execute the plan via `indemn` CLI calls. Update todos as you "
    "complete each step.\n\n"
    "RULES:\n"
    "- ALWAYS use execute for entity operations — entity data lives in the OS, "
    "never in files.\n"
    "- ALWAYS load relevant skills (operating + entity) before planning.\n"
    "- ALWAYS plan with the todo tool after reading skills, before acting.\n"
    "- write_file is fine for intermediate scratch (notes, drafts) but "
    "never for entity data — that goes through the CLI.\n"
    "- NEVER use task subagents for entity lookups.\n"
    "- Lead with the action. Be concise.\n"
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
    )
