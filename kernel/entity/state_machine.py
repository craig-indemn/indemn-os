"""State machine enforcement for entities.

Validates that transitions follow the defined state machine.
Stores transition metadata for event emission.
Does NOT save — the caller must call save_tracked().
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kernel.entity.base import BaseEntity


class StateMachineError(Exception):
    """Raised when an invalid state transition is attempted."""

    pass


class TransitionValidationError(Exception):
    """Raised by pre-transition validation hooks."""

    pass


def validate_and_apply_transition(
    entity: "BaseEntity", target_state: str, reason: str = None
):
    """Validate transition, run pre-transition hooks, apply state change.
    Does NOT save — caller must call save_tracked()."""
    sm = entity._state_machine
    if sm is None:
        return  # No state machine — all transitions allowed

    # Find the state field
    state_field = _find_state_field(entity)
    current_state = getattr(entity, state_field, None)

    # Check if transition is allowed
    valid_transitions = sm.get(current_state, [])
    if target_state not in valid_transitions:
        raise StateMachineError(
            f"Cannot transition {type(entity).__name__} from '{current_state}' "
            f"to '{target_state}'. Valid transitions: {valid_transitions}"
        )

    # Pre-transition validation hook (subclass override)
    entity._validate_pre_transition(target_state)

    # Store transition metadata for event emission
    entity._pending_transition = {
        "from": current_state,
        "to": target_state,
        "reason": reason,
    }

    # Apply the transition
    setattr(entity, state_field, target_state)


def _find_state_field(entity: "BaseEntity") -> str:
    """Find the field controlled by the state machine.

    For domain entities: uses _state_field_name set by factory.py from is_state_field
    flag on the EntityDefinition.
    For kernel entities: uses _state_field_name class variable set on the entity class.

    No convention-based fallback — the field must be explicitly declared.
    """
    state_field = getattr(type(entity), "_state_field_name", None)
    if state_field:
        return state_field
    raise StateMachineError(
        f"{type(entity).__name__} has a state machine but no _state_field_name configured. "
        f"Kernel entities must set _state_field_name as a class variable. "
        f"Domain entities must have is_state_field=True on their state field "
        f"in the EntityDefinition."
    )
