"""Unit tests for Deployment kernel entity (AI-404-1).

Structure: class-attribute introspection (matches test_trace_entity.py
convention). Beanie Document subclasses cannot be instantiated via
`Deployment(...)` without init_beanie() — `Document.__init__` calls
`get_motor_collection()`.

Behavior: Pydantic v2 `model_construct(...)` bypasses Document.__init__
entirely (constructs without running validators). Tests then invoke the
specific model_validator method directly. This lets us verify validator
behavior in unit tests without a real MongoDB connection.
"""

from typing import get_args

import pytest
from bson import ObjectId

from kernel_entities.deployment import Deployment


def _make_deployment(**overrides) -> Deployment:
    """Build a Deployment via model_construct (skips all validators).

    Tests invoke `d._derive_acts_as_and_validate()` or `d._derive_validation_mode()`
    directly on the returned instance to exercise the specific validator
    under test.
    """
    defaults = dict(
        org_id=ObjectId(),
        name="Test",
        associate_id=ObjectId(),
        runtime_id=ObjectId(),
        parameter_schema={},
        acts_as=None,
        parameter_schema_validation_mode=None,
    )
    defaults.update(overrides)
    return Deployment.model_construct(**defaults)


def test_deployment_can_import():
    """Deployment class can be imported."""
    assert Deployment is not None


def test_deployment_required_fields():
    """name, associate_id, runtime_id are required (no Pydantic default)."""
    for field_name in ("name", "associate_id", "runtime_id"):
        assert Deployment.model_fields[field_name].is_required(), (
            f"{field_name} should be required"
        )


def test_deployment_status_default():
    """status defaults to 'configured' (§5.7 state machine entry)."""
    assert Deployment.model_fields["status"].default == "configured"


def test_deployment_acts_as_literal_values():
    """acts_as is Optional[Literal['session_actor', 'associate_self']].

    Optional so the derivation model_validator (Task 1.2) can fill it from
    parameter_schema per §5.6.
    """
    annotation = Deployment.model_fields["acts_as"].annotation
    # Optional[Literal[...]] is Union[Literal[...], NoneType]
    union_args = get_args(annotation)
    literal_arg = next(a for a in union_args if a is not type(None))
    literal_values = get_args(literal_arg)
    assert set(literal_values) == {"session_actor", "associate_self"}


def test_deployment_is_kernel_entity_marker():
    """Deployment is marked as a kernel entity (kernel ships it; per-org overrides forbidden)."""
    assert Deployment._is_kernel_entity is True


def test_deployment_settings_collection_name():
    """Beanie Settings.name is 'deployments' (matches auto-route /api/deployments/)."""
    assert Deployment.Settings.name == "deployments"


# --- Task 1.2 — model_validator registration (existence checks; behavior verified
#     by Task 1.10.5's sample_deployment fixture once integration tests land) ---


def test_deployment_has_derive_acts_as_and_validate_validator():
    """`_derive_acts_as_and_validate` derives acts_as from parameter_schema (§5.6)
    AND enforces the session_actor+actor_id-required consistency check."""
    mvs = Deployment.__pydantic_decorators__.model_validators
    assert "_derive_acts_as_and_validate" in mvs, (
        "Deployment must register a _derive_acts_as_and_validate model_validator"
    )
    assert mvs["_derive_acts_as_and_validate"].info.mode == "after"


def test_deployment_has_derive_validation_mode_validator():
    """`_derive_validation_mode` runs after _derive_acts_as to fill the mode field (§5.4)."""
    mvs = Deployment.__pydantic_decorators__.model_validators
    assert "_derive_validation_mode" in mvs, (
        "Deployment must register a _derive_validation_mode model_validator"
    )
    assert mvs["_derive_validation_mode"].info.mode == "after"


# --- Task 1.2 — model_validator BEHAVIOR
#     via Pydantic model_construct + manual invocation (avoids Beanie init while
#     still exercising real validator logic). Each test invokes the validator
#     under test on a model_construct'd instance and asserts the resulting state.


def test_derive_acts_as_session_actor_when_actor_id_required():
    """parameter_schema requires actor_id → acts_as defaults to session_actor (§5.6)."""
    d = _make_deployment(
        parameter_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["actor_id"],
            "properties": {"actor_id": {"type": "string"}},
        },
    )
    d._derive_acts_as_and_validate()
    assert d.acts_as == "session_actor"


def test_derive_acts_as_associate_self_when_actor_id_not_required():
    """parameter_schema does NOT require actor_id → acts_as defaults to associate_self."""
    d = _make_deployment(
        parameter_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
        },
    )
    d._derive_acts_as_and_validate()
    assert d.acts_as == "associate_self"


def test_derive_acts_as_associate_self_when_no_schema():
    """Empty parameter_schema → acts_as defaults to associate_self."""
    d = _make_deployment(parameter_schema={})
    d._derive_acts_as_and_validate()
    assert d.acts_as == "associate_self"


def test_derive_acts_as_explicit_override_wins():
    """Operator-supplied acts_as is preserved (validator does not overwrite)."""
    d = _make_deployment(
        parameter_schema={"required": ["actor_id"]},
        acts_as="associate_self",
    )
    d._derive_acts_as_and_validate()
    assert d.acts_as == "associate_self"


def test_derive_validation_mode_strict_for_session_actor():
    """acts_as=session_actor → validation_mode defaults to strict (§5.4)."""
    d = _make_deployment(acts_as="session_actor")
    d._derive_validation_mode()
    assert d.parameter_schema_validation_mode == "strict"


def test_derive_validation_mode_forgiving_for_associate_self():
    """acts_as=associate_self → validation_mode defaults to forgiving."""
    d = _make_deployment(acts_as="associate_self")
    d._derive_validation_mode()
    assert d.parameter_schema_validation_mode == "forgiving"


def test_derive_validation_mode_explicit_override_wins():
    """Operator-supplied validation_mode is preserved (validator does not overwrite)."""
    d = _make_deployment(
        acts_as="session_actor",
        parameter_schema_validation_mode="forgiving",
    )
    d._derive_validation_mode()
    assert d.parameter_schema_validation_mode == "forgiving"


# --- Task 1.3 — acts_as=session_actor consistency check
#     §5.6 + implementation-readiness scrub: if operator explicitly sets
#     acts_as=session_actor but parameter_schema does NOT require actor_id,
#     reject at save time (runtime's session-start gate would otherwise have
#     nothing to validate). Validator renamed to _derive_acts_as_and_validate
#     to signal it does both derivation + consistency enforcement.


def test_derive_rejects_session_actor_without_actor_id_in_schema_required():
    """acts_as=session_actor + parameter_schema missing actor_id → ValueError (§5.6)."""
    d = _make_deployment(
        acts_as="session_actor",
        parameter_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            # actor_id NOT in required
        },
    )
    with pytest.raises(ValueError, match="actor_id"):
        d._derive_acts_as_and_validate()


def test_derive_associate_self_without_actor_id_ok():
    """acts_as=associate_self does not require actor_id in schema — no raise."""
    d = _make_deployment(acts_as="associate_self")
    d._derive_acts_as_and_validate()
    assert d.acts_as == "associate_self"
