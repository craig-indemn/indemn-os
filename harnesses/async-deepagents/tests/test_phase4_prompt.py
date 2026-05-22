"""Tests pinning the Phase-4 DEFAULT_PROMPT for async-deepagents (AI-407 §15.5).

The Phase-4 shift: operating skills arrive as a <skill> SystemMessage at
session start (composed by the harness in compose_initial_messages — Task 2.3),
not loaded via CLI on turn 1 as in Phase 3. Entity skills continue to load
via execute('indemn skill get <EntityName>') on demand — that pattern is
preserved (this is what makes the skill loading 'progressive disclosure-via-CLI'
canonical across all three harnesses).

Per design §15.5 lockdown: the DEFAULT_PROMPT text is locked verbatim.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make the harness package importable as `agent` (mirrors test_agent.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub heavy runtime deps — DEFAULT_PROMPT is a module-level string constant,
# but agent.py imports deepagents + langchain + harness_common at module load.
for mod in [
    "deepagents",
    "harness_common",
    "harness_common.backend",
    "langchain",
    "langchain.agents",
    "langchain.agents.middleware",
    "langchain.agents.middleware.types",
    "langchain.chat_models",
    "langchain_core",
    "langchain_core.messages",
    "langgraph",
    "langgraph.checkpoint",
    "langgraph.checkpoint.memory",
]:
    sys.modules.setdefault(mod, MagicMock())

from agent import DEFAULT_PROMPT  # noqa: E402


class TestAsyncDefaultPrompt:
    def test_prompt_references_skill_systemmessage(self):
        """Phase 4: prompt instructs agent to read <skill> SystemMessage."""
        assert "<skill>" in DEFAULT_PROMPT
        assert "SystemMessage" in DEFAULT_PROMPT

    def test_prompt_step1_reads_systemmessage_not_cli(self):
        """Phase 4: Step 1 of work order is 'Read your <skill> SystemMessage'.

        Phase 3 anti-pattern: Step 1 loaded the OPERATING skill via CLI
        (`execute('indemn skill get <associate-name>')`). Phase 4 removes
        that — operating skill arrives in a SystemMessage at session start.

        ENTITY skills are STILL loaded via CLI in Step 3 — that's by design,
        progressive disclosure-via-CLI is the canonical pattern. This test
        only pins Step 1's shift.

        Implementer note (deviation from playbook spec): playbook Task 2.2
        Step 2 asserts `"indemn skill get" not in DEFAULT_PROMPT` — but the
        §15.5 locked text includes that string at Step 3 (entity skills).
        Tightening the assertion to match the locked text's intent.
        """
        assert "Read your <skill> SystemMessage" in DEFAULT_PROMPT

    def test_prompt_mentions_entity_reference(self):
        """Phase 4 still has the <entity> context reference (in HumanMessage)."""
        assert "<entity>" in DEFAULT_PROMPT
