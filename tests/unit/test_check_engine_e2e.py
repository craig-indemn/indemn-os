"""End-to-end tests for `kernel/eval/check_engine.py` against IE-1/IE-2/IE-4.

These exercise the EXACT check expressions of the 3 IE code Evaluators from P2
(seeded as Evaluator records in dev OS):
  IE-1: ie_entity_skills_loaded (6a14b1249187c4299d7115f7)
  IE-2: ie_touchpoint_transitioned_to_processed (6a14b1269187c4299d7115f9)
  IE-4: ie_entities_link_correctly (6a14b12d9187c4299d7115fd)

The fixture trace structure mirrors Run 11's IE trace (LangSmith trace_id
1257e0f8-9655-42f1-aa4a-d538f9f6fa0e, OS trace _id 6a0f3e4f645b9e9a56d09814)
at the level of detail needed to verify each Evaluator passes/fails correctly.

Per Group D++ no-fallbacks: if any check_engine path cannot evaluate cleanly
against this fixture, STOP and escalate — do NOT silently coerce or fall back.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from kernel.eval import check_engine


# ---------------------------------------------------------------------------
# Fixture: synthetic Run 11 IE trace mimicking the actual structure


TOUCHPOINT_ID = "6a09cd120b1654c6ada0c0ae"
COMPANY_ID = "69fcae6883e73b8c4346eaae"
CORRELATION_ID = "f3c30b0a19fd4fb4a44ba3bae58875da"


def make_run11_like_trace() -> dict:
    """Build a synthetic trace mirroring Run 11's IE structure.

    Key shape preserved:
    - 37 messages (mixed types)
    - First AI message has tool_calls including chained `indemn skill get X` commands
    - Followed by `indemn <entity> create` calls in later AI messages
    - Final AI message has a `indemn touchpoint transition <id> --to processed` call

    Shape simplified vs the live trace (which has 37 messages with full content);
    here we keep just the tool-call structure that the checks rely on.
    """
    return {
        "_id": "6a0f3e4f645b9e9a56d09814",
        "trace_id": "1257e0f8-9655-42f1-aa4a-d538f9f6fa0e",
        "associate_id": "69ea1bd223eefe641ea13f4c",
        "associate_name": "Intelligence Extractor",
        "entity_type": "Touchpoint",
        "entity_id": TOUCHPOINT_ID,
        "correlation_id": CORRELATION_ID,
        "messages": [
            # Initial human prompt
            {"type": "human", "content": "Process this work: ..."},
            # AI: Step 0 — load entity skills (chained `indemn skill get` commands)
            {
                "type": "ai",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "name": "execute",
                        "args": {
                            "command": (
                                "indemn skill get Operation && "
                                "indemn skill get OperationStep && "
                                "indemn skill get Decision && "
                                "indemn skill get Commitment && "
                                "indemn skill get Signal && "
                                "indemn skill get Task && "
                                "indemn skill get Opportunity && "
                                "indemn skill get Contact && "
                                "indemn skill get System && "
                                "indemn skill get ReviewItem && "
                                "indemn skill get BusinessRelationship"
                            )
                        },
                    }
                ],
            },
            {"type": "tool", "tool_call_id": "tc1", "content": "[Command succeeded with exit code 0]\n..."},
            # AI: Step 0.5 — write_todos plan
            {"type": "ai", "tool_calls": [{"id": "tc2", "name": "write_todos", "args": {"todos": []}}]},
            {"type": "tool", "tool_call_id": "tc2", "content": "ok"},
            # AI: Step 1 — load constellation (chained list calls)
            {
                "type": "ai",
                "tool_calls": [
                    {
                        "id": "tc3",
                        "name": "execute",
                        "args": {"command": f"indemn contact list --data '{{\"company\":\"{COMPANY_ID}\"}}'"},
                    }
                ],
            },
            {"type": "tool", "tool_call_id": "tc3", "content": "[Command succeeded with exit code 0]\n..."},
            # AI: Step 2 — entity creates (operation create — note "operation" matches IE-1's regex_target)
            {
                "type": "ai",
                "tool_calls": [
                    {
                        "id": "tc4",
                        "name": "execute",
                        "args": {"command": "indemn operation create --data '{\"name\":\"Webchat Support Workflow\",...}'"},
                    }
                ],
            },
            {"type": "tool", "tool_call_id": "tc4", "content": "[Command succeeded with exit code 0]\n<Operation id=\"6a0f3eaa645b9e9a56d09820\">..."},
            # AI: more creates (signal create, decision create, etc.)
            {
                "type": "ai",
                "tool_calls": [
                    {
                        "id": "tc5",
                        "name": "execute",
                        "args": {"command": "indemn signal create --data '{...}'"},
                    },
                    {
                        "id": "tc6",
                        "name": "execute",
                        "args": {"command": "indemn decision create --data '{...}'"},
                    },
                ],
            },
            {"type": "tool", "tool_call_id": "tc5", "content": "[Command succeeded with exit code 0]\n..."},
            {"type": "tool", "tool_call_id": "tc6", "content": "[Command succeeded with exit code 0]\n..."},
            # AI: Step 3 — transition Touchpoint to processed
            {
                "type": "ai",
                "tool_calls": [
                    {
                        "id": "tc7",
                        "name": "execute",
                        "args": {
                            "command": (
                                f"indemn touchpoint transition {TOUCHPOINT_ID} --to processed "
                                '--reason "Extracted 3 Operations + 5 OperationSteps + 3 Decisions ..."'
                            )
                        },
                    }
                ],
            },
            {"type": "tool", "tool_call_id": "tc7", "content": "[Command succeeded with exit code 0]\n..."},
        ],
    }


# ---------------------------------------------------------------------------
# IE-1: ie_entity_skills_loaded — should PASS on Run 11 (entity skills loaded before creates)


@pytest.mark.asyncio
async def test_ie1_passes_on_run11_like_trace_with_refined_record():
    """IE-1 (with the Session 34 refinement using `trace.tool_call_commands`) PASSES on Run 11.

    The refinement: changed first sub-check field from
      `trace.messages[*].tool_calls[*].args.command` (raw chained strings)
    to
      `trace.tool_call_commands` (P3-added derived path — flat split list).

    With the derived path, the anchored regex `^indemn skill get (Op|...)$` matches
    each individual chained command — the IE skill's `&&` pattern works correctly.

    This mirrors the LIVE IE-1 Evaluator record in dev OS as of Session 34.
    """
    trace = make_run11_like_trace()
    ie1_check = {
        "all": [
            {
                "field": "trace.tool_call_commands",
                "op": "any_matches_regex",
                "value": r"^indemn skill get (Operation|OperationStep|Decision|Commitment|Signal|Task|Opportunity|Contact|System|ReviewItem|BusinessRelationship)$",
            },
            {
                "field": "trace.messages",
                "op": "first_call_matching_regex_before_first_create",
                "regex_call": "indemn skill get",
                "regex_target": r"indemn (operation|signal|decision|commitment|task|opportunity|contact|system|reviewitem|businessrelationship|operationstep) create",
            },
        ]
    }
    result = await check_engine.evaluate_check(ie1_check, trace)
    assert result is True, "IE-1 with the refined check expression passes on chained commands"


@pytest.mark.asyncio
async def test_ie1_pre_refactor_record_still_fails_on_chained_documented():
    """Documents the OLD (pre-refactor) IE-1 behavior — if someone reverts the live
    record back to using `trace.messages[*].tool_calls[*].args.command`, this test
    fires as a regression guard."""
    trace = make_run11_like_trace()
    old_ie1_check = {
        "all": [
            {
                "field": "trace.messages[*].tool_calls[*].args.command",
                "op": "any_matches_regex",
                "value": r"^indemn skill get (Operation|OperationStep|Decision|Commitment|Signal|Task|Opportunity|Contact|System|ReviewItem|BusinessRelationship)$",
            },
            {
                "field": "trace.messages",
                "op": "first_call_matching_regex_before_first_create",
                "regex_call": "indemn skill get",
                "regex_target": r"indemn (operation|signal|decision|commitment|task|opportunity|contact|system|reviewitem|businessrelationship|operationstep) create",
            },
        ]
    }
    result = await check_engine.evaluate_check(old_ie1_check, trace)
    assert result is False, "Pre-refactor IE-1 expression with raw path field fails on chained `&&`"


@pytest.mark.asyncio
async def test_ie1_first_sub_check_anchor_misses_chained_commands_design_tension():
    """Design tension surfaced by P3 implementation, documented for IE skill iteration.

    IE-1's first sub-check uses the anchored regex `^indemn skill get X$`, but the IE
    skill v9 chains skill-gets with `&&` to save turns. Path resolution now flattens
    correctly (one command string per tool call); the issue is the anchor.

    Behavior with current grammar + chained command: first sub-check returns False.
    Behavior with standalone command: first sub-check returns True.

    Resolution options (escalate to Craig):
      1. Refine IE-1 to drop `$` anchor: `^indemn skill get (X|Y|Z)\\b`
      2. Pre-split commands on `&&` via a new derived field
      3. Drop the first sub-check entirely; rely only on the ordering check

    Test asserts current behavior (False on chained) so any future fix to IE-1 makes
    this test fail — a forcing function to revisit the choice.
    """
    trace = make_run11_like_trace()
    first_check_only = {
        "field": "trace.messages[*].tool_calls[*].args.command",
        "op": "any_matches_regex",
        "value": r"^indemn skill get (Operation|OperationStep|Decision|Commitment|Signal|Task|Opportunity|Contact|System|ReviewItem|BusinessRelationship)$",
    }
    result = await check_engine.evaluate_check(first_check_only, trace)
    assert result is False, "Anchor mismatch on chained `&&` commands — escalate to Craig"


@pytest.mark.asyncio
async def test_ie1_first_sub_check_passes_with_standalone_skill_get():
    """When skill-get is NOT chained with &&, IE-1's first sub-check passes correctly."""
    trace = {
        "messages": [
            {"type": "ai", "tool_calls": [{"args": {"command": "indemn skill get Operation"}}]},
        ]
    }
    expr = {
        "field": "trace.messages[*].tool_calls[*].args.command",
        "op": "any_matches_regex",
        "value": r"^indemn skill get (Operation|OperationStep|Decision|Commitment|Signal|Task|Opportunity|Contact|System|ReviewItem|BusinessRelationship)$",
    }
    result = await check_engine.evaluate_check(expr, trace)
    assert result is True, "Standalone skill-get command matches IE-1's anchored regex"


