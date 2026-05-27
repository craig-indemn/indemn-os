"""Unit tests for `kernel/eval/check_engine.py` path resolution.

Covers trace.*, entity:*, changes:*, example.*, constellation.* prefixes plus
derived paths (trace.tool_call_summary, trace.transition_reason).

Entity / changes / constellation paths use mocked ENTITY_REGISTRY + ChangeRecord
so the tests are unit-level (no MongoDB). The e2e test file exercises the real
MongoDB layer against Run 11's IE trace.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from kernel.eval import check_engine


# ---------------------------------------------------------------------------
# trace.* — top-level fields + array navigation + derived paths


@pytest.mark.asyncio
async def test_trace_top_level_field():
    trace = {"entity_id": "abc", "correlation_id": "cid-1"}
    assert await check_engine.resolve_path("trace.entity_id", {"trace": trace}) == "abc"
    assert await check_engine.resolve_path("trace.correlation_id", {"trace": trace}) == "cid-1"


@pytest.mark.asyncio
async def test_trace_messages_iterate_star_flattens():
    """`[*]` produces a flat list per JSONPath standard. Missing fields are dropped."""
    trace = {
        "messages": [
            {"type": "ai", "tool_calls": [{"name": "execute", "args": {"command": "cmd1"}}]},
            {"type": "ai", "tool_calls": [{"name": "execute", "args": {"command": "cmd2"}}]},
            {"type": "tool", "content": "result"},  # no tool_calls — dropped from iteration
        ]
    }
    cmds = await check_engine.resolve_path(
        "trace.messages[*].tool_calls[*].args.command", {"trace": trace}
    )
    assert cmds == ["cmd1", "cmd2"]


@pytest.mark.asyncio
async def test_trace_messages_index():
    trace = {"messages": [{"type": "ai", "id": "m1"}, {"type": "human", "id": "m2"}]}
    result = await check_engine.resolve_path("trace.messages[0].id", {"trace": trace})
    assert result == "m1"
    result = await check_engine.resolve_path("trace.messages[1].id", {"trace": trace})
    assert result == "m2"


@pytest.mark.asyncio
async def test_trace_messages_filter_predicate():
    trace = {
        "messages": [
            {"type": "ai", "id": "m1"},
            {"type": "human", "id": "m2"},
            {"type": "ai", "id": "m3"},
        ]
    }
    result = await check_engine.resolve_path(
        'trace.messages[?{type:"ai"}].id', {"trace": trace}
    )
    assert result == ["m1", "m3"]


@pytest.mark.asyncio
async def test_trace_missing_field_returns_none():
    trace = {"entity_id": "abc"}
    assert await check_engine.resolve_path("trace.nonexistent", {"trace": trace}) is None


@pytest.mark.asyncio
async def test_trace_tool_call_summary_derived():
    trace = {
        "messages": [
            {
                "type": "ai",
                "tool_calls": [
                    {"id": "tc1", "name": "execute", "args": {"command": "indemn skill get X"}},
                ],
            },
            {"type": "tool", "tool_call_id": "tc1", "content": "[Command succeeded with exit code 0]\n..."},
            {
                "type": "ai",
                "tool_calls": [
                    {"id": "tc2", "name": "execute", "args": {"command": "indemn op create"}},
                ],
            },
            {"type": "tool", "tool_call_id": "tc2", "content": "[Command failed] error: bad data"},
        ]
    }
    summary = await check_engine.resolve_path("trace.tool_call_summary", {"trace": trace})
    assert len(summary) == 2
    assert summary[0]["tool_name"] == "execute"
    assert summary[0]["args"]["command"] == "indemn skill get X"
    assert summary[0]["result_status"] == "success"
    assert summary[1]["result_status"] == "error"


@pytest.mark.asyncio
async def test_trace_transition_reason_derived():
    trace = {
        "messages": [
            {
                "type": "ai",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "name": "execute",
                        "args": {
                            "command": (
                                'indemn touchpoint transition abc123 --to processed '
                                '--reason "Extracted 3 Operations, 1 Signal"'
                            )
                        },
                    }
                ],
            }
        ]
    }
    reason = await check_engine.resolve_path("trace.transition_reason", {"trace": trace})
    assert reason == "Extracted 3 Operations, 1 Signal"


@pytest.mark.asyncio
async def test_trace_transition_reason_none_when_no_transition():
    trace = {"messages": [{"type": "ai", "tool_calls": []}]}
    assert await check_engine.resolve_path("trace.transition_reason", {"trace": trace}) is None


# ---------------------------------------------------------------------------
# trace.tool_call_commands — derived path with chained-command splitting


@pytest.mark.asyncio
async def test_tool_call_commands_splits_on_double_amp():
    """`&&` chaining → separate logical commands."""
    trace = {
        "messages": [
            {
                "type": "ai",
                "tool_calls": [
                    {
                        "args": {
                            "command": (
                                "indemn skill get Operation && "
                                "indemn skill get OperationStep && "
                                "indemn skill get Decision"
                            )
                        }
                    }
                ],
            }
        ]
    }
    cmds = await check_engine.resolve_path("trace.tool_call_commands", {"trace": trace})
    assert cmds == [
        "indemn skill get Operation",
        "indemn skill get OperationStep",
        "indemn skill get Decision",
    ]


@pytest.mark.asyncio
async def test_tool_call_commands_unchained_passes_through():
    """A single command (no `&&`) passes through as a single element."""
    trace = {
        "messages": [
            {"type": "ai", "tool_calls": [{"args": {"command": "indemn operation create --data '{}'"}}]}
        ]
    }
    cmds = await check_engine.resolve_path("trace.tool_call_commands", {"trace": trace})
    assert cmds == ["indemn operation create --data '{}'"]


@pytest.mark.asyncio
async def test_tool_call_commands_handles_or_and_semicolon():
    """`||` and `;` also count as command separators."""
    trace = {
        "messages": [
            {"type": "ai", "tool_calls": [{"args": {"command": "cmd1 ; cmd2 || cmd3"}}]}
        ]
    }
    cmds = await check_engine.resolve_path("trace.tool_call_commands", {"trace": trace})
    assert cmds == ["cmd1", "cmd2", "cmd3"]


@pytest.mark.asyncio
async def test_tool_call_commands_across_multiple_messages_in_order():
    """Commands preserve message + intra-message order."""
    trace = {
        "messages": [
            {"type": "ai", "tool_calls": [{"args": {"command": "cmdA && cmdB"}}]},
            {"type": "human", "content": "..."},  # skipped (not ai)
            {"type": "ai", "tool_calls": [{"args": {"command": "cmdC"}}]},
            {"type": "tool", "content": "result"},  # skipped (not ai)
        ]
    }
    cmds = await check_engine.resolve_path("trace.tool_call_commands", {"trace": trace})
    assert cmds == ["cmdA", "cmdB", "cmdC"]


@pytest.mark.asyncio
async def test_tool_call_commands_anchored_regex_matches_each_split():
    """The whole point of this derived path: anchored regex `^...$` matches each
    chained invocation as a standalone command. This is what IE-1's first sub-check
    needs."""
    trace = {
        "messages": [
            {
                "type": "ai",
                "tool_calls": [
                    {"args": {"command": "indemn skill get Operation && indemn skill get Decision"}}
                ],
            }
        ]
    }
    expr = {
        "field": "trace.tool_call_commands",
        "op": "any_matches_regex",
        "value": r"^indemn skill get (Operation|Decision|Signal)$",
    }
    assert await check_engine.evaluate_check(expr, trace) is True


# ---------------------------------------------------------------------------
# entity:Type:id.field


@pytest.mark.asyncio
async def test_entity_path_load_and_field_access():
    fake_entity = MagicMock()
    fake_entity.model_dump = MagicMock(return_value={"_id": ObjectId(), "company": "co-123", "status": "processed"})
    fake_cls = MagicMock()
    fake_cls.get_scoped = AsyncMock(return_value=fake_entity)
    with patch.object(check_engine, "_load_entity") as mock_load:
        async def _fake(et, eid):
            return {"_id": eid, "company": "co-123", "status": "processed"}
        mock_load.side_effect = _fake
        # entity_type:id.field form
        result = await check_engine.resolve_path(
            "entity:Touchpoint:69ea1bd200000000000000aa.status", {"trace": {}}
        )
        assert result == "processed"


@pytest.mark.asyncio
async def test_entity_path_missing_entity_returns_none():
    with patch.object(check_engine, "_load_entity") as mock_load:
        mock_load.return_value = None
        result = await check_engine.resolve_path(
            "entity:Touchpoint:69ea1bd200000000000000aa.status", {"trace": {}}
        )
        assert result is None


@pytest.mark.asyncio
async def test_entity_path_malformed_raises():
    with pytest.raises(ValueError, match="Type:id"):
        await check_engine.resolve_path("entity:noColon.field", {"trace": {}})


# ---------------------------------------------------------------------------
# _state virtual field — decouples check expressions from entity-specific field names


@pytest.mark.asyncio
async def test_state_virtual_field_resolves_to_actual_state_field():
    """`entity:Type:id._state` resolves to whatever field has is_state_field:true on that entity."""
    with patch.object(check_engine, "_lookup_state_field_name") as mock_lookup, \
         patch.object(check_engine, "_load_entity") as mock_load:
        # Meeting's state field is `stage`
        mock_lookup.return_value = "stage"
        mock_load.return_value = {"_id": ObjectId(), "stage": "processed", "title": "Demo call"}
        result = await check_engine.resolve_path(
            "entity:Meeting:69ea000000000000000000aa._state", {"trace": {}}
        )
        assert result == "processed"
        mock_lookup.assert_called_once_with("Meeting")


@pytest.mark.asyncio
async def test_state_virtual_field_works_across_entity_types():
    """The polymorphic case: same path template, different state field per entity type."""
    test_cases = [
        ("Email", "status", "classified"),
        ("Meeting", "stage", "processed"),
        ("SlackMessage", "status", "received"),
    ]
    for entity_type, state_field, state_value in test_cases:
        async def fake_load(et, eid):
            return {"_id": ObjectId(eid), state_field: state_value}

        def fake_lookup(et):
            return state_field

        with patch.object(check_engine, "_load_entity", side_effect=fake_load), \
             patch.object(check_engine, "_lookup_state_field_name", side_effect=fake_lookup):
            result = await check_engine.resolve_path(
                f"entity:{entity_type}:69ea000000000000000000aa._state", {"trace": {}}
            )
            assert result == state_value, f"Failed for {entity_type}"


@pytest.mark.asyncio
async def test_state_virtual_field_with_polymorphic_substitution():
    """`entity:{trace.entity_type}:{trace.entity_id}._state` — TS-style polymorphic check."""
    trace = {"entity_type": "Meeting", "entity_id": "69ea000000000000000000aa"}
    context = {"trace": trace, "example": None, "experiment": None}

    async def fake_load(et, eid):
        return {"_id": ObjectId(eid), "stage": "processed"}

    def fake_lookup(et):
        return "stage" if et == "Meeting" else "status"

    with patch.object(check_engine, "_load_entity", side_effect=fake_load), \
         patch.object(check_engine, "_lookup_state_field_name", side_effect=fake_lookup):
        result = await check_engine.resolve_path(
            "entity:{trace.entity_type}:{trace.entity_id}._state", context
        )
        assert result == "processed"


@pytest.mark.asyncio
async def test_state_virtual_field_raises_when_entity_has_no_state_field():
    """If an entity has no field marked is_state_field:true, _state resolution raises."""
    with patch.object(check_engine, "_lookup_state_field_name", return_value=None):
        with pytest.raises(ValueError, match="has no state field"):
            await check_engine.resolve_path(
                "entity:Stateless:69ea000000000000000000aa._state", {"trace": {}}
            )


@pytest.mark.asyncio
async def test_state_virtual_field_supports_nested_access():
    """`_state.nested` resolves _state to the actual field name, then descends into nested attrs."""
    with patch.object(check_engine, "_lookup_state_field_name", return_value="stage"), \
         patch.object(check_engine, "_load_entity") as mock_load:
        # Hypothetical: state field is a structured object
        mock_load.return_value = {"_id": ObjectId(), "stage": {"value": "processed", "set_at": "2026-05-21"}}
        result = await check_engine.resolve_path(
            "entity:Meeting:69ea000000000000000000aa._state.value", {"trace": {}}
        )
        assert result == "processed"


# ---------------------------------------------------------------------------
# changes: paths


@pytest.mark.asyncio
async def test_changes_correlation_id_field_projection():
    fake_records = [
        MagicMock(
            model_dump=MagicMock(return_value={
                "correlation_id": "cid-1",
                "entity_type": "Email",
                "entity_id": "e1",
                "change_type": "create",
            }),
        ),
        MagicMock(
            model_dump=MagicMock(return_value={
                "correlation_id": "cid-1",
                "entity_type": "Touchpoint",
                "entity_id": "t1",
                "change_type": "create",
            }),
        ),
    ]
    with patch("kernel.changes.collection.ChangeRecord") as mock_cr:
        find_q = MagicMock()
        find_q.to_list = AsyncMock(return_value=fake_records)
        mock_cr.find = MagicMock(return_value=find_q)
        result = await check_engine.resolve_path(
            "changes:correlation_id=cid-1.entity_type", {"trace": {"correlation_id": "cid-1"}}
        )
        assert result == ["Email", "Touchpoint"]


@pytest.mark.asyncio
async def test_changes_entity_id_field_values():
    rec1 = MagicMock()
    rec1.changes = [MagicMock(field="status", new_value="processed")]
    rec2 = MagicMock()
    rec2.changes = [MagicMock(field="status", new_value="logged")]
    rec3 = MagicMock()
    rec3.changes = [MagicMock(field="company", new_value="x")]  # different field — excluded
    with patch("kernel.changes.collection.ChangeRecord") as mock_cr:
        find_q = MagicMock()
        find_q.to_list = AsyncMock(return_value=[rec1, rec2, rec3])
        mock_cr.find = MagicMock(return_value=find_q)
        result = await check_engine.resolve_path(
            "changes:entity_id=6a14b1249187c4299d7115f7.field=status.values", {"trace": {}}
        )
        assert result == ["processed", "logged"]


@pytest.mark.asyncio
async def test_changes_malformed_raises():
    with pytest.raises(ValueError, match=r"\.field"):
        await check_engine.resolve_path("changes:correlation_id=cid-1", {"trace": {}})
    with pytest.raises(ValueError, match="key=value"):
        await check_engine.resolve_path("changes:no_equals.field", {"trace": {}})


# ---------------------------------------------------------------------------
# example.* paths


@pytest.mark.asyncio
async def test_example_reference_outputs_path():
    example = {"reference_outputs": {"operations": [{"name": "Workflow A"}], "decisions_count": 2}}
    context = {"trace": {}, "example": example}
    result = await check_engine.resolve_path("example.reference_outputs.operations", context)
    assert result == [{"name": "Workflow A"}]
    result = await check_engine.resolve_path("example.reference_outputs.decisions_count", context)
    assert result == 2


@pytest.mark.asyncio
async def test_example_inputs_path():
    example = {"inputs": {"touchpoint_id": "abc"}}
    context = {"trace": {}, "example": example}
    result = await check_engine.resolve_path("example.inputs.touchpoint_id", context)
    assert result == "abc"


@pytest.mark.asyncio
async def test_example_path_when_no_example_returns_none():
    context = {"trace": {}, "example": None}
    result = await check_engine.resolve_path("example.reference_outputs.x", context)
    assert result is None


# ---------------------------------------------------------------------------
# constellation.* paths


@pytest.mark.asyncio
async def test_constellation_entity_counts():
    cid = "cid-1"
    fake_records = [
        MagicMock(entity_type="Decision", entity_id=ObjectId()),
        MagicMock(entity_type="Decision", entity_id=ObjectId()),
        MagicMock(entity_type="Signal", entity_id=ObjectId()),
    ]
    with patch("kernel.changes.collection.ChangeRecord") as mock_cr:
        find_q = MagicMock()
        find_q.to_list = AsyncMock(return_value=fake_records)
        mock_cr.find = MagicMock(return_value=find_q)
        result = await check_engine.resolve_path(
            "constellation.created_in_this_run.entity_counts",
            {"trace": {"correlation_id": cid}},
        )
        assert result == {"Decision": 2, "Signal": 1}


@pytest.mark.asyncio
async def test_constellation_by_entity_type():
    """constellation.created_in_this_run.Decision[*].touchpoint → list of touchpoint values."""
    cid = "cid-1"
    decision_id_1, decision_id_2 = ObjectId(), ObjectId()
    fake_records = [
        MagicMock(entity_type="Decision", entity_id=decision_id_1),
        MagicMock(entity_type="Decision", entity_id=decision_id_2),
        MagicMock(entity_type="Signal", entity_id=ObjectId()),  # different type — excluded
    ]
    fake_decisions = [
        MagicMock(model_dump=MagicMock(return_value={"_id": decision_id_1, "touchpoint": ObjectId("69ea000000000000000000aa")})),
        MagicMock(model_dump=MagicMock(return_value={"_id": decision_id_2, "touchpoint": ObjectId("69ea000000000000000000aa")})),
    ]
    fake_decision_cls = MagicMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=fake_decisions)
    fake_decision_cls.find_scoped = MagicMock(return_value=cursor)

    with patch("kernel.changes.collection.ChangeRecord") as mock_cr, \
         patch("kernel.db.ENTITY_REGISTRY", {"Decision": fake_decision_cls}):
        find_q = MagicMock()
        find_q.to_list = AsyncMock(return_value=fake_records)
        mock_cr.find = MagicMock(return_value=find_q)
        result = await check_engine.resolve_path(
            "constellation.created_in_this_run.Decision[*].touchpoint",
            {"trace": {"correlation_id": cid}},
        )
        # touchpoint values should be the ObjectIds (or their string forms post-normalize).
        # Both decisions have same touchpoint here.
        assert len(result) == 2
        assert all(str(t) == "69ea000000000000000000aa" for t in result)


@pytest.mark.asyncio
async def test_constellation_empty_run_returns_empty():
    """No records for this correlation_id → empty list."""
    with patch("kernel.changes.collection.ChangeRecord") as mock_cr:
        find_q = MagicMock()
        find_q.to_list = AsyncMock(return_value=[])
        mock_cr.find = MagicMock(return_value=find_q)
        result = await check_engine.resolve_path(
            "constellation.created_in_this_run.Decision",
            {"trace": {"correlation_id": "no-such-cid"}},
        )
        assert result == []


@pytest.mark.asyncio
async def test_unknown_prefix_raises():
    with pytest.raises(ValueError, match="Unknown path prefix"):
        await check_engine.resolve_path("unknownprefix.foo", {"trace": {}})
