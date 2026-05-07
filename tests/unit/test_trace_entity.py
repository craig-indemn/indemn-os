"""Tests for Trace kernel entity — field definitions, state machine, class attributes."""

import inspect
from typing import get_args, get_type_hints

from kernel_entities.trace import Trace


def test_trace_is_kernel_entity():
    assert Trace._is_kernel_entity is True


def test_trace_collection_name():
    assert Trace.Settings.name == "traces"


def test_trace_state_field_name():
    assert Trace._state_field_name == "status"


def test_trace_state_machine_created_to_evaluated():
    assert "evaluated" in Trace._state_machine["created"]


def test_trace_state_machine_evaluated_is_terminal():
    assert Trace._state_machine.get("evaluated") is None or Trace._state_machine.get("evaluated") == []


def test_trace_execution_status_default():
    hints = get_type_hints(Trace, include_extras=True)
    field_info = Trace.model_fields["execution_status"]
    assert field_info.default == "success"


def test_trace_execution_status_values():
    annotation = Trace.model_fields["execution_status"].annotation
    args = get_args(annotation)
    assert "success" in args
    assert "error" in args
    assert "cancelled" in args


def test_trace_status_default():
    field_info = Trace.model_fields["status"]
    assert field_info.default == "created"


def test_trace_status_values():
    annotation = Trace.model_fields["status"].annotation
    args = get_args(annotation)
    assert "created" in args
    assert "evaluated" in args


def test_trace_has_all_spec_fields():
    field_names = set(Trace.model_fields.keys())
    spec_fields = {
        "trace_id", "langsmith_run_id", "session_id",
        "associate_id", "associate_name", "message_id",
        "correlation_id", "entity_type", "entity_id",
        "name", "run_type", "inputs", "outputs",
        "messages", "child_runs", "events",
        "tags", "extra",
        "prompt_tokens", "completion_tokens", "total_tokens",
        "total_cost", "prompt_cost", "completion_cost",
        "start_time", "end_time", "duration_ms", "first_token_time",
        "execution_status", "error",
        "status", "feedback_stats",
    }
    missing = spec_fields - field_names
    assert not missing, f"Missing fields: {missing}"


def test_trace_indexes():
    indexes = Trace.Settings.indexes
    assert len(indexes) >= 5
    index_fields = [tuple(f for f, _ in idx) for idx in indexes]
    assert ("org_id", "associate_id", "created_at") in index_fields
    assert ("org_id", "entity_type", "entity_id") in index_fields
    assert ("org_id", "correlation_id") in index_fields
    assert ("org_id", "status") in index_fields
    assert ("org_id", "execution_status") in index_fields
