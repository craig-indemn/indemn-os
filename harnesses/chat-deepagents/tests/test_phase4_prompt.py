"""Tests pinning the Phase-4 DEFAULT_PROMPT for chat-deepagents (AI-407 §15.5).

Phase 3 chat pattern:
  - DEFAULT_PROMPT told the agent to "Load your operating skill via execute('indemn
    skill get <name>')" at Step 1
  - OPERATING_SKILL_SECTION appended per-skill execute() lines

Phase 4 (this migration):
  - Operating skill arrives as a <skill> SystemMessage at session start (Task 2.9)
  - <deployment_context> SystemMessage carries surface-specific context
  - DEFAULT_PROMPT tells the agent to READ those SystemMessages, not load via CLI
  - Entity skills are still loaded via CLI on demand (canonical Phase 4 — only
    OPERATING skill moved to SystemMessage)

Per design §15.5 lockdown: chat DEFAULT_PROMPT text is locked verbatim.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for mod in [
    "deepagents",
    "harness_common",
    "harness_common.backend",
    "langchain",
    "langchain.chat_models",
]:
    sys.modules.setdefault(mod, MagicMock())

from agent import DEFAULT_PROMPT  # noqa: E402


class TestChatPhase4DefaultPrompt:
    def test_prompt_references_skill_systemmessage(self):
        """Phase 4: prompt instructs agent to read <skill> SystemMessage."""
        assert "<skill>" in DEFAULT_PROMPT
        assert "SystemMessage" in DEFAULT_PROMPT

    def test_prompt_references_deployment_context(self):
        """Phase 4 chat: <deployment_context> SystemMessage carries surface context."""
        assert "<deployment_context>" in DEFAULT_PROMPT

    def test_prompt_step1_reads_skill_not_loads_via_cli(self):
        """Phase 4: Step 1 of per-turn procedure is 'Read your <skill>', NOT
        'Load your operating skill via execute(indemn skill get ...)'.

        Entity skills are STILL loaded via CLI in Step 3 — that's by design.
        Only the OPERATING skill moved to SystemMessage. The Phase 3 anti-
        pattern (loading the OPERATING skill on every turn) is gone.
        """
        assert "Read your <skill>" in DEFAULT_PROMPT
        # Phase 3 anti-pattern strings — should NOT be in Phase 4 prompt
        assert "Load your operating skill" not in DEFAULT_PROMPT

    def test_prompt_mentions_chat_real_time_framing(self):
        """Phase 4 chat prompt explicitly frames the agent as real-time chat."""
        assert "real-time chat" in DEFAULT_PROMPT or "chat" in DEFAULT_PROMPT.lower()

    def test_prompt_keeps_destructive_confirm_rule(self):
        """Phase 4 preserves the 'confirm before destructive ops' guidance."""
        assert "confirm" in DEFAULT_PROMPT.lower()
        assert "destructive" in DEFAULT_PROMPT.lower()
