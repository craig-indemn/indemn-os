"""Tests pinning the Phase-4 DEFAULT_PROMPT for voice-deepagents (AI-407 §15.5).

Phase 3 voice pattern:
  - DEFAULT_PROMPT told the agent to "Load your operating skill(s) via
    execute('indemn skill get <name>')" at Step 1
  - OPERATING_SKILL_SECTION appended per-skill execute() lines

Phase 4 (this migration):
  - Operating skill arrives as a <skill> SystemMessage at session start (Task 2.17)
  - <deployment_context> SystemMessage carries surface-specific context
  - DEFAULT_PROMPT tells the agent to READ those SystemMessages, not load via CLI
  - Entity skills are still loaded via CLI on demand (canonical Phase 4 — only
    OPERATING skill moved to SystemMessage)
  - Voice-specific guidance preserved: BE CONCISE, 1-2 sentences, confirm
    before destructive ops, ask one question at a time

Per design §15.5 lockdown: voice DEFAULT_PROMPT text is locked verbatim.

NOTE: the playbook's Task 2.14 Step 1 includes the assertion
`assert "execute('indemn skill get" not in DEFAULT_PROMPT` which contradicts
the §15.5 locked text — that text DOES include `execute('indemn skill get
<EntityName>')` at Step 3 (entity-skills-via-CLI is canonical Phase 4; only
the OPERATING skill moved to SystemMessage). Tightened here to match the
docstring intent and the chat test's pattern: assert the agent reads <skill>
from SystemMessage rather than fetching the operating skill via CLI. Same
deviation as Phase 2A Task 2.2 Step 2.
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


class TestVoicePhase4DefaultPrompt:
    def test_prompt_references_skill_systemmessage(self):
        """Phase 4: prompt instructs agent to read <skill> SystemMessage."""
        assert "<skill>" in DEFAULT_PROMPT
        assert "SystemMessage" in DEFAULT_PROMPT

    def test_prompt_references_deployment_context(self):
        """Phase 4 voice: <deployment_context> SystemMessage carries surface context."""
        assert "<deployment_context>" in DEFAULT_PROMPT

    def test_prompt_step1_reads_skill_not_loads_via_cli(self):
        """Phase 4: Step 1 of per-turn procedure is 'Read your <skill>', NOT
        'Load your operating skill(s) via execute(indemn skill get ...)'.

        Entity skills are STILL loaded via CLI in Step 3 — that's by design.
        Only the OPERATING skill moved to SystemMessage. The Phase 3 anti-
        pattern (loading the OPERATING skill on every turn) is gone.
        """
        assert "Read your <skill>" in DEFAULT_PROMPT
        # Phase 3 anti-pattern string — should NOT be in Phase 4 prompt
        assert "Load your operating skill" not in DEFAULT_PROMPT

    def test_prompt_emphasizes_brevity(self):
        """Voice-specific: BE CONCISE + 1-2 sentences guidance preserved."""
        assert "BE CONCISE" in DEFAULT_PROMPT or "concise" in DEFAULT_PROMPT.lower()
        assert "1-2 sentences" in DEFAULT_PROMPT or "1–2 sentences" in DEFAULT_PROMPT

    def test_prompt_mentions_voice_real_time_framing(self):
        """Phase 4 voice prompt explicitly frames the agent as voice/TTS."""
        body = DEFAULT_PROMPT.lower()
        assert "voice" in body or "speaking" in body or "tts" in body

    def test_prompt_keeps_destructive_confirm_rule(self):
        """Phase 4 preserves the 'confirm before destructive ops' guidance."""
        assert "confirm" in DEFAULT_PROMPT.lower()
        assert "destructive" in DEFAULT_PROMPT.lower()

    def test_prompt_keeps_no_subagents_rule(self):
        """Voice rule preserved: never spawn task subagents."""
        body = DEFAULT_PROMPT.lower()
        assert "subagent" in body or "task subagents" in body

    def test_prompt_does_not_mandate_write_todos(self):
        """Phase 4 voice DEFAULT_PROMPT per §11.3: voice agents don't need
        todos for conversational turns. The Phase 3 anti-pattern ("Plan with
        the todo tool", "Use the todo tool") is gone. Skills CAN still drive
        todos when conversation is task-shaped (e.g., the Sales Assistant
        proposal skill may call write_todos to enumerate proposal sections);
        but the framework prompt no longer mandates it.

        Skips the ~500-800ms planning step on pure dialogue turns (§11.3 —
        latency budget).

        Task 2.22: this is verification, not implementation. The locked
        Phase 4 prompt (Task 2.14) does the work; this test pins it.
        """
        body = DEFAULT_PROMPT.lower()
        # Negative assertions — these phrases were in the Phase 3 voice prompt
        assert "use the todo tool" not in body
        assert "plan with the todo" not in body
        assert "write_todos for every" not in body
