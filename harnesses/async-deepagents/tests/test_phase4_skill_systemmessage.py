"""Tests for Phase-4 SystemMessage composition in async (AI-407 §15.5).

Phase 3: skill content went in the HumanMessage as `<skill>...` inside the
`<context>` block.
Phase 4: skill content arrives as a dedicated SystemMessage; entity stays in
HumanMessage. Agent reads <skill> SystemMessage at turn 1 (no CLI fetch for
the operating skill).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make the harness package importable as `main` + `agent` (mirrors test_agent.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# langchain_core is a real dep of the harness — leave it real so SystemMessage
# / HumanMessage are actual types (isinstance() needs that). Import BEFORE
# stubbing the other deps so sys.modules has the real langchain_core.
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

# Stub the other heavy runtime deps that aren't installed in the test env.
# compose_initial_messages is a pure function but main.py imports a lot at
# module load.
for mod in [
    "deepagents",
    "harness",
    "harness.agent",
    "harness.cron_runner",
    "harness.trace_helpers",
    "harness_common",
    "harness_common.backend",
    "harness_common.cli",
    "harness_common.runtime",
    "indemn_os",
    "indemn_os.types",
    "langchain.agents",
    "langchain.agents.middleware",
    "langchain.agents.middleware.types",
    "langchain.chat_models",
    "langchain_core.tracers",
    "langchain_core.tracers.run_collector",
    "langgraph",
    "langgraph.checkpoint",
    "langgraph.checkpoint.memory",
    "langgraph.checkpoint.mongodb",
    "motor",
    "motor.motor_asyncio",
    "temporalio",
    "temporalio.client",
    "temporalio.contrib",
    "temporalio.contrib.opentelemetry",
    "temporalio.worker",
    "temporalio.activity",
]:
    sys.modules.setdefault(mod, MagicMock())


class TestPhase4SkillSystemMessage:
    def test_compose_messages_puts_skill_in_systemmessage(self):
        """Skill content lives in a SystemMessage, not in a HumanMessage block."""
        from main import compose_initial_messages

        messages = compose_initial_messages(
            skill_content="step 1: do X",
            entity_xml="<entity><id>123</id></entity>",
        )

        # First message is a SystemMessage with the skill wrapped in <skill>...</skill>
        assert isinstance(messages[0], SystemMessage)
        assert "<skill>" in messages[0].content
        assert "step 1: do X" in messages[0].content

        # Entity XML lives in a HumanMessage, NOT inside <skill>
        human_messages = [m for m in messages if isinstance(m, HumanMessage)]
        assert len(human_messages) >= 1
        for hm in human_messages:
            assert "<skill>" not in hm.content
        assert "<entity>" in human_messages[0].content

    def test_returns_two_messages_in_correct_order(self):
        """Phase 4 shape: SystemMessage first, then HumanMessage."""
        from main import compose_initial_messages

        messages = compose_initial_messages(
            skill_content="rules: do X",
            entity_xml="<entity>...</entity>",
        )

        assert len(messages) == 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)
