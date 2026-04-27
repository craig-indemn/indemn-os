"""Tests for the effective_actor_id forensics chain (Bug #22).

Before this fix, every associate authenticated as Platform Admin via the
shared runtime service token. Changes collection recorded `actor_id =
Platform Admin` for every mutation — indistinguishable across the
Email Classifier, Touchpoint Synthesizer, and Intelligence Extractor.
We hit it during the 446-Company explosion: couldn't tell which
associate created which dupes.

The fix: a parallel field `effective_actor_id` on ChangeRecord, set via
the `current_effective_actor_id` contextvar, populated from the
`X-Effective-Actor-Id` HTTP header, propagated from the
`INDEMN_EFFECTIVE_ACTOR_ID` env var that the harness sets per activity.
Mirrors the existing causation_message_id propagation pattern.

These tests pin: (1) the contextvar exists and defaults to None,
(2) ChangeRecord accepts the optional field, (3) the CLI client adds
the header from env var, (4) the harness CLI wrapper propagates the
env var to subprocess. The middleware path is exercised by integration
tests against a live MongoDB; here we keep it unit-level.
"""

import os
from unittest.mock import patch

import pytest

from kernel.changes.collection import ChangeRecord
from kernel.context import current_effective_actor_id


# --- Contextvar ---


def test_contextvar_defaults_to_none():
    """Fresh request → no effective actor asserted → contextvar reads None."""
    assert current_effective_actor_id.get() is None


def test_contextvar_set_and_read():
    """Set it, read it, reset it. Standard contextvar mechanics — guards
    against accidental rename or removal."""
    token = current_effective_actor_id.set("69e23d586a448759a34d3824")
    try:
        assert current_effective_actor_id.get() == "69e23d586a448759a34d3824"
    finally:
        current_effective_actor_id.reset(token)
    assert current_effective_actor_id.get() is None


# --- ChangeRecord schema ---
# ChangeRecord is a Beanie Document and can't be instantiated outside
# init_beanie context (raises CollectionWasNotInitialized). We assert on
# the Pydantic-level field metadata instead — same surface, no Beanie init.


def test_change_record_has_effective_actor_id_field():
    """The field exists on the model so it survives schema queries +
    accepts values from save_tracked. Guard against accidental rename
    or removal."""
    assert "effective_actor_id" in ChangeRecord.model_fields


def test_change_record_effective_actor_id_is_optional():
    """Old call sites that don't pass effective_actor_id still construct
    valid records (default None). Backward-compatible."""
    field_info = ChangeRecord.model_fields["effective_actor_id"]
    assert field_info.is_required() is False
    assert field_info.default is None


def test_change_record_actor_id_and_effective_actor_id_are_independent_fields():
    """The whole point of the fix: actor_id and effective_actor_id are
    distinct fields. A query can index either."""
    assert "actor_id" in ChangeRecord.model_fields
    assert "effective_actor_id" in ChangeRecord.model_fields
    # The compound forensics index ((org_id, effective_actor_id, timestamp))
    # is what makes per-associate queries cheap; verify it's declared.
    indexes = ChangeRecord.Settings.indexes
    has_eff_index = any(
        any(field == "effective_actor_id" for field, _ in idx)
        for idx in indexes
        if isinstance(idx, list)
    )
    assert has_eff_index, "Expected a compound index on effective_actor_id"


# --- CLI client header propagation ---


def test_cli_client_adds_effective_actor_header_from_env():
    """When INDEMN_EFFECTIVE_ACTOR_ID is set, the CLI client adds the
    X-Effective-Actor-Id header. This is the link from
    harness env var → API request header → middleware → contextvar."""
    from indemn_os.client import CLIClient

    with patch.dict(os.environ, {"INDEMN_EFFECTIVE_ACTOR_ID": "69abc...", "INDEMN_SERVICE_TOKEN": "tok"}):
        client = CLIClient()
        h = client._headers()
    assert h.get("X-Effective-Actor-Id") == "69abc..."


def test_cli_client_omits_effective_actor_header_when_env_unset():
    """No env var → no header. Don't send `X-Effective-Actor-Id: None`."""
    from indemn_os.client import CLIClient

    env = {k: v for k, v in os.environ.items() if k != "INDEMN_EFFECTIVE_ACTOR_ID"}
    env["INDEMN_SERVICE_TOKEN"] = "tok"
    with patch.dict(os.environ, env, clear=True):
        client = CLIClient()
        h = client._headers()
    assert "X-Effective-Actor-Id" not in h


def test_cli_client_passes_both_causation_and_effective_actor():
    """Causation and effective-actor are independent — both can be set on
    the same request. Regression guard against a refactor that picks one."""
    from indemn_os.client import CLIClient

    with patch.dict(
        os.environ,
        {
            "INDEMN_CAUSATION_MESSAGE_ID": "msg_69abc",
            "INDEMN_EFFECTIVE_ACTOR_ID": "69actor",
            "INDEMN_SERVICE_TOKEN": "tok",
        },
    ):
        client = CLIClient()
        h = client._headers()
    assert h["X-Causation-Message-ID"] == "msg_69abc"
    assert h["X-Effective-Actor-Id"] == "69actor"


# --- Harness CLI wrapper env-var propagation ---


def test_harness_cli_wrapper_propagates_effective_actor_to_subprocess(monkeypatch):
    """The harness's `indemn()` shells to subprocess. The subprocess starts
    with a clean env; the wrapper explicitly forwards specific keys.
    INDEMN_EFFECTIVE_ACTOR_ID must be one of them — otherwise the CLI
    client never sees it and never adds the header."""
    import sys
    from pathlib import Path

    # harness_common is namespace-packaged into harnesses/_base/
    HARNESS_BASE = Path(__file__).resolve().parents[2] / "harnesses" / "_base"
    if str(HARNESS_BASE) not in sys.path:
        sys.path.insert(0, str(HARNESS_BASE))

    from harness_common import cli as harness_cli

    captured_env = {}

    class FakeResult:
        returncode = 0
        stdout = b'{"ok": true}'
        stderr = b""

    def fake_run(cmd, env, capture_output, timeout, check):
        captured_env.update(env)
        return FakeResult()

    monkeypatch.setattr(harness_cli.subprocess, "run", fake_run)
    monkeypatch.setenv("INDEMN_API_URL", "http://x")
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("INDEMN_EFFECTIVE_ACTOR_ID", "69eff...")

    harness_cli.indemn("actor", "list")

    assert captured_env.get("INDEMN_EFFECTIVE_ACTOR_ID") == "69eff..."


def test_harness_cli_wrapper_does_not_propagate_when_unset(monkeypatch):
    """When the env var isn't set, don't add it to the subprocess env at all."""
    import sys
    from pathlib import Path

    HARNESS_BASE = Path(__file__).resolve().parents[2] / "harnesses" / "_base"
    if str(HARNESS_BASE) not in sys.path:
        sys.path.insert(0, str(HARNESS_BASE))

    from harness_common import cli as harness_cli

    captured_env = {}

    class FakeResult:
        returncode = 0
        stdout = b'{"ok": true}'
        stderr = b""

    def fake_run(cmd, env, capture_output, timeout, check):
        captured_env.update(env)
        return FakeResult()

    monkeypatch.setattr(harness_cli.subprocess, "run", fake_run)
    monkeypatch.setenv("INDEMN_API_URL", "http://x")
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "tok")
    monkeypatch.delenv("INDEMN_EFFECTIVE_ACTOR_ID", raising=False)

    harness_cli.indemn("actor", "list")

    assert "INDEMN_EFFECTIVE_ACTOR_ID" not in captured_env
