"""Subprocess wrapper for harness orchestration CLI calls.

Used for: register_instance, heartbeat, load associate/entity/skill context,
mark message complete/failed. NOT used for agent's runtime tool execution —
that goes through deepagents' LocalShellBackend (MVP) or DaytonaSandbox (prod).
"""

import json
import os
import subprocess
from typing import Any


class CLIError(RuntimeError):
    pass


def indemn(*args: str, timeout: float = 30.0, parse_json: bool = True) -> Any:
    """Run `indemn <args> --json` as subprocess, parse JSON result."""
    env = {
        "INDEMN_API_URL": os.environ["INDEMN_API_URL"],
        "INDEMN_SERVICE_TOKEN": os.environ["INDEMN_SERVICE_TOKEN"],
        "PATH": os.environ["PATH"],
        "PYTHONUNBUFFERED": "1",
    }
    # OTEL context propagation
    for k in ("TRACEPARENT", "TRACESTATE", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        if k in os.environ:
            env[k] = os.environ[k]

    cmd = ["indemn", *args]
    if parse_json and "--json" not in cmd:
        cmd.append("--json")

    result = subprocess.run(
        cmd, env=env, capture_output=True, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise CLIError(f"CLI failed ({result.returncode}): {result.stderr.decode()[:500]}")

    output = result.stdout.decode()
    return json.loads(output) if parse_json and output.strip() else output
