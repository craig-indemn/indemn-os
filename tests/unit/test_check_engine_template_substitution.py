"""Unit tests for template substitution in `kernel/eval/check_engine.py`.

Covers all four Group E grammar extensions (2026-05-26):
  E1 — count dual-mode (tested in test_check_engine_aggregations.py)
  E2 — subscript-then-field substitution on constellation arrays
  E3 — entity-type-slot substitution in entity:* prefix
  E4 — null as valid value in equality ops
Plus the original D-C grammar examples:
  - Bare-value substitution
  - id-slot substitution
  - Nested entity-load substitution
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from kernel.eval import check_engine


# ---------------------------------------------------------------------------
# Bare-value substitution (D-C base grammar)


@pytest.mark.asyncio
async def test_bare_value_substitution():
    """{trace.entity_id} resolves to the trace's entity_id field."""
    trace = {"entity_id": "abc123"}
    context = {"trace": trace, "example": None, "experiment": None}
    result = await check_engine.substitute_template("{trace.entity_id}", context)
    assert result == "abc123"


@pytest.mark.asyncio
async def test_substitution_preserves_non_string_scalars():
    """If a placeholder spans the entire input, the raw scalar (not str()) is returned."""
    trace = {"correlation_id": "cid-1", "count": 42}
    context = {"trace": trace, "example": None, "experiment": None}
    result = await check_engine.substitute_template("{trace.count}", context)
    assert result == 42
    assert isinstance(result, int)


@pytest.mark.asyncio
async def test_substitution_in_mixed_text():
    """Mixed text + placeholder concatenates string representations."""
    trace = {"entity_id": "abc"}
    context = {"trace": trace, "example": None, "experiment": None}
    result = await check_engine.substitute_template("prefix-{trace.entity_id}-suffix", context)
    assert result == "prefix-abc-suffix"


# ---------------------------------------------------------------------------
# id-slot substitution


@pytest.mark.asyncio
async def test_id_slot_substitution_in_entity_path():
    """`entity:Touchpoint:{trace.entity_id}.company` substitutes the id, then resolves the entity."""
    trace = {"entity_id": "69ea000000000000000000aa"}
    context = {"trace": trace, "example": None, "experiment": None}

    async def fake_load(et, eid):
        assert et == "Touchpoint"
        assert eid == "69ea000000000000000000aa"
        return {"_id": ObjectId(eid), "company": "co-123"}

    with patch.object(check_engine, "_load_entity", side_effect=fake_load):
        result = await check_engine.resolve_path(
            "entity:Touchpoint:{trace.entity_id}.company", context
        )
        assert result == "co-123"


# ---------------------------------------------------------------------------
# Nested entity-load substitution (D-C example shown in IE-4)


@pytest.mark.asyncio
async def test_nested_entity_load_substitution():
    """`{entity:Touchpoint:{trace.entity_id}.company}` resolves the whole entity-load
    as a substitution value (used in IE-4's value field).
    """
    trace = {"entity_id": "69ea000000000000000000aa"}
    context = {"trace": trace, "example": None, "experiment": None}

    async def fake_load(et, eid):
        assert et == "Touchpoint"
        assert eid == "69ea000000000000000000aa"
        return {"_id": ObjectId(eid), "company": "co-999"}

    with patch.object(check_engine, "_load_entity", side_effect=fake_load):
        result = await check_engine.substitute_template(
            "{entity:Touchpoint:{trace.entity_id}.company}", context
        )
        assert result == "co-999"


@pytest.mark.asyncio
async def test_ie4_value_template_resolves_to_company_id():
    """Exercises the exact template shape used in IE-4's value field."""
    trace = {"entity_id": "69ea000000000000000000aa"}
    context = {"trace": trace, "example": None, "experiment": None}

    async def fake_load(et, eid):
        return {"_id": ObjectId(eid), "company": "co-real-id"}

    with patch.object(check_engine, "_load_entity", side_effect=fake_load):
        value_template = "{entity:Touchpoint:{trace.entity_id}.company}"
        resolved = await check_engine.substitute_template(value_template, context)
        assert resolved == "co-real-id"


# ---------------------------------------------------------------------------
# Group E E3 — entity-type-slot substitution in path prefix


@pytest.mark.asyncio
async def test_e3_entity_type_substitution():
    """entity:{trace.entity_type}:{trace.entity_id}.status for polymorphic associates."""
    trace = {"entity_type": "Email", "entity_id": "69ea000000000000000000bb"}
    context = {"trace": trace, "example": None, "experiment": None}

    calls = []

    async def fake_load(et, eid):
        calls.append((et, eid))
        return {"_id": ObjectId(eid), "status": "classified"}

    with patch.object(check_engine, "_load_entity", side_effect=fake_load):
        result = await check_engine.resolve_path(
            "entity:{trace.entity_type}:{trace.entity_id}.status", context
        )
        assert result == "classified"
        assert calls == [("Email", "69ea000000000000000000bb")]


