"""Sandbox backend factory.

Two modes supported via INDEMN_SANDBOX_TYPE env var:
  - localshell : LocalShellBackend (MVP / dev / testing) — no sandbox
  - daytona    : DaytonaSandbox (pre-prod / production)  — per-session isolation

Agent code (agent.py) doesn't change — only the backend instance differs.
"""

import os


def build_backend():
    """Dispatch based on INDEMN_SANDBOX_TYPE env var."""
    sandbox_type = os.environ.get("INDEMN_SANDBOX_TYPE", "localshell")

    if sandbox_type == "localshell":
        return _build_localshell_backend()
    elif sandbox_type == "daytona":
        return _build_daytona_backend()
    else:
        raise ValueError(
            f"Unknown INDEMN_SANDBOX_TYPE: {sandbox_type!r}. "
            f"Supported: 'localshell' (MVP) | 'daytona' (production)."
        )


def _build_localshell_backend():
    """LocalShellBackend — no sandbox. Container is the blast radius.

    SECURITY POSTURE (non-production only):
    ----------------------------------------
    deepagents' LocalShellBackend uses subprocess.run(shell=True).
    LLM output feeds directly into a shell interpreter.
    Prompt injection CAN execute arbitrary shell commands inside
    the harness container.

    Acceptable during testing/dev because:
      1. We control the prompts (approved + content-hashed skills)
      2. We control the inputs (internal data during dev, not adversarial)
      3. Container isolation from kernel (no MongoDB creds, no kernel code)
      4. Railway container is ephemeral (no persistent corruption)

    DO NOT SHIP TO EXTERNAL CUSTOMERS WITH THIS BACKEND.
    Switch INDEMN_SANDBOX_TYPE=daytona before Phase 7.
    """
    from deepagents.backends import LocalShellBackend

    return LocalShellBackend(
        root_dir="/workspace",
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "INDEMN_API_URL": os.environ["INDEMN_API_URL"],
            "INDEMN_SERVICE_TOKEN": os.environ["INDEMN_SERVICE_TOKEN"],
        },
    )


def _build_daytona_backend():
    """DaytonaSandbox — per-session isolated VM. Production path.

    Config:
      INDEMN_SANDBOX_TYPE=daytona
      DAYTONA_API_KEY_REF=<secret_ref>  — resolved from AWS Secrets Manager

    Cold start ~90ms per session. Acceptable for async. Voice latency
    budget re-evaluated per-harness when voice harness ships.
    """
    raise NotImplementedError(
        "Daytona backend not yet implemented. "
        "Enable before Phase 7 (first external customer). "
        "Tracked in: 2026-04-16-harness-implementation-plan.md"
    )
