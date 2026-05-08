"""Subprocess wrapper for harness orchestration CLI calls.

Used for: register_instance, heartbeat, load associate/entity/skill context,
mark message complete/failed. NOT used for agent's runtime tool execution —
that goes through deepagents' LocalShellBackend (MVP) or DaytonaSandbox (prod).
"""

import json
import os
import shutil
import subprocess
import sys
from typing import Any


class CLIError(RuntimeError):
    pass


def _resolve_indemn_binary() -> str:
    """Resolve the absolute path to the python `indemn_os` CLI.

    LiveKit Agents (and other multiprocess harness frameworks) spawn
    JobProcess subprocesses whose inherited PATH may pick up the wrong
    `indemn` binary — e.g. on dev macOS, `/opt/homebrew/bin/indemn` is
    a Node.js CLI from the `@indemn/cli` npm package that only has
    `init` (no actor/runtime/skill commands). The Python `indemn_os`
    binary lives in the same venv as harness_common; resolve that
    explicitly via `shutil.which` against `sys.executable`'s bin dir
    so the harness can't accidentally invoke a different tool.

    Resolution order:
      1. `INDEMN_CLI_PATH` env var if set
      2. `<sys.executable's directory>/indemn` if it exists + is executable
      3. `shutil.which("indemn")` fallback (PATH-based)
    """
    explicit = os.environ.get("INDEMN_CLI_PATH")
    if explicit and os.access(explicit, os.X_OK):
        return explicit

    venv_bin_dir = os.path.dirname(sys.executable)
    venv_indemn = os.path.join(venv_bin_dir, "indemn")
    if os.access(venv_indemn, os.X_OK):
        return venv_indemn

    fallback = shutil.which("indemn")
    if fallback:
        return fallback

    raise RuntimeError(
        "Cannot find `indemn` binary. Install indemn_os in the venv "
        "(`pip install -e indemn_os/`) or set INDEMN_CLI_PATH."
    )


_INDEMN_BIN = _resolve_indemn_binary()


def indemn(*args: str, timeout: float = 30.0, parse_json: bool = True) -> Any:
    """Run `indemn <args>` as subprocess, parse JSON result.

    The CLI outputs JSON by default — no --json flag needed.
    """
    env = {
        "INDEMN_API_URL": os.environ["INDEMN_API_URL"],
        "INDEMN_SERVICE_TOKEN": os.environ["INDEMN_SERVICE_TOKEN"],
        "INDEMN_OUTPUT_FORMAT": "json",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }
    # OTEL context propagation
    for k in ("TRACEPARENT", "TRACESTATE", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        if k in os.environ:
            env[k] = os.environ[k]
    # Causation message ID propagation
    if "INDEMN_CAUSATION_MESSAGE_ID" in os.environ:
        env["INDEMN_CAUSATION_MESSAGE_ID"] = os.environ["INDEMN_CAUSATION_MESSAGE_ID"]
    # Effective-actor-id propagation (Bug #22): the harness sets this to the
    # associate's actor_id before agent/CLI work, so the changes collection
    # records which associate acted (vs just "the runtime token's actor").
    if "INDEMN_EFFECTIVE_ACTOR_ID" in os.environ:
        env["INDEMN_EFFECTIVE_ACTOR_ID"] = os.environ["INDEMN_EFFECTIVE_ACTOR_ID"]

    cmd = [_INDEMN_BIN, *args]

    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise CLIError(f"CLI failed ({result.returncode}): {result.stderr.decode()[:500]}")

    output = result.stdout.decode()
    return json.loads(output) if parse_json and output.strip() else output
