"""Unit tests for chat-deepagents agent system-prompt construction (Phase 4).

UPDATED for AI-407 Phase 4 (§15.5):
  - DEFAULT_PROMPT is the §15.5 chat locked text (SystemMessage skill framing)
  - OPERATING_SKILL_SECTION REMOVED — operating skill arrives as <skill>
    SystemMessage at session start, composed by ChatSession.compose_initial_messages
    in Task 2.9, persisted via MongoDB checkpointer
  - build_system_prompt simplifies to: associate.prompt override OR DEFAULT_PROMPT.
    No per-skill suffix appending.

The Phase-4-specific shape pins (SystemMessage references, deployment_context,
"Read your <skill>" framing) live in test_phase4_prompt.py — this file pins
the smaller build_system_prompt contract (prompt resolution).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make the harness package importable as `agent`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub deepagents + langchain + harness_common.
for mod in [
    "deepagents",
    "harness_common",
    "harness_common.backend",
    "langchain",
    "langchain.chat_models",
]:
    sys.modules.setdefault(mod, MagicMock())

from agent import DEFAULT_PROMPT, build_system_prompt  # noqa: E402


def test_no_skills_returns_default_prompt():
    """No associate.prompt, no skills → DEFAULT_PROMPT verbatim."""
    associate = {"name": "OS Assistant"}
    assert build_system_prompt(associate) == DEFAULT_PROMPT


def test_empty_skills_list_returns_default_prompt():
    """Phase 4: empty skills list is a no-op (was no-op in Phase 3 too)."""
    associate = {"name": "OS Assistant", "skills": []}
    assert build_system_prompt(associate) == DEFAULT_PROMPT


def test_skills_list_does_NOT_append_suffix():
    """Phase 4: build_system_prompt no longer appends per-skill execute() lines.

    Operating skill arrives as a <skill> SystemMessage at session start
    (Task 2.9). The system_prompt only contains DEFAULT_PROMPT (or the operator's
    override). This pins the Phase-3 → Phase-4 behavior change.
    """
    associate = {"name": "Voice OS Assistant", "skills": ["log-touchpoint"]}
    result = build_system_prompt(associate)

    # System prompt IS the default — no skill-section suffix
    assert result == DEFAULT_PROMPT
    # Specifically: no per-skill execute() directives in the system prompt
    assert "execute('indemn skill get log-touchpoint')" not in result


def test_custom_associate_prompt_honored_as_base():
    """Operator override via associate.prompt still works (Phase 3 + Phase 4)."""
    custom = "You are a custom assistant. Be specific."
    associate = {
        "name": "Custom",
        "prompt": custom,
        "skills": ["custom-skill"],
    }
    result = build_system_prompt(associate)

    # Custom prompt is returned verbatim
    assert result == custom
    # Phase 4: skills list does NOT append anything
    assert "execute('indemn skill get custom-skill')" not in result


def test_custom_prompt_no_skills_returns_just_custom():
    """Custom prompt, no skills — return custom verbatim."""
    custom = "You are a custom assistant."
    associate = {"name": "Custom", "prompt": custom}
    assert build_system_prompt(associate) == custom
