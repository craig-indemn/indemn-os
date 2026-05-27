"""dynamic_params JSON Schema validation (AI-408 Task 3.6).

Mirrors voice-frontdoor's `_validate_parameters` helper — same
Draft202012Validator + `check_schema` defense-in-depth, same MERGED
static + dynamic semantics per §5.4 ("the schema describes the union").

Validation modes:
- `strict` (default) — any validation warning → WebSocket close 1008 with
  code=validation_error
- `forgiving` — warnings logged + session proceeds. Warnings stay
  server-side (matches voice-frontdoor's canonical 4-key success shape;
  no warnings surfaced in the connected payload)

Schema-itself errors (malformed parameter_schema on the Deployment) →
treated as validation_error too, so a bad operator config surfaces
cleanly rather than crashing the worker.

Validation runs BEFORE acts_as so a malformed `actor_id` type is rejected
as a validation_error (operator-actionable) rather than silently passing
through to impersonation-mismatch.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same setup as other AI-408 test files
for _mod_name in list(sys.modules):
    if _mod_name == "starlette" or _mod_name.startswith("starlette."):
        del sys.modules[_mod_name]
_harness_session_stub = MagicMock()
_harness_session_stub.ChatSession = MagicMock()
sys.modules["harness.session"] = _harness_session_stub
if isinstance(sys.modules.get("harness_common.cli"), MagicMock):
    del sys.modules["harness_common.cli"]
import harness_common.cli  # noqa: E402,F401
if isinstance(sys.modules.get("harness_common.jwt_auth"), MagicMock):
    del sys.modules["harness_common.jwt_auth"]
import harness_common.jwt_auth  # noqa: E402,F401

import main as harness_main  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_verify_jwt(monkeypatch):
    """JWT validation isn't this file's concern — stub so all tests can
    proceed past the JWT gate to exercise parameter_schema specifically."""
    monkeypatch.setattr(
        harness_main,
        "_verify_jwt",
        lambda token: {"sub": "act_test", "actor_id": "act_test"},
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_websocket():
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.headers = {"origin": "https://sales.indemn.ai"}
    return ws


def _send_payloads(ws):
    return [c.args[0] for c in ws.send_json.call_args_list]


# Deployment with strict-mode parameter_schema requiring actor_id (string)
_STRICT_DEPLOYMENT = {
    "_id": "dep_strict",
    "status": "active",
    "associate_id": "act_associate",
    "allowed_origins": ["https://sales.indemn.ai"],
    "acts_as": "associate_self",
    "parameter_schema_validation_mode": "strict",
    "static_parameters": {"role": "sales"},
    "parameter_schema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["actor_id"],
        "properties": {
            "actor_id": {"type": "string", "pattern": "^[A-Za-z0-9_]+$"},
            "current_route": {"type": "string"},
            "role": {"type": "string", "enum": ["sales", "support"]},
        },
        "additionalProperties": False,
    },
}


_FORGIVING_DEPLOYMENT = {
    **_STRICT_DEPLOYMENT,
    "_id": "dep_forgiving",
    "parameter_schema_validation_mode": "forgiving",
}


_NO_SCHEMA_DEPLOYMENT = {
    "_id": "dep_no_schema",
    "status": "active",
    "associate_id": "act_associate",
    "allowed_origins": ["https://sales.indemn.ai"],
    "acts_as": "associate_self",
    # No parameter_schema → no validation
}


def _drive(*, deployment, dynamic_params):
    """Helper: drive `_start_deployment_session` with the given Deployment
    + dynamic_params. Returns the mock websocket so callers can inspect
    sent payloads + close-code."""
    ws = _mock_websocket()
    chat_instance = MagicMock()
    chat_instance.start = AsyncMock()
    chat_instance.close = AsyncMock()
    chat_instance.interaction_id = "int_new"

    with patch.object(
        harness_main, "indemn", return_value=deployment
    ), patch.object(
        harness_main, "ChatSession", return_value=chat_instance
    ) as mock_cls:
        result = _run(
            harness_main._start_deployment_session(
                websocket=ws,
                deployment_id=deployment["_id"],
                dynamic_params=dynamic_params,
                auth_token="tok",
                connect_msg={},
            )
        )
    return ws, mock_cls, result


# -----------------------------------------------------------------------------
# Pure helper — _validate_parameters
# -----------------------------------------------------------------------------


class TestValidateParametersHelper:
    def test_no_schema_returns_merged_no_warnings(self):
        deployment = {"static_parameters": {"role": "sales"}}
        merged, warnings = harness_main._validate_parameters(
            deployment, {"actor_id": "act_alice"}
        )
        assert merged == {"role": "sales", "actor_id": "act_alice"}
        assert warnings == []

    def test_dynamic_overrides_static_in_merge(self):
        """When the same key is in both, dynamic wins (user-supplied
        override of operator default)."""
        deployment = {"static_parameters": {"role": "sales"}}
        merged, _ = harness_main._validate_parameters(
            deployment, {"role": "support"}
        )
        assert merged["role"] == "support"

    def test_valid_dynamic_params_pass(self):
        merged, warnings = harness_main._validate_parameters(
            _STRICT_DEPLOYMENT,
            {"actor_id": "act_alice", "current_route": "/x"},
        )
        assert warnings == []
        # Merged includes static (role) + dynamic (actor_id, current_route)
        assert merged["actor_id"] == "act_alice"
        assert merged["role"] == "sales"

    def test_missing_required_field_produces_warning(self):
        _, warnings = harness_main._validate_parameters(
            _STRICT_DEPLOYMENT, {}  # no actor_id
        )
        assert len(warnings) == 1
        assert "actor_id" in warnings[0]

    def test_wrong_type_produces_warning(self):
        _, warnings = harness_main._validate_parameters(
            _STRICT_DEPLOYMENT, {"actor_id": 12345}  # int, not string
        )
        assert len(warnings) >= 1
        # warning text includes the field path
        assert any("actor_id" in w for w in warnings)

    def test_pattern_violation_produces_warning(self):
        _, warnings = harness_main._validate_parameters(
            _STRICT_DEPLOYMENT, {"actor_id": "has-hyphens-not-allowed"}
        )
        assert len(warnings) >= 1

    def test_additional_property_produces_warning(self):
        """additionalProperties:false on the schema → unknown keys warn."""
        _, warnings = harness_main._validate_parameters(
            _STRICT_DEPLOYMENT,
            {"actor_id": "act_alice", "unknown_field": "x"},
        )
        assert len(warnings) >= 1

    def test_malformed_schema_raises_schema_error(self):
        """check_schema fails on malformed schemas — caller catches as
        validation_error (treated like a bad config)."""
        import jsonschema

        deployment = {
            "parameter_schema": {
                "type": "not-a-valid-jsonschema-type",
            },
        }
        with pytest.raises(jsonschema.SchemaError):
            harness_main._validate_parameters(deployment, {})

    def test_static_only_in_merge_when_dynamic_missing(self):
        """Static params alone still validate against the schema (even with
        no dynamic_params, the merge happens)."""
        merged, warnings = harness_main._validate_parameters(
            _STRICT_DEPLOYMENT, {}
        )
        # role is in static; actor_id is missing → warning about actor_id
        assert merged == {"role": "sales"}
        assert any("actor_id" in w for w in warnings)


# -----------------------------------------------------------------------------
# Integration — strict mode rejection in _start_deployment_session
# -----------------------------------------------------------------------------


class TestStrictModeRejection:
    def test_missing_required_actor_id_rejected(self):
        """Required field missing → 1008 validation_error."""
        ws, mock_cls, result = _drive(
            deployment=_STRICT_DEPLOYMENT,
            dynamic_params={},  # no actor_id
        )
        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "validation_error"
        ws.close.assert_called_once_with(code=1008)
        mock_cls.assert_not_called()

    def test_invalid_pattern_rejected(self):
        ws, mock_cls, result = _drive(
            deployment=_STRICT_DEPLOYMENT,
            dynamic_params={"actor_id": "has-hyphens"},
        )
        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors[0]["code"] == "validation_error"
        ws.close.assert_called_once_with(code=1008)

    def test_wrong_type_rejected(self):
        ws, mock_cls, result = _drive(
            deployment=_STRICT_DEPLOYMENT,
            dynamic_params={"actor_id": 12345},  # int, not string
        )
        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors[0]["code"] == "validation_error"

    def test_additional_property_rejected(self):
        ws, mock_cls, result = _drive(
            deployment=_STRICT_DEPLOYMENT,
            dynamic_params={
                "actor_id": "act_alice",
                "unknown_field": "leaked-info",
            },
        )
        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors[0]["code"] == "validation_error"

    def test_valid_params_accepted(self):
        ws, mock_cls, result = _drive(
            deployment=_STRICT_DEPLOYMENT,
            dynamic_params={"actor_id": "act_alice"},
        )
        assert result is not None
        # No errors sent
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        mock_cls.assert_called_once()


# -----------------------------------------------------------------------------
# Integration — forgiving mode + no-schema cases
# -----------------------------------------------------------------------------


class TestForgivingMode:
    def test_invalid_params_pass_through(self, caplog):
        """forgiving mode → invalid params don't reject; session continues."""
        ws, mock_cls, result = _drive(
            deployment=_FORGIVING_DEPLOYMENT,
            dynamic_params={"actor_id": "has-hyphens"},  # would fail strict
        )
        # Session constructed despite validation warnings
        assert result is not None
        # No error sent — forgiving mode logs + proceeds
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []
        mock_cls.assert_called_once()

    def test_valid_params_in_forgiving_mode_pass(self):
        """Smoke: forgiving + valid params → session constructed (no warnings)."""
        ws, mock_cls, result = _drive(
            deployment=_FORGIVING_DEPLOYMENT,
            dynamic_params={"actor_id": "act_alice"},
        )
        assert result is not None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []


