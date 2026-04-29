"""Tests for kernel.api.errors.register_error_handlers.

The Apr 27 Alliance trace surfaced the root cause of the create-500 family
(Bug #25 Company create, Bug #26 Deal update, Meeting create): any exception
not in the small set of typed handlers (StateMachineError, ValueError, etc.)
falls through to FastAPI's default and returns a literal `Internal Server
Error` string with no body. That makes the entire create flow opaque to
autonomous associates and humans alike — there is no way to self-correct.

This module tests two new handlers plus preserves the existing ones:

  - PydanticValidationError -> 400 with field-level error array
  - catch-all Exception     -> 500 with {error, type, message}

The existing handlers must still take precedence (FastAPI matches the most
specific handler), so a ValueError still returns 400 rather than the 500
catch-all.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from kernel.api.errors import register_error_handlers
from kernel.entity.save import VersionConflictError
from kernel.entity.state_machine import StateMachineError, TransitionValidationError
from kernel.integration.adapter import AdapterValidationError


# --- Test app builder ---


def _build_app_with_routes():
    """Build a minimal app with one route per exception type so we can assert
    the registered handler turns each into the right status + body."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise-state-machine")
    def raise_state_machine():
        raise StateMachineError("invalid transition")

    @app.get("/raise-transition-validation")
    def raise_transition_validation():
        raise TransitionValidationError("transition rejected")

    @app.get("/raise-version-conflict")
    def raise_version_conflict():
        raise VersionConflictError("version mismatch")

    @app.get("/raise-permission")
    def raise_permission():
        raise PermissionError("denied")

    @app.get("/raise-value")
    def raise_value():
        raise ValueError("bad input")

    @app.get("/raise-adapter-validation")
    def raise_adapter_validation():
        raise AdapterValidationError(
            "Unknown params for SomeAdapter.fetch: ['until']. Supported: since."
        )

    @app.get("/raise-pydantic")
    def raise_pydantic():
        # Construct a Pydantic ValidationError by attempting to instantiate
        # a model with invalid data. This is the same shape an entity
        # factory hits when relationship-coercion fails inside an endpoint.
        class _Inner(BaseModel):
            n: int
            name: str

        _Inner.model_validate({"n": "not-an-int"})

    @app.get("/raise-runtime")
    def raise_runtime():
        raise RuntimeError("kaboom")

    @app.get("/raise-key-error")
    def raise_key_error():
        d = {}
        return d["missing"]  # KeyError

    @app.get("/raise-very-long-message")
    def raise_very_long_message():
        raise RuntimeError("x" * 10000)

    return TestClient(app, raise_server_exceptions=False)


# --- Existing typed handlers still work (regression guard) ---


def test_state_machine_error_returns_400():
    client = _build_app_with_routes()
    r = client.get("/raise-state-machine")
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "StateMachineError"
    assert "invalid transition" in body["message"]


def test_transition_validation_error_returns_400():
    client = _build_app_with_routes()
    r = client.get("/raise-transition-validation")
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "TransitionValidationError"
    assert "transition rejected" in body["message"]


def test_version_conflict_returns_409():
    client = _build_app_with_routes()
    r = client.get("/raise-version-conflict")
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "VersionConflict"


def test_adapter_validation_error_returns_400():
    """AdapterValidationError surfaces operator-actionable misuse (Bug #36):
    unknown params, malformed input shape, etc. Must map to 400 (not the
    catch-all 500) so callers can self-correct."""
    client = _build_app_with_routes()
    r = client.get("/raise-adapter-validation")
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "AdapterValidationError"
    assert "Unknown params for SomeAdapter.fetch" in body["message"]
    assert "Supported: since" in body["message"]


def test_permission_error_returns_403():
    client = _build_app_with_routes()
    r = client.get("/raise-permission")
    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "PermissionDenied"


def test_value_error_returns_400_with_validation_error_label():
    """ValueError must continue to map to 400 ValidationError — preserves
    the existing contract so that entity validation paths that raise
    ValueError don't suddenly start returning 500."""
    client = _build_app_with_routes()
    r = client.get("/raise-value")
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "ValidationError"
    assert "bad input" in body["message"]


# --- NEW: Pydantic ValidationError handler ---


def test_pydantic_validation_error_returns_400_with_field_errors():
    """A pydantic.ValidationError raised inside a handler (e.g. when an
    entity factory builds a class and instantiation fails) returns 400
    with a field-level errors array."""
    client = _build_app_with_routes()
    r = client.get("/raise-pydantic")
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "ValidationError"
    # Field-level details must be present and structured.
    assert "errors" in body
    assert isinstance(body["errors"], list)
    assert len(body["errors"]) >= 1
    # Each field error has at least loc + msg fields.
    err = body["errors"][0]
    assert "loc" in err
    assert "msg" in err


# --- NEW: catch-all handler ---


def test_unhandled_runtime_error_returns_500_with_detail():
    """Any exception not in the typed-handler set (RuntimeError, KeyError,
    etc.) returns 500 with type + message — not a literal "Internal Server
    Error" string. This is the root fix for the Apr 27 Alliance-trace
    Meeting create blocker."""
    client = _build_app_with_routes()
    r = client.get("/raise-runtime")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "InternalServerError"
    assert body["type"] == "RuntimeError"
    assert "kaboom" in body["message"]


def test_unhandled_key_error_returns_500_with_detail():
    """KeyError is one of the most common 'crashed inside an endpoint'
    cases. It should surface, not silently return a generic body."""
    client = _build_app_with_routes()
    r = client.get("/raise-key-error")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "InternalServerError"
    assert body["type"] == "KeyError"
    # KeyError's str(exc) is the missing key in repr form.
    assert "missing" in body["message"]


def test_500_message_truncated_to_prevent_huge_responses():
    """A 10K-character message gets truncated. We never want a single 500
    response to balloon to megabytes (e.g., a serialized stack trace
    smuggled into the exception arg)."""
    client = _build_app_with_routes()
    r = client.get("/raise-very-long-message")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "InternalServerError"
    # Message must be present but bounded.
    assert "message" in body
    assert len(body["message"]) <= 4096


def test_500_response_body_is_json_not_string():
    """The previous default returned a plain text body 'Internal Server
    Error'. The new handler must return a JSON object so callers can parse
    it consistently with the typed-error responses."""
    client = _build_app_with_routes()
    r = client.get("/raise-runtime")
    # Must be valid JSON, not a string body.
    body = r.json()
    assert isinstance(body, dict)
    assert "error" in body and "type" in body and "message" in body
