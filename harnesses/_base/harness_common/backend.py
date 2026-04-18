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

    Security note: LocalShellBackend uses shell execution within the container.
    Risk is mitigated by:
      1. Container isolation (Railway) — no direct kernel/DB access
      2. CLI-only auth surface (service token required)
      3. Skill content hash verification on every fetch (U-08 wired 2026-04-17)
      4. Daytona sandbox is deferred — add when Tier 3 user-submitted skills arrive

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
