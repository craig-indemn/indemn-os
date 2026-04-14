"""Unit tests for state machine enforcement."""

import pytest

from kernel.entity.state_machine import (
    StateMachineError,
    _find_state_field,
    validate_and_apply_transition,
)


class FakeEntity:
    """Minimal entity mock for state machine tests."""

    _state_machine = {
        "received": ["triaging"],
        "triaging": ["processing", "awaiting_info"],
        "processing": ["quoted", "declined"],
        "quoted": ["closed"],
        "declined": ["closed"],
    }
    _state_field_name = None
    _is_kernel_entity = False
    _pending_transition = None
    status = "received"

    def _validate_pre_transition(self, target_state):
        pass


class FakeEntityWithStage:
    """Entity that uses 'stage' instead of 'status'."""

    _state_machine = {"new": ["active"], "active": ["closed"]}
    _state_field_name = "stage"
    _is_kernel_entity = False
    _pending_transition = None
    stage = "new"
    status = "some_other_field"  # Should NOT be used as state field

    def _validate_pre_transition(self, target_state):
        pass


class FakeKernelEntity:
    """Kernel entity uses convention (status)."""

    _state_machine = {"active": ["suspended"], "suspended": ["active"]}
    _state_field_name = None  # Kernel entities don't set this
    _is_kernel_entity = True
    _pending_transition = None
    status = "active"

    def _validate_pre_transition(self, target_state):
        pass


def test_valid_transition():
    entity = FakeEntity()
    validate_and_apply_transition(entity, "triaging")
    assert entity.status == "triaging"
    assert entity._pending_transition == {
        "from": "received",
        "to": "triaging",
        "reason": None,
    }


def test_valid_transition_with_reason():
    entity = FakeEntity()
    validate_and_apply_transition(entity, "triaging", reason="ready for review")
    assert entity._pending_transition["reason"] == "ready for review"


def test_invalid_transition_raises():
    entity = FakeEntity()
    with pytest.raises(StateMachineError, match="Cannot transition"):
        validate_and_apply_transition(entity, "closed")


def test_skip_states_rejected():
    entity = FakeEntity()
    with pytest.raises(StateMachineError):
        validate_and_apply_transition(entity, "quoted")  # Can't skip triaging→processing


def test_multi_step_transitions():
    entity = FakeEntity()
    validate_and_apply_transition(entity, "triaging")
    entity._pending_transition = None  # Clear as save_tracked would
    validate_and_apply_transition(entity, "processing")
    assert entity.status == "processing"


def test_no_state_machine_allows_all():
    entity = FakeEntity()
    entity._state_machine = None
    validate_and_apply_transition(entity, "anything")
    # No error — no state machine means no enforcement


def test_find_state_field_with_state_field_name():
    entity = FakeEntityWithStage()
    assert _find_state_field(entity) == "stage"


def test_find_state_field_kernel_convention():
    entity = FakeKernelEntity()
    assert _find_state_field(entity) == "status"


def test_stage_entity_transitions_correctly():
    entity = FakeEntityWithStage()
    validate_and_apply_transition(entity, "active")
    assert entity.stage == "active"  # stage changed, not status
    assert entity.status == "some_other_field"  # status unchanged


def test_kernel_entity_transition():
    entity = FakeKernelEntity()
    validate_and_apply_transition(entity, "suspended")
    assert entity.status == "suspended"