class TestNoSchemaDeployment:
    def test_any_params_accepted_when_no_schema(self):
        """Deployment without parameter_schema → no validation; whatever
        the user supplies passes through."""
        ws, mock_cls, result = _drive(
            deployment=_NO_SCHEMA_DEPLOYMENT,
            dynamic_params={"actor_id": 12345, "random": "stuff"},
        )
        assert result is not None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors == []


# -----------------------------------------------------------------------------
# Malformed schema (operator config bug)
# -----------------------------------------------------------------------------


class TestMalformedSchema:
    def test_malformed_schema_rejected_as_validation_error(self):
        """A bad parameter_schema on the Deployment surfaces as
        validation_error (with the SchemaError message) — not a 500 crash."""
        bad_deployment = {
            **_STRICT_DEPLOYMENT,
            "_id": "dep_bad_schema",
            "parameter_schema": {"type": "not-a-real-type"},
        }
        ws, mock_cls, result = _drive(
            deployment=bad_deployment,
            dynamic_params={"actor_id": "act_alice"},
        )
        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert errors[0]["code"] == "validation_error"
        ws.close.assert_called_once_with(code=1008)


# -----------------------------------------------------------------------------
# Validation order — parameter_schema runs BEFORE acts_as
# -----------------------------------------------------------------------------


class TestValidationOrder:
    def test_malformed_actor_id_rejected_before_acts_as_check(self):
        """A wrong-type actor_id (int when schema says string) → validation
        rejects FIRST. acts_as gate never sees it. This matters because the
        acts_as mismatch error is less operator-actionable than a schema
        violation — schema validation tells the operator exactly which field
        is wrong; acts_as just says 'mismatch'."""
        session_actor_strict = {
            **_STRICT_DEPLOYMENT,
            "_id": "dep_session_strict",
            "acts_as": "session_actor",
        }
        ws, mock_cls, result = _drive(
            deployment=session_actor_strict,
            dynamic_params={"actor_id": 99},  # int — fails schema first
        )
        assert result is None
        errors = [p for p in _send_payloads(ws) if p.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "validation_error"  # NOT actor_mismatch
