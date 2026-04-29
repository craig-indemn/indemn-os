"""Unit tests for harness agent system-prompt construction.

Pins the contract that `build_system_prompt(associate)`:
- returns DEFAULT_PROMPT when associate has no custom prompt and no skills
- honors `associate.prompt` as the base when set (operator override)
- appends the OPERATING_SKILL_SECTION suffix when `associate.skills` is non-empty,
  with one `execute('indemn skill get <ref>')` line per skill
- pluralizes the section header for multi-skill associates
- omits the suffix entirely for empty/missing skills lists

The agent reads its operating + entity skills at runtime via the CLI per the
prompt's procedure — no filesystem SKILL.md writes. These tests pin the shape
of the directive that drives that behavior.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make the harness package importable as `agent` (mirrors test_completion_logic).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub deepagents + langchain + harness_common — agent.py imports them at module
# load time, but the functions under test (`build_system_prompt`) are pure string
# manipulation. Mock the deps so the test runs without the harness's runtime venv.
for mod in [
    "deepagents",
    "harness_common",
    "harness_common.backend",
    "langchain",
    "langchain.chat_models",
]:
    sys.modules.setdefault(mod, MagicMock())

from agent import (  # noqa: E402
    DEFAULT_PROMPT,
    OPERATING_SKILL_SECTION,
    build_system_prompt,
)


def test_no_skills_returns_default_prompt():
    associate = {"name": "OS Assistant"}
    assert build_system_prompt(associate) == DEFAULT_PROMPT


def test_empty_skills_list_returns_default_prompt():
    associate = {"name": "OS Assistant", "skills": []}
    assert build_system_prompt(associate) == DEFAULT_PROMPT


def test_single_skill_appends_suffix_with_call():
    associate = {"name": "Email Classifier", "skills": ["email-classifier"]}
    result = build_system_prompt(associate)

    assert result.startswith(DEFAULT_PROMPT)
    assert "## Your Operating Skill\n" in result  # singular header
    assert "## Your Operating Skills\n" not in result
    assert "execute('indemn skill get email-classifier')" in result
    # The suffix should not have the 's' for single skill
    assert "operating instructions" in result.lower()


def test_multiple_skills_pluralized_with_all_calls():
    associate = {
        "name": "Multi Skiller",
        "skills": ["skill-a", "skill-b", "skill-c"],
    }
    result = build_system_prompt(associate)

    assert "## Your Operating Skills\n" in result  # plural header
    assert "execute('indemn skill get skill-a')" in result
    assert "execute('indemn skill get skill-b')" in result
    assert "execute('indemn skill get skill-c')" in result


def test_custom_associate_prompt_honored_as_base():
    custom = "You are a custom associate. Do custom things."
    associate = {
        "name": "Custom",
        "prompt": custom,
        "skills": ["custom-skill"],
    }
    result = build_system_prompt(associate)

    assert result.startswith(custom)
    # Custom prompt replaces DEFAULT_PROMPT — DEFAULT shouldn't be present
    assert "You are an Indemn OS Associate." not in result
    # Suffix is still appended
    assert "execute('indemn skill get custom-skill')" in result


def test_custom_prompt_no_skills_returns_just_custom():
    custom = "You are a custom associate."
    associate = {"name": "Custom", "prompt": custom}
    assert build_system_prompt(associate) == custom


def test_default_prompt_has_ordered_procedure():
    """DEFAULT_PROMPT must direct: load skills → load entity skills → plan via todo → execute."""
    assert "1. Load your operating skill" in DEFAULT_PROMPT
    assert "2. Load entity skill" in DEFAULT_PROMPT
    assert "3. Use the todo tool to plan" in DEFAULT_PROMPT
    assert "4. Execute" in DEFAULT_PROMPT
    # Scratch is allowed; entity data through CLI only
    assert "write_file is fine for intermediate scratch" in DEFAULT_PROMPT


def test_operating_skill_section_says_take_precedence():
    """Suffix must instruct the agent to follow loaded skill rules over generic guidance."""
    assert "take precedence" in OPERATING_SKILL_SECTION