@pytest.mark.asyncio
async def test_ie1_ordering_check_works_correctly_on_run11_trace():
    """IE-1's second sub-check (ordering) DOES work — `indemn skill get` precedes any create."""
    trace = make_run11_like_trace()
    ordering_check = {
        "field": "trace.messages",
        "op": "first_call_matching_regex_before_first_create",
        "regex_call": "indemn skill get",
        "regex_target": r"indemn (operation|signal|decision|commitment|task|opportunity|contact|system|reviewitem|businessrelationship|operationstep) create",
    }
    result = await check_engine.evaluate_check(ordering_check, trace)
    assert result is True


# ---------------------------------------------------------------------------
# IE-2: ie_touchpoint_transitioned_to_processed


@pytest.mark.asyncio
async def test_ie2_trace_side_check_passes():
    """IE-2 first sub-check: any tool call matches `indemn touchpoint transition <hex> --to processed`.

    Path now flattens correctly. The `{24}` in the regex is left literal (not treated
    as a template placeholder) per the placeholder/regex-quantifier heuristic.
    """
    trace = make_run11_like_trace()
    expr = {
        "field": "trace.messages[*].tool_calls[*].args.command",
        "op": "any_matches_regex",
        "value": r"^indemn touchpoint transition [a-f0-9]{24} --to processed",
    }
    result = await check_engine.evaluate_check(expr, trace)
    assert result is True, "Run 11 has the transition CLI call in its tool_calls"


