"""Tests for harness_common.cli per-call env kwargs (AI-407 Task 2.5).

The async harness can safely mutate os.environ (single activity per process at
a time). Chat + voice harnesses run many concurrent sessions in one process —
mutating os.environ races across sessions and contaminates cross-session
lineage attribution. Task 2.5 adds per-call kwargs (correlation_id,
effective_actor_id, service_token) on the indemn() wrapper so chat + voice
(Tasks 2.11 + 2.19) can pass session-local values without touching process env.

Back-compat is preserved: async's existing pattern (set on os.environ, call
indemn() without kwargs) still works — the kwargs are optional overrides.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

HARNESS_BASE = Path(__file__).resolve().parents[2]
if str(HARNESS_BASE) not in sys.path:
    sys.path.insert(0, str(HARNESS_BASE))


class TestProcessEnvInheritance:
    """Async harness back-compat: set on os.environ, call indemn() without kwargs."""

    @patch("subprocess.run")
    def test_cli_subprocess_inherits_correlation_id(self, mock_run):
        with patch.dict(
            os.environ,
            {
                "INDEMN_API_URL": "http://test",
                "INDEMN_SERVICE_TOKEN": "tok",
                "INDEMN_CORRELATION_ID": "cor_async_xyz",
            },
        ):
            from harness_common.cli import indemn

            mock_run.return_value = MagicMock(returncode=0, stdout=b"{}", stderr=b"")
            try:
                indemn("actor", "list")
            except Exception:
                pass

            env = mock_run.call_args.kwargs.get("env", {})
            assert env.get("INDEMN_CORRELATION_ID") == "cor_async_xyz"

    @patch("subprocess.run")
    def test_cli_subprocess_inherits_effective_actor_id(self, mock_run):
        with patch.dict(
            os.environ,
            {
                "INDEMN_API_URL": "http://test",
                "INDEMN_SERVICE_TOKEN": "tok",
                "INDEMN_EFFECTIVE_ACTOR_ID": "act_async_xyz",
            },
        ):
            from harness_common.cli import indemn

            mock_run.return_value = MagicMock(returncode=0, stdout=b"{}", stderr=b"")
            try:
                indemn("actor", "list")
            except Exception:
                pass

            env = mock_run.call_args.kwargs.get("env", {})
            assert env.get("INDEMN_EFFECTIVE_ACTOR_ID") == "act_async_xyz"


class TestPerCallEnv:
    """Chat + voice harness path: per-call kwargs, no os.environ mutation."""

    @patch("subprocess.run")
    def test_per_call_correlation_id_overrides_process_env(self, mock_run):
        """Per-call kwarg wins over process env — fixes the multi-session race."""
        with patch.dict(
            os.environ,
            {
                "INDEMN_API_URL": "http://test",
                "INDEMN_SERVICE_TOKEN": "tok",
                "INDEMN_CORRELATION_ID": "process_value",
            },
        ):
            from harness_common.cli import indemn

            mock_run.return_value = MagicMock(returncode=0, stdout=b"{}", stderr=b"")
            try:
                indemn("actor", "list", correlation_id="per_call_value")
            except Exception:
                pass

            env = mock_run.call_args.kwargs.get("env", {})
            assert env.get("INDEMN_CORRELATION_ID") == "per_call_value"

    @patch("subprocess.run")
    def test_per_call_effective_actor_id_overrides_process_env(self, mock_run):
        with patch.dict(
            os.environ,
            {
                "INDEMN_API_URL": "http://test",
                "INDEMN_SERVICE_TOKEN": "tok",
                "INDEMN_EFFECTIVE_ACTOR_ID": "process_actor",
            },
        ):
            from harness_common.cli import indemn

            mock_run.return_value = MagicMock(returncode=0, stdout=b"{}", stderr=b"")
            try:
                indemn(
                    "actor",
                    "list",
                    effective_actor_id="per_call_actor",
                )
            except Exception:
                pass

            env = mock_run.call_args.kwargs.get("env", {})
            assert env.get("INDEMN_EFFECTIVE_ACTOR_ID") == "per_call_actor"

    @patch("subprocess.run")
    def test_per_call_service_token_overrides_process_env(self, mock_run):
        """Per-call service_token override — used in voice frontdoor when
        impersonating different deployments."""
        with patch.dict(
            os.environ,
            {
                "INDEMN_API_URL": "http://test",
                "INDEMN_SERVICE_TOKEN": "process_token",
            },
        ):
            from harness_common.cli import indemn

            mock_run.return_value = MagicMock(returncode=0, stdout=b"{}", stderr=b"")
            try:
                indemn("actor", "list", service_token="per_call_token")
            except Exception:
                pass

            env = mock_run.call_args.kwargs.get("env", {})
            assert env.get("INDEMN_SERVICE_TOKEN") == "per_call_token"

    @patch("subprocess.run")
    def test_no_kwarg_no_overrid_no_mutation(self, mock_run):
        """When no kwargs are passed, the process env is inherited unchanged.

        Pins back-compat: async harness keeps working without modification.
        """
        with patch.dict(
            os.environ,
            {
                "INDEMN_API_URL": "http://test",
                "INDEMN_SERVICE_TOKEN": "tok",
                "INDEMN_CORRELATION_ID": "cor_inherited",
                "INDEMN_EFFECTIVE_ACTOR_ID": "act_inherited",
            },
        ):
            from harness_common.cli import indemn

            mock_run.return_value = MagicMock(returncode=0, stdout=b"{}", stderr=b"")
            try:
                indemn("actor", "list")
            except Exception:
                pass

            env = mock_run.call_args.kwargs.get("env", {})
            assert env.get("INDEMN_CORRELATION_ID") == "cor_inherited"
            assert env.get("INDEMN_EFFECTIVE_ACTOR_ID") == "act_inherited"
            assert env.get("INDEMN_SERVICE_TOKEN") == "tok"
