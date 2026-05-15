"""Sandbox backend factory.

Two modes supported via INDEMN_SANDBOX_TYPE env var:
  - localshell : LocalShellBackend (MVP / dev / testing) — no sandbox
  - daytona    : DaytonaSandbox (pre-prod / production)  — per-session isolation

Agent code (agent.py) doesn't change — only the backend instance differs.

Bug #3 (cross-invocation tool-cache leak): the LocalShellBackend was previously
constructed with a hardcoded `root_dir="/workspace"` shared across all agent
activities in the same runtime container. An agent's grep matched content from
a different prior agent's cached tool results (`/large_tool_results/...` lives
inside root_dir). Fix: scope `root_dir` per `activity_id`.
"""

import os


def _root_dir_for_activity(activity_id: str | None) -> str:
    """Compute the per-activity sandbox root directory.

    Returns {workspace}/{activity_id} for filesystem isolation between
    concurrent agents (the Bug #3 fix). Falls back to {workspace} if no
    activity_id is given — chat/voice sessions don't always have one
    and the existing layout for skills already lives there.

    Workspace resolution: `INDEMN_WORKSPACE_DIR` if set, else `/workspace`
    when writable (Docker layout — the Dockerfile creates it), else
    `/tmp/indemn-workspace` fallback for local dev (macOS `/` is read-only).
    """
    explicit = os.environ.get("INDEMN_WORKSPACE_DIR")
    if explicit:
        workspace = explicit
    elif os.path.isdir("/workspace") and os.access("/workspace", os.W_OK):
        workspace = "/workspace"
    else:
        workspace = "/tmp/indemn-workspace"
    if not activity_id:
        return workspace
    return f"{workspace}/{activity_id}"


def build_backend(activity_id: str | None = None):
    """Dispatch based on INDEMN_SANDBOX_TYPE env var.

    activity_id: per-invocation identifier used to scope the sandbox filesystem.
        For async associates, pass `act-{message_id[:12]}` (matching the existing
        skills_dir layout in main.py). For chat/voice sessions where there's no
        single activity_id, omit and the backend uses the shared /workspace root.
    """
    sandbox_type = os.environ.get("INDEMN_SANDBOX_TYPE", "localshell")

    if sandbox_type == "localshell":
        return _build_localshell_backend(activity_id)
    elif sandbox_type == "daytona":
        return _build_daytona_backend(activity_id)
    else:
        raise ValueError(
            f"Unknown INDEMN_SANDBOX_TYPE: {sandbox_type!r}. "
            f"Supported: 'localshell' (MVP) | 'daytona' (production)."
        )


def _build_localshell_backend(activity_id: str | None = None):
    """LocalShellBackend — no sandbox. Container is the blast radius.

    Security note: LocalShellBackend uses shell execution within the container.
    Risk is mitigated by:
      1. Container isolation (Railway) — no direct kernel/DB access
      2. CLI-only auth surface (service token required)
      3. Skill content hash verification on every fetch (U-08 wired 2026-04-17)
      4. Per-activity root_dir isolation (Bug #3 fix)
      5. Daytona sandbox is deferred — add when Tier 3 user-submitted skills arrive

    DO NOT SHIP TO EXTERNAL CUSTOMERS WITH THIS BACKEND.
    Switch INDEMN_SANDBOX_TYPE=daytona before Phase 7.
    """
    from deepagents.backends import LocalShellBackend

    root_dir = _root_dir_for_activity(activity_id)
    os.makedirs(root_dir, exist_ok=True)

    # Whitelist matches harness_common.cli.indemn() — the agent's `execute`
    # tool subprocesses must propagate the same trace/forensics/cascade
    # context as the harness's own CLI calls. Without these here, the
    # backend's env= replaces parent env entirely, so X-Correlation-ID +
    # X-Effective-Actor-Id + X-Causation-Message-ID never reach the API
    # from agent-driven calls. Snapshot taken at agent construction —
    # per-activity, since main.py sets these vars before calling
    # build_backend().
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "INDEMN_API_URL": os.environ["INDEMN_API_URL"],
        "INDEMN_SERVICE_TOKEN": os.environ["INDEMN_SERVICE_TOKEN"],
    }
    for k in ("TRACEPARENT", "TRACESTATE", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        if k in os.environ:
            env[k] = os.environ[k]
    for k in ("INDEMN_CAUSATION_MESSAGE_ID", "INDEMN_EFFECTIVE_ACTOR_ID", "INDEMN_CORRELATION_ID"):
        if k in os.environ:
            env[k] = os.environ[k]

    return LocalShellBackend(
        root_dir=root_dir,
        env=env,
    )


def _build_daytona_backend(activity_id: str | None = None):
    """DaytonaSandbox — per-session isolated VM. Production path.

    Config:
      INDEMN_SANDBOX_TYPE=daytona
      DAYTONA_API_KEY_REF=<secret_ref>  — resolved from AWS Secrets Manager

    Cold start ~90ms per session. Acceptable for async. Voice latency
    budget re-evaluated per-harness when voice harness ships.

    activity_id will scope the sandbox session for cross-invocation isolation
    (Bug #3 fix) when this is implemented.
    """
    raise NotImplementedError(
        "Daytona backend not yet implemented. "
        "Enable before Phase 7 (first external customer). "
        "Tracked in: 2026-04-16-harness-implementation-plan.md"
    )
