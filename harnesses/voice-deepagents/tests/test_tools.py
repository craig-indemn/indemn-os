"""Tests for voice-deepagents tools.py — the `execute` CLI subprocess wrapper.

Pin the contract: only `indemn` commands allowed; success returns stdout;
failure returns formatted exit code + stderr; timeouts caught + reported.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add the harness directory to the path so we can import tools
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.asyncio
async def test_rejects_empty_command():
    """Empty / whitespace-only commands return an error string instead of
    spawning a subprocess."""
    from tools import execute

    result = await execute.fn(MagicMock(), command="")
    assert "ERROR" in result
    assert "empty" in result.lower()

    result = await execute.fn(MagicMock(), command="   ")
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_rejects_non_indemn_commands():
    """Tool surface is restricted to `indemn` CLI invocations only — agent
    cannot spawn arbitrary processes via `execute('rm -rf /')`."""
    from tools import execute

    result = await execute.fn(MagicMock(), command="ls /tmp")
    assert "ERROR" in result
    assert "only `indemn` CLI commands are allowed" in result

    result = await execute.fn(MagicMock(), command="cat /etc/passwd")
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_returns_stdout_on_success():
    """Successful command (rc=0) returns stdout verbatim."""
    from tools import execute

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"hello world\n", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        result = await execute.fn(MagicMock(), command="indemn skill get log-touchpoint")

    assert result == "hello world"


@pytest.mark.asyncio
async def test_returns_failure_on_nonzero_exit():
    """Non-zero exit returns formatted output: code + stdout + stderr.
    The agent reads this and can recover (retry, ask user, etc.)."""
    from tools import execute

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b"some output", b"NotFound: company X")
    )
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        result = await execute.fn(MagicMock(), command="indemn company get fake-id")

    assert "Command failed with exit code 1" in result
    assert "some output" in result
    assert "NotFound: company X" in result


@pytest.mark.asyncio
async def test_handles_timeout():
    """Long-running command past DEFAULT_CMD_TIMEOUT_SEC kills the process
    and returns a TIMEOUT error so the agent can recover or warn the user."""
    from tools import execute

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("tools.DEFAULT_CMD_TIMEOUT_SEC", 0.01):
            result = await execute.fn(MagicMock(), command="indemn email fetch-new")

    assert "ERROR" in result
    assert "timed out" in result
    mock_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_appends_stderr_on_success_when_nonempty():
    """If a successful command (rc=0) also wrote to stderr (warnings),
    surface it to the agent so it can react."""
    from tools import execute

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b"created entity 123", b"WARN: deprecated flag")
    )
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        result = await execute.fn(MagicMock(), command="indemn touchpoint create --data '{}'")

    assert "created entity 123" in result
    assert "[stderr]" in result
    assert "WARN: deprecated flag" in result
