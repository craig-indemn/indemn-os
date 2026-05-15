"""Tests for the correlation_id propagation chain across the cascade.

Before this fix, every CLI subprocess invoked by the harness (within one
agent run AND across cascade hops) generated a fresh correlation_id. The
result: `indemn trace cascade <id>` returned only the single event sharing
that id — never the full cascade tree. Drain debugging and cascade-level
observability were impossible.

The fix: the harness sets `INDEMN_CORRELATION_ID` from the inbound message's
correlation_id at activity entry. The harness's `indemn()` subprocess wrapper
forwards that env var to every CLI subprocess. The CLI client reads the env
var and sends `X-Correlation-ID` on every API request. The kernel's auth
middleware (already wired pre-fix) reads the header and sets
`current_correlation_id`, which `save_tracked` and watch-emission propagate
through the kernel.

Mirrors the existing causation_message_id + effective_actor_id propagation
patterns (see test_effective_actor_id.py).

These tests pin: (1) the CLI client adds the header from env var,
(2) omits the header when unset, (3) all three propagation values coexist,
(4) the harness CLI wrapper propagates the env var to subprocess, (5) the
wrapper doesn't propagate when unset. The middleware path is exercised by
integration tests against a live MongoDB; here we keep it unit-level.
"""

import os
from unittest.mock import patch


# --- CLI client header propagation ---


def test_cli_client_adds_correlation_id_header_from_env():
    """When INDEMN_CORRELATION_ID is set, the CLI client adds the
    X-Correlation-ID header. This is the link from harness env var →
    API request header → middleware → contextvar → save_tracked."""
    from indemn_os.client import CLIClient

    with patch.dict(
        os.environ,
        {"INDEMN_CORRELATION_ID": "cid_69abc_123", "INDEMN_SERVICE_TOKEN": "tok"},
    ):
        client = CLIClient()
        h = client._headers()
    assert h.get("X-Correlation-ID") == "cid_69abc_123"


def test_cli_client_omits_correlation_id_header_when_env_unset():
    """No env var → no header. Don't send `X-Correlation-ID: None`."""
    from indemn_os.client import CLIClient

    env = {k: v for k, v in os.environ.items() if k != "INDEMN_CORRELATION_ID"}
    env["INDEMN_SERVICE_TOKEN"] = "tok"
    with patch.dict(os.environ, env, clear=True):
        client = CLIClient()
        h = client._headers()
    assert "X-Correlation-ID" not in h


def test_cli_client_passes_causation_effective_and_correlation_together():
    """All three propagation values are independent — they can coexist on
    the same request. Regression guard against a refactor that picks one."""
    from indemn_os.client import CLIClient

    with patch.dict(
        os.environ,
        {
            "INDEMN_CAUSATION_MESSAGE_ID": "msg_69abc",
            "INDEMN_EFFECTIVE_ACTOR_ID": "69actor",
            "INDEMN_CORRELATION_ID": "cid_xyz",
            "INDEMN_SERVICE_TOKEN": "tok",
        },
    ):
        client = CLIClient()
        h = client._headers()
    assert h["X-Causation-Message-ID"] == "msg_69abc"
    assert h["X-Effective-Actor-Id"] == "69actor"
    assert h["X-Correlation-ID"] == "cid_xyz"


# --- Harness CLI wrapper env-var propagation ---


def test_harness_cli_wrapper_propagates_correlation_id_to_subprocess(monkeypatch):
    """The harness's `indemn()` shells to subprocess. The subprocess starts
    with a clean env; the wrapper explicitly forwards specific keys.
    INDEMN_CORRELATION_ID must be one of them — otherwise the CLI client
    never sees it and never adds the X-Correlation-ID header, breaking the
    cascade propagation chain at the subprocess boundary."""
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
    monkeypatch.setenv("INDEMN_CORRELATION_ID", "cid_69cascade")

    harness_cli.indemn("actor", "list")

    assert captured_env.get("INDEMN_CORRELATION_ID") == "cid_69cascade"


def test_harness_cli_wrapper_does_not_propagate_correlation_id_when_unset(monkeypatch):
    """When the env var isn't set, don't add it to the subprocess env at all.
    Mirrors the conditional pattern for causation + effective_actor."""
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
    monkeypatch.delenv("INDEMN_CORRELATION_ID", raising=False)

    harness_cli.indemn("actor", "list")

    assert "INDEMN_CORRELATION_ID" not in captured_env