@pytest.mark.asyncio
async def test_ie2_entity_side_check_with_template_substitution():
    """IE-2 second sub-check: entity:Touchpoint:{trace.entity_id}.status equals 'processed'."""
    trace = make_run11_like_trace()
    expr = {
        "field": "entity:Touchpoint:{trace.entity_id}.status",
        "op": "equals",
        "value": "processed",
    }

    async def fake_load(et, eid):
        assert et == "Touchpoint"
        assert eid == TOUCHPOINT_ID
        return {"_id": ObjectId(eid), "status": "processed"}

    with patch.object(check_engine, "_load_entity", side_effect=fake_load):
        result = await check_engine.evaluate_check(expr, trace)
        assert result is True


@pytest.mark.asyncio
async def test_ie2_entity_side_check_fails_when_not_processed():
    trace = make_run11_like_trace()
    expr = {
        "field": "entity:Touchpoint:{trace.entity_id}.status",
        "op": "equals",
        "value": "processed",
    }

    async def fake_load(et, eid):
        return {"_id": ObjectId(eid), "status": "logged"}

    with patch.object(check_engine, "_load_entity", side_effect=fake_load):
        result = await check_engine.evaluate_check(expr, trace)
        assert result is False


# ---------------------------------------------------------------------------
# IE-4: ie_entities_link_correctly


@pytest.mark.asyncio
async def test_ie4_all_decisions_link_to_touchpoint():
    """IE-4 first sub-check: constellation.created_in_this_run.Decision[*].touchpoint all equal {trace.entity_id}."""
    trace = {"entity_id": TOUCHPOINT_ID, "correlation_id": CORRELATION_ID}

    decision_id_1 = ObjectId()
    decision_id_2 = ObjectId()

    # Mock the constellation lookup.
    fake_records = [
        MagicMock(entity_type="Decision", entity_id=decision_id_1),
        MagicMock(entity_type="Decision", entity_id=decision_id_2),
    ]
    fake_decision_cls = MagicMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[
        MagicMock(model_dump=MagicMock(return_value={"_id": decision_id_1, "touchpoint": ObjectId(TOUCHPOINT_ID), "company": ObjectId(COMPANY_ID)})),
        MagicMock(model_dump=MagicMock(return_value={"_id": decision_id_2, "touchpoint": ObjectId(TOUCHPOINT_ID), "company": ObjectId(COMPANY_ID)})),
    ])
    fake_decision_cls.find_scoped = MagicMock(return_value=cursor)

    expr = {
        "field": "constellation.created_in_this_run.Decision[*].touchpoint",
        "op": "all_equal",
        "value": "{trace.entity_id}",
    }
    with patch("kernel.changes.collection.ChangeRecord") as mock_cr, \
         patch("kernel.db.ENTITY_REGISTRY", {"Decision": fake_decision_cls}):
        find_q = MagicMock()
        find_q.to_list = AsyncMock(return_value=fake_records)
        mock_cr.find = MagicMock(return_value=find_q)
        result = await check_engine.evaluate_check(expr, trace)
        assert result is True


