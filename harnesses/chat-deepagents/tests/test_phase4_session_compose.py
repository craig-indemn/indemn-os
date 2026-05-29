"""ChatSession composes both SystemMessages at start (AI-407 Task 2.9).

Per §15.5 chat DEFAULT_PROMPT framing: the conversation contains
- <skill> SystemMessage(s): operating instructions (PRE-LOADED at session start
  — Phase 4's load-bearing change vs Phase 3's CLI-on-turn-1 fetch)
- <deployment_context> SystemMessage: surface-specific context (who the user
  is, what page they're on, what data scope the session has)

`ChatSession.compose_initial_messages(skill_content, deployment_context)`
produces both SystemMessages. They're prepended to the agent state ONCE at
session start (the start() wiring) and persisted via MongoDB checkpointer
keyed by interaction_id; subsequent turns see them in conversation history.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Real langchain_core for isinstance() checks
from langchain_core.messages import SystemMessage  # noqa: E402

for mod in [
    "deepagents",
    "harness",
    "harness.agent",
    "harness_common",
    "harness_common.backend",
    "harness_common.cli",
    "harness_common.runtime",
    "harness_common.attention",
    "harness_common.interaction",
    "langchain",
    "langchain.chat_models",
    "starlette",
    "starlette.websockets",
    "langgraph",
    "langgraph.checkpoint",
    "langgraph.checkpoint.memory",
    "langgraph.checkpoint.mongodb",
    "motor",
    "motor.motor_asyncio",
]:
    sys.modules.setdefault(mod, MagicMock())

from session import ChatSession  # noqa: E402


class TestChatSessionInitialMessages:
    def test_initial_messages_include_skill_systemmessage(self):
        """Skill content lives in a <skill> SystemMessage."""
        skill = "step 1: ask the user what they need"
        context = {"actor_id": "act_abc", "tenant": "indemn-internal"}

        msgs = ChatSession.compose_initial_messages(skill, context)

        # Both messages are SystemMessages
        sys_msgs = [m for m in msgs if isinstance(m, SystemMessage)]
        assert len(sys_msgs) == 2

        skill_msg = next((m for m in sys_msgs if "<skill>" in m.content), None)
        ctx_msg = next(
            (m for m in sys_msgs if "<deployment_context>" in m.content), None
        )

        assert skill_msg is not None
        assert ctx_msg is not None
        assert "step 1" in skill_msg.content
        assert "act_abc" in ctx_msg.content

    def test_returns_two_systemmessages_in_correct_order(self):
        """Phase 4 chat shape: <skill> first, <deployment_context> second."""
        msgs = ChatSession.compose_initial_messages(
            skill_content="rules: do X",
            deployment_context={"page": "/proposal/new"},
        )

        assert len(msgs) == 2
        assert isinstance(msgs[0], SystemMessage)
        assert isinstance(msgs[1], SystemMessage)
        assert "<skill>" in msgs[0].content
        assert "<deployment_context>" in msgs[1].content

    def test_empty_context_still_produces_deployment_context_message(self):
        """Even with empty context dict, the <deployment_context> SystemMessage
        is still produced — the agent's DEFAULT_PROMPT references it; agent
        should not error on a missing reference."""
        msgs = ChatSession.compose_initial_messages(
            skill_content="...",
            deployment_context={},
        )

        ctx_msg = next(m for m in msgs if "<deployment_context>" in m.content)
        assert "<deployment_context>" in ctx_msg.content

    def test_context_values_appear_in_systemmessage(self):
        """Each key/value of deployment_context becomes visible in the
        <deployment_context> SystemMessage so the agent can read it."""
        context = {
            "actor_id": "act_xyz",
            "actor_name": "Cam Torstenson",
            "current_route": "/proposals",
        }
        msgs = ChatSession.compose_initial_messages(
            skill_content="...", deployment_context=context
        )

        ctx_msg = next(m for m in msgs if "<deployment_context>" in m.content)
        assert "act_xyz" in ctx_msg.content
        assert "Cam Torstenson" in ctx_msg.content
        assert "/proposals" in ctx_msg.content
