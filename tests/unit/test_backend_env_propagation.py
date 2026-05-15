"""Tests for the harness backend's env-var propagation to the agent's
execute tool subprocess.

The deepagents `execute` tool (used by the agent for all `indemn` CLI calls
during agent.ainvoke()) runs subprocesses via the `LocalShellBackend`. That
backend takes an `env=` dict at construction time, and `subprocess.run(env=)`
REPLACES the parent environment — so anything not in the dict is invisible
to the agent's CLI calls.

Pre-fix, the dict had only PATH + INDEMN_API_URL + INDEMN_SERVICE_TOKEN.
Result: the harness's own direct `indemn` calls (via harness_common.cli)
propagated X-Correlation-ID + X-Effective-Actor-Id + X-Causation-Message-ID
correctly, but the AGENT's calls did not — because the agent goes through
the backend's restricted env. Effective_actor_id showed `null` on every
change record the agent wrote; correlation_ids fragmented into fresh UUIDs
per CLI call.

Fix: the backend env dict now mirrors the cli.py whitelist. These tests pin
that whitelist so the two propagation paths stay in sync.
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# harness_common is namespace-packaged into harnesses/_base/
HARNESS_BASE = Path(__file__).resolve().parents[2] / "harnesses" / "_base"
if str(HARNESS_BASE) not in sys.path:
    sys.path.insert(0, str(HARNESS_BASE))


def _install_stub_backend(monkeypatch, captured):
    """Install a stub for deepagents.backends.LocalShellBackend that records
    the env dict passed at construction time.

    Uses real ModuleType objects (not MagicMock) because Python's
    `from X import Y` requires X to be a proper module with Y as an attr.
    MagicMock-as-module silently creates fresh MagicMock children on
    attribute access, which means our fake never gets called.
    """
    def fake_lsb(root_dir, env):
        captured["root_dir"] = root_dir
        captured["env"] = env
        return MagicMock()

    deepagents_mod = types.ModuleType("deepagents")
    backends_mod = types.ModuleType("deepagents.backends")
    backends_mod.LocalShellBackend = fake_lsb
    deepagents_mod.backends = backends_mod
    monkeypatch.setitem(sys.modules, "deepagents", deepagents_mod)
    monkeypatch.setitem(sys.modules, "deepagents.backends", backends_mod)


def test_backend_propagates_correlation_id_when_set(tmp_path, monkeypatch):
    """When INDEMN_CORRELATION_ID is set, the backend captures it into its
    env dict so the agent's execute tool subprocess can read it."""
    monkeypatch.setenv("INDEMN_API_URL", "http://x")
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("INDEMN_CORRELATION_ID", "cid_abc123")
    monkeypatch.setenv("INDEMN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("INDEMN_SANDBOX_TYPE", "localshell")

    captured = {}
    _install_stub_backend(monkeypatch, captured)

    from harness_common.backend import build_backend
    build_backend(activity_id="act-test")

    assert captured["env"].get("INDEMN_CORRELATION_ID") == "cid_abc123"


def test_backend_omits_correlation_id_when_unset(tmp_path, monkeypatch):
    """No env var set → key omitted from backend env (not set to empty)."""
    monkeypatch.setenv("INDEMN_API_URL", "http://x")
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "tok")
    monkeypatch.delenv("INDEMN_CORRELATION_ID", raising=False)
    monkeypatch.setenv("INDEMN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("INDEMN_SANDBOX_TYPE", "localshell")

    captured = {}
    _install_stub_backend(monkeypatch, captured)

    from harness_common.backend import build_backend
    build_backend(activity_id="act-test")

    assert "INDEMN_CORRELATION_ID" not in captured["env"]


def test_backend_propagates_effective_actor_id_when_set(tmp_path, monkeypatch):
    """The latent bug being fixed alongside correlation_id: agent-driven
    CLI calls had effective_actor_id=null forever because this path
    wasn't propagated. Pin it now."""
    monkeypatch.setenv("INDEMN_API_URL", "http://x")
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("INDEMN_EFFECTIVE_ACTOR_ID", "69eff_actor")
    monkeypatch.setenv("INDEMN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("INDEMN_SANDBOX_TYPE", "localshell")

    captured = {}
    _install_stub_backend(monkeypatch, captured)

    from harness_common.backend import build_backend
    build_backend(activity_id="act-test")

    assert captured["env"].get("INDEMN_EFFECTIVE_ACTOR_ID") == "69eff_actor"


def test_backend_propagates_causation_message_id_when_set(tmp_path, monkeypatch):
    """Same propagation pattern for causation_message_id."""
    monkeypatch.setenv("INDEMN_API_URL", "http://x")
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("INDEMN_CAUSATION_MESSAGE_ID", "msg_69abc")
    monkeypatch.setenv("INDEMN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("INDEMN_SANDBOX_TYPE", "localshell")

    captured = {}
    _install_stub_backend(monkeypatch, captured)

    from harness_common.backend import build_backend
    build_backend(activity_id="act-test")

    assert captured["env"].get("INDEMN_CAUSATION_MESSAGE_ID") == "msg_69abc"


def test_backend_propagates_all_three_propagation_vars_together(tmp_path, monkeypatch):
    """All three propagation values can coexist on the same backend.
    Regression guard against a refactor that picks one."""
    monkeypatch.setenv("INDEMN_API_URL", "http://x")
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("INDEMN_CAUSATION_MESSAGE_ID", "msg_abc")
    monkeypatch.setenv("INDEMN_EFFECTIVE_ACTOR_ID", "act_def")
    monkeypatch.setenv("INDEMN_CORRELATION_ID", "cid_xyz")
    monkeypatch.setenv("INDEMN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("INDEMN_SANDBOX_TYPE", "localshell")

    captured = {}
    _install_stub_backend(monkeypatch, captured)

    from harness_common.backend import build_backend
    build_backend(activity_id="act-test")

    assert captured["env"].get("INDEMN_CAUSATION_MESSAGE_ID") == "msg_abc"
    assert captured["env"].get("INDEMN_EFFECTIVE_ACTOR_ID") == "act_def"
    assert captured["env"].get("INDEMN_CORRELATION_ID") == "cid_xyz"


def test_backend_propagates_otel_context_when_set(tmp_path, monkeypatch):
    """OTEL trace context (TRACEPARENT/TRACESTATE) should propagate too,
    so the agent's subprocess can participate in the same OTEL trace."""
    monkeypatch.setenv("INDEMN_API_URL", "http://x")
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("TRACEPARENT", "00-abc123-def456-01")
    monkeypatch.setenv("TRACESTATE", "vendor1=value1")
    monkeypatch.setenv("INDEMN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("INDEMN_SANDBOX_TYPE", "localshell")

    captured = {}
    _install_stub_backend(monkeypatch, captured)

    from harness_common.backend import build_backend
    build_backend(activity_id="act-test")

    assert captured["env"].get("TRACEPARENT") == "00-abc123-def456-01"
    assert captured["env"].get("TRACESTATE") == "vendor1=value1"


def test_backend_env_required_baseline_keys_still_present(tmp_path, monkeypatch):
    """The pre-existing PATH + API_URL + SERVICE_TOKEN must remain — the
    backend can't function without them. Pin against accidental removal."""
    monkeypatch.setenv("INDEMN_API_URL", "http://x")
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("INDEMN_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("INDEMN_SANDBOX_TYPE", "localshell")

    captured = {}
    _install_stub_backend(monkeypatch, captured)

    from harness_common.backend import build_backend
    build_backend(activity_id="act-test")

    assert captured["env"]["PATH"]
    assert captured["env"]["INDEMN_API_URL"] == "http://x"
    assert captured["env"]["INDEMN_SERVICE_TOKEN"] == "tok"