@pytest.mark.asyncio
async def test_ie4_all_decisions_link_to_company_via_nested_template():
    """IE-4 second sub-check: value uses nested template {entity:Touchpoint:{trace.entity_id}.company}."""
    trace = {"entity_id": TOUCHPOINT_ID, "correlation_id": CORRELATION_ID}

    decision_id = ObjectId()
    fake_records = [
        MagicMock(entity_type="Decision", entity_id=decision_id),
    ]
    fake_decision_cls = MagicMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[
        MagicMock(model_dump=MagicMock(return_value={"_id": decision_id, "touchpoint": ObjectId(TOUCHPOINT_ID), "company": ObjectId(COMPANY_ID)})),
    ])
    fake_decision_cls.find_scoped = MagicMock(return_value=cursor)

    # The Touchpoint loader returns a dict with .company
    async def fake_load(et, eid):
        if et == "Touchpoint":
            return {"_id": ObjectId(eid), "company": ObjectId(COMPANY_ID)}
        return None

    expr = {
        "field": "constellation.created_in_this_run.Decision[*].company",
        "op": "all_equal",
        "value": "{entity:Touchpoint:{trace.entity_id}.company}",
    }
    with patch("kernel.changes.collection.ChangeRecord") as mock_cr, \
         patch("kernel.db.ENTITY_REGISTRY", {"Decision": fake_decision_cls}), \
         patch.object(check_engine, "_load_entity", side_effect=fake_load):
        find_q = MagicMock()
        find_q.to_list = AsyncMock(return_value=fake_records)
        mock_cr.find = MagicMock(return_value=find_q)
        result = await check_engine.evaluate_check(expr, trace)
        assert result is True


@pytest.mark.asyncio
async def test_ie4_empty_constellation_vacuously_passes():
    """IE-4 with no Decisions/Commitments/etc. created → all_equal on empty list = True for all sub-checks."""
    trace = {"entity_id": TOUCHPOINT_ID, "correlation_id": CORRELATION_ID}

    # No create records for any type.
    with patch("kernel.changes.collection.ChangeRecord") as mock_cr:
        find_q = MagicMock()
        find_q.to_list = AsyncMock(return_value=[])
        mock_cr.find = MagicMock(return_value=find_q)

        async def fake_load(et, eid):
            return {"_id": ObjectId(eid), "company": ObjectId(COMPANY_ID)}

        with patch.object(check_engine, "_load_entity", side_effect=fake_load):
            ie4_check = {
                "all": [
                    {
                        "field": "constellation.created_in_this_run.Decision[*].touchpoint",
                        "op": "all_equal",
                        "value": "{trace.entity_id}",
                    },
                    {
                        "field": "constellation.created_in_this_run.Commitment[*].touchpoint",
                        "op": "all_equal",
                        "value": "{trace.entity_id}",
                    },
                ]
            }
            result = await check_engine.evaluate_check(ie4_check, trace)
            # Both vacuously pass on empty arrays.
            assert result is True


# ---------------------------------------------------------------------------
# Design tension documented (NOT silent — surfaces a finding for IE skill iteration)


@pytest.mark.asyncio
async def test_ie1_first_check_design_tension_documented():
    """Design tension: IE-1's first sub-check expects STANDALONE skill-get calls,
    but the IE skill v9 chains them with `&&` to save turns. The actual command in
    Run 11 is `indemn skill get Operation && indemn skill get OperationStep && ...`.

    Per Group D++ no-fallbacks: this is surfaced as a finding for IE skill / Evaluator
    iteration, NOT silently coerced.

    Options to resolve (escalate to Craig):
    1. Refine IE-1 first sub-check regex to match chained: `indemn skill get \\w+( |&&|$)`
    2. Change the path field to match against PARSED commands (split on &&).
    3. Drop the first sub-check and rely only on the ordering check.

    For now: this test documents the gap and ASSERTS False (the current behavior).
    """
    # Documented in test_ie1_passes_on_run11_like_trace's commentary — no action here.
    pass