@pytest.mark.asyncio
async def test_e3_polymorphic_dispatches_per_source_type():
    """Same path template against different trace.entity_type values dispatches per-type.

    This validates the polymorphic source-agnostic associate pattern (TS, CE, PH).
    """
    test_cases = [
        ("Email", "69ea000000000000000000b1"),
        ("Meeting", "69ea000000000000000000b2"),
        ("SlackMessage", "69ea000000000000000000b3"),
    ]
    for entity_type, entity_id in test_cases:
        trace = {"entity_type": entity_type, "entity_id": entity_id}
        context = {"trace": trace, "example": None, "experiment": None}

        async def fake_load(et, eid):
            return {"_id": ObjectId(eid), "status": "processed"}

        with patch.object(check_engine, "_load_entity", side_effect=fake_load):
            result = await check_engine.resolve_path(
                "entity:{trace.entity_type}:{trace.entity_id}.status", context
            )
            assert result == "processed"


# ---------------------------------------------------------------------------
# Group E E2 — subscript-then-field substitution


@pytest.mark.asyncio
async def test_e2_subscript_then_field_substitution():
    """{constellation.created_in_this_run.Deal[0]._id} resolves to first Deal's _id as scalar."""
    cid = "cid-xyz"
    deal_id = ObjectId("69ea000000000000000000c1")

    # Mock the constellation lookup path.
    async def fake_resolve(path: str, ctx: dict):
        if path == "constellation.created_in_this_run.Deal[0]._id":
            return deal_id
        if path == "trace.entity_id":
            return "abc"
        raise AssertionError(f"unexpected path: {path}")

    with patch.object(check_engine, "resolve_path", side_effect=fake_resolve):
        result = await check_engine.substitute_template(
            "{constellation.created_in_this_run.Deal[0]._id}", {"trace": {}, "example": None, "experiment": None}
        )
        assert result == deal_id  # raw scalar (ObjectId), not str — single-placeholder case


# ---------------------------------------------------------------------------
# Group E E4 — null as valid value in equality ops


@pytest.mark.asyncio
async def test_e4_null_as_target_in_equals():
    """`{"op": "equals", "value": null}` checks "field IS null"."""
    trace = {"some_field": None}
    expr = {"field": "trace.some_field", "op": "equals", "value": None}
    assert await check_engine.evaluate_check(expr, trace) is True

    trace = {"some_field": "x"}
    assert await check_engine.evaluate_check(expr, trace) is False


@pytest.mark.asyncio
async def test_e4_null_as_target_in_not_equals():
    trace = {"some_field": "x"}
    expr = {"field": "trace.some_field", "op": "not_equals", "value": None}
    assert await check_engine.evaluate_check(expr, trace) is True


@pytest.mark.asyncio
async def test_e4_null_as_target_in_none_match_equals():
    """TS-1 pattern: assert "no Touchpoint has null source_entity_id"."""
    trace = {"items": ["a", "b", "c"]}
    expr = {"field": "trace.items", "op": "none_match_equals", "value": None}
    assert await check_engine.evaluate_check(expr, trace) is True

    trace = {"items": ["a", None, "c"]}
    assert await check_engine.evaluate_check(expr, trace) is False


@pytest.mark.asyncio
async def test_e4_null_value_via_template_substitution():
    """If `{...}` resolves to None, downstream equality op should use it as null target."""
    trace = {"missing": None, "items": [None, None]}
    expr = {"field": "trace.items", "op": "all_equal", "value": "{trace.missing}"}
    # {trace.missing} resolves to None; all elements are None; all_equal vs None → True
    assert await check_engine.evaluate_check(expr, trace) is True


# ---------------------------------------------------------------------------
# Error cases


@pytest.mark.asyncio
async def test_substitution_to_list_at_scalar_position_raises():
    """Per Group D++ no-fallbacks: list at scalar position is a runtime error."""
    trace = {"my_list": ["a", "b"]}
    context = {"trace": trace, "example": None, "experiment": None}
    with pytest.raises(ValueError, match="scalar"):
        # The placeholder resolves to a list, but it's a fragment (not whole input)
        # so it can't be coerced to a string scalar.
        await check_engine.substitute_template("prefix-{trace.my_list}-suffix", context)


@pytest.mark.asyncio
async def test_substitution_innermost_first_order():
    """Innermost {} resolves before outer (left-to-right by regex match position)."""
    trace = {"a": "ENTITY", "b": "test-id"}
    context = {"trace": trace, "example": None, "experiment": None}

    async def fake_load(et, eid):
        # Validate the inner substitutions both fired before outer.
        assert et == "ENTITY"
        assert eid == "test-id"
        return {"_id": ObjectId("69ea000000000000000000aa"), "status": "x"}

    with patch.object(check_engine, "_load_entity", side_effect=fake_load):
        result = await check_engine.resolve_path(
            "entity:{trace.a}:{trace.b}.status", context
        )
        assert result == "x"
