"""Tests for deterministic skill interpreter — step parsing and condition parsing."""

from kernel.temporal.activities import _parse_simple_condition, _parse_skill_steps


class TestParseSkillSteps:
    def test_extracts_backtick_commands(self):
        skill = """# My Skill
        1. Run `indemn email classify EMAIL-001`
        2. Then `indemn email send EMAIL-001 --to admin`
        """
        steps = _parse_skill_steps(skill)
        assert len(steps) == 2
        assert steps[0]["type"] == "command"
        assert steps[0]["command"] == "indemn email classify EMAIL-001"
        assert steps[1]["type"] == "command"
        assert steps[1]["command"] == "indemn email send EMAIL-001 --to admin"

    def test_detects_auto_commands(self):
        skill = "Run `indemn submission classify SUB-001 --auto`"
        steps = _parse_skill_steps(skill)
        assert len(steps) == 1
        assert steps[0]["type"] == "auto_command"
        assert "--auto" in steps[0]["command"]

    def test_detects_conditions(self):
        skill = "If needs_reasoning is true\nWhen status equals pending"
        steps = _parse_skill_steps(skill)
        assert len(steps) == 2
        assert steps[0]["type"] == "condition"
        assert steps[1]["type"] == "condition"

    def test_skips_headers_and_dividers(self):
        skill = "# Header\n---\n`indemn test run`"
        steps = _parse_skill_steps(skill)
        assert len(steps) == 1
        assert steps[0]["command"] == "indemn test run"

    def test_skips_empty_lines(self):
        skill = "\n\n`indemn foo bar`\n\n"
        steps = _parse_skill_steps(skill)
        assert len(steps) == 1

    def test_mixed_steps(self):
        skill = """
# Process submission
1. `indemn submission classify SUB-001 --auto`
If needs_reasoning is true
2. `indemn submission review SUB-001`
"""
        steps = _parse_skill_steps(skill)
        assert len(steps) == 3
        assert steps[0]["type"] == "auto_command"
        assert steps[1]["type"] == "condition"
        assert steps[2]["type"] == "command"


class TestParseSimpleCondition:
    def test_boolean_true(self):
        result = _parse_simple_condition("If needs_reasoning is true")
        assert result == {"field": "needs_reasoning", "op": "equals", "value": True}

    def test_boolean_false(self):
        result = _parse_simple_condition("When active equals false")
        assert result == {"field": "active", "op": "equals", "value": False}

    def test_string_value(self):
        result = _parse_simple_condition("If status is pending")
        assert result == {"field": "status", "op": "equals", "value": "pending"}

    def test_unparseable_fallback(self):
        result = _parse_simple_condition("If the sky is blue today")
        # Should return a fallback that matches anything
        assert result["field"] in ("sky", "_always")
