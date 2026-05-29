"""Pin CLI surfaces per-field validation error detail.

Discovered during IE drain: agent tried `indemn signal create` without the
required `description` field, API returned 400 with detail naming the
missing field, BUT the CLI's `_handle_error` only showed:

    Error 400: 1 validation error(s)

Stripping the `errors` array (or FastAPI's `detail` array). The agent
retried the exact same call (no new info to act on), failed again, gave
up. Operator triaging via CLI was similarly blind.

Fix: when the response body carries an `errors` (OS shape) or `detail`
(FastAPI shape) list, append per-field detail under the summary line.

These tests pin that both shapes surface, that the legacy single-line
output still works when there's no errors array, and that multi-error
responses format each error on its own line.
"""

import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _make_response(status_code: int, body: dict):
    """Build a minimal httpx-like Response stand-in."""
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


def _capture_stderr(client, response):
    """Call _handle_error and capture the stderr output."""
    from indemn_os.client import CLIClient as _CLIClient
    if not isinstance(client, _CLIClient):
        # Fresh CLIClient with stubbed env
        with patch.dict(os.environ, {"INDEMN_SERVICE_TOKEN": "tok"}):
            client = _CLIClient()
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        try:
            client._handle_error(response)
        except SystemExit:
            pass
    return buf.getvalue()


@pytest.fixture
def client():
    from indemn_os.client import CLIClient
    with patch.dict(os.environ, {"INDEMN_SERVICE_TOKEN": "tok"}):
        return CLIClient()


def test_os_api_errors_array_surfaces_field_detail(client):
    """OS API error shape: body.errors carries [{loc, msg, type}] — surface each."""
    body = {
        "error": "ValidationError",
        "message": "1 validation error(s)",
        "errors": [
            {"loc": ["description"], "msg": "Field required", "type": "missing"}
        ],
    }
    out = _capture_stderr(client, _make_response(400, body))
    assert "Error 400:" in out
    assert "1 validation error(s)" in out
    assert "description: Field required" in out
    assert "(missing)" in out


def test_fastapi_detail_array_surfaces_field_detail(client):
    """FastAPI validation errors use `detail` instead of `errors` — same handling."""
    body = {
        "detail": [
            {"loc": ["body", "company"], "msg": "field required", "type": "value_error.missing"}
        ],
    }
    out = _capture_stderr(client, _make_response(422, body))
    assert "Error 422:" in out
    assert "body.company: field required" in out


def test_multiple_errors_render_on_separate_lines(client):
    """Multi-error response: each error on its own line under the summary."""
    body = {
        "message": "3 validation error(s)",
        "errors": [
            {"loc": ["description"], "msg": "Field required", "type": "missing"},
            {"loc": ["severity"], "msg": "Invalid enum value", "type": "value_error"},
            {"loc": ["company"], "msg": "ObjectId expected", "type": "type_error"},
        ],
    }
    out = _capture_stderr(client, _make_response(400, body))
    assert "description: Field required" in out
    assert "severity: Invalid enum value" in out
    assert "company: ObjectId expected" in out
    # Each on its own line — not all crammed in one
    detail_lines = [ln for ln in out.split("\n") if ln.startswith("  ")]
    assert len(detail_lines) == 3


def test_simple_message_only_unchanged(client):
    """Backward-compat: when no errors/detail array, output is still the single line."""
    body = {"message": "Forbidden"}
    out = _capture_stderr(client, _make_response(403, body))
    assert "Error 403: Forbidden" in out
    # No newlines after "Forbidden" (no detail to add)
    assert out.count("\n") == 1  # just the final newline


def test_non_json_response_falls_back_to_text(client):
    """When the body isn't valid JSON, fall back to raw response.text."""
    r = MagicMock()
    r.status_code = 500
    r.json.side_effect = ValueError("not json")
    r.text = "<html>Internal Server Error</html>"
    out = _capture_stderr(client, r)
    assert "Error 500:" in out
    assert "Internal Server Error" in out


def test_nested_loc_path_joined_with_dots(client):
    """Pydantic nested validation errors have multi-element loc — join with dots."""
    body = {
        "message": "1 validation error(s)",
        "errors": [
            {"loc": ["body", "address", "zip"], "msg": "Invalid format", "type": "value_error"}
        ],
    }
    out = _capture_stderr(client, _make_response(400, body))
    assert "body.address.zip: Invalid format" in out
