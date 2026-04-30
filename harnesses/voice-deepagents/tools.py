"""Tool surface for voice-deepagents — single `execute` tool that runs `indemn` CLI commands.

The voice agent talks via LiveKit (audio in / audio out). Reasoning happens in a Vertex AI
Gemini LLM. To act on the OS, the agent calls one tool: `execute(command: str)` which runs
the command as a subprocess against the `indemn` CLI. Output (stdout) is returned to the
agent so it can read entity skills, list/get/create entities, transition states, etc.

This is symmetric with the async-deepagents harness's tool surface (per Session 12's
deepagents-skills-layer drop, commit `7281b83` on indemn-os main): single `execute`
primitive, all OS interaction via the CLI.
"""

import asyncio
import logging
import os
from livekit.agents import function_tool, RunContext

log = logging.getLogger(__name__)

# How long an `indemn` CLI call may run before we abort with a timeout error.
# Most calls return in <2s; some `fetch-new` runs can take minutes. Default
# matches `INDEMN_CLI_TIMEOUT` from the user-side CLI.
DEFAULT_CMD_TIMEOUT_SEC = float(os.environ.get("INDEMN_CLI_TIMEOUT", "600"))


@function_tool
async def execute(context: RunContext, command: str) -> str:
    """Run an `indemn` CLI command and return its output.

    The voice agent uses this to interact with the Indemn OS — load skills,
    look up entities, resolve people/companies, create entities (Touchpoint,
    Email, Meeting, etc.), transition states.

    Args:
        command: The CLI command to run. MUST be a single shell line starting
            with `indemn`. Example: `indemn skill get log-touchpoint` or
            `indemn touchpoint create --data '{"company": "...", ...}'`.

    Returns:
        The combined stdout/stderr of the command. The agent should parse
        this (often JSON) to act on the result.
    """
    if not command or not command.strip():
        return "ERROR: empty command"

    cmd = command.strip()
    if not cmd.startswith("indemn"):
        # Allow `indemn` CLI invocations only — keeps the tool surface minimal
        # and prevents the agent from spawning arbitrary processes on the
        # voice harness host.
        return (
            f"ERROR: only `indemn` CLI commands are allowed. Got: {cmd[:80]}\n"
            f"Example: `indemn skill get log-touchpoint` or "
            f"`indemn touchpoint create --data '{{...}}'`"
        )

    log.info("execute: %s", cmd[:200])

    # Pass through INDEMN_SERVICE_TOKEN + INDEMN_API_URL so the CLI authenticates
    # against the OS API as the runtime's service actor (effective_actor_id =
    # the voice agent's actor when set via INDEMN_EFFECTIVE_ACTOR_ID).
    env = {**os.environ}

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DEFAULT_CMD_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"ERROR: command timed out after {DEFAULT_CMD_TIMEOUT_SEC}s: {cmd[:80]}"

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        rc = proc.returncode

        # Compose result: stdout for success path; include stderr + exit code on
        # failure so the agent can recover (read error message, retry, ask user).
        if rc == 0:
            if err:
                return f"{out}\n[stderr]\n{err}"
            return out
        return (
            f"[Command failed with exit code {rc}]\n"
            f"[stdout]\n{out}\n"
            f"[stderr]\n{err}"
        )
    except Exception as e:
        log.exception("execute failed for command: %s", cmd[:80])
        return f"ERROR: subprocess raised {type(e).__name__}: {e}"
