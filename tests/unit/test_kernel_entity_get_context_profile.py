"""Pin --context-profile flag acceptance on kernel-entity CLI get commands.

The async-deepagents harness's `_load_message_context` unconditionally passes
`--context-profile llm` to every entity get call (commit `80ac99f`, Phase C).
Kernel entities are uncapped by design (no FieldDefinition rows), but the CLI
must accept the flag at parse time AND propagate it to the API as
`?context_profile=` — otherwise Typer rejects the unknown option with exit
code 2 before any HTTP call happens.

If any kernel-entity static CLI `get` command stops accepting the flag, the
Evaluator (and any future associate watching a kernel entity) goes dark.
Same incident pattern as 2026-05-21 Session 28 EOS finding (Evaluator stuck
on every IE trace since Session 27 Phase C deploy).

These tests pin: (a) `--context-profile llm` is accepted without parse error,
and (b) the value propagates to the underlying API call as a query param.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from indemn_os.actor_commands import actor_app
from indemn_os.entity_commands import entity_app
from indemn_os.eval_commands import eval_app
from indemn_os.integration_commands import integration_app
from indemn_os.lookup_commands import lookup_app
from indemn_os.role_commands import role_app
from indemn_os.runtime_commands import runtime_app
from indemn_os.skill_commands import skill_app
from indemn_os.trace_commands import trace_app


# Each tuple: (app, module path where CLIClient is imported, arg for `get`)
KERNEL_ENTITY_APPS = [
    (trace_app, "indemn_os.trace_commands", "test_trace_id"),
    (actor_app, "indemn_os.actor_commands", "test_actor_id"),
    (role_app, "indemn_os.role_commands", "test_role_id"),
    (runtime_app, "indemn_os.runtime_commands", "test_runtime_id"),
    (integration_app, "indemn_os.integration_commands", "test_integration_id"),
    (skill_app, "indemn_os.skill_commands", "test_skill_name"),
    (lookup_app, "indemn_os.lookup_commands", "test_lookup_name"),
    (entity_app, "indemn_os.entity_commands", "TestEntity"),
    (eval_app, "indemn_os.eval_commands", "test_run_id"),
]


@pytest.mark.parametrize("app,client_module,arg", KERNEL_ENTITY_APPS)
def test_kernel_entity_get_accepts_context_profile_flag(app, client_module, arg):
    """Verify --context-profile is accepted and propagates to the API call.

    Mocks both CLIClient (so no real HTTP call) and the module-level `render`
    function (so the test is isolated from any output-format quirks). Asserts
    exit_code == 0 (Typer accepted the flag) and that `context_profile=llm`
    was passed through to `client.get(params=...)`.
    """
    runner = CliRunner()
    with patch.dict(os.environ, {"INDEMN_SERVICE_TOKEN": "tok"}), \
         patch(f"{client_module}.CLIClient") as mock_client_cls, \
         patch(f"{client_module}.render"):
        mock_client = MagicMock()
        mock_client.get.return_value = {"_id": arg, "name": "stub"}
        mock_client_cls.return_value = mock_client

        result = runner.invoke(app, ["get", arg, "--context-profile", "llm"])

        # Exit code 0 = Typer accepted the flag and the command ran.
        # Exit code 2 = Typer rejected the flag (the bug we're fixing).
        assert result.exit_code == 0, (
            f"{client_module} rejected --context-profile "
            f"(exit={result.exit_code}): {result.output}\n"
            f"Exception: {result.exception}"
        )

        # Verify the flag was passed through to the API as ?context_profile=.
        assert mock_client.get.call_args is not None, (
            f"{client_module}::get did not call client.get(...)"
        )
        params = mock_client.get.call_args.kwargs.get("params", {})
        assert params.get("context_profile") == "llm", (
            f"{client_module}::get did not propagate context_profile to "
            f"params: got {params}"
        )


@pytest.mark.parametrize("app,client_module,arg", KERNEL_ENTITY_APPS)
def test_kernel_entity_get_omits_context_profile_when_not_passed(
    app, client_module, arg
):
    """Verify omitting --context-profile leaves context_profile out of params.

    Pins that the option is None by default — the flag is opt-in. Other
    callers (humans + the agent's execute() tool calls) don't pass it, and
    we don't want to silently inject `context_profile=raw` query strings
    into every kernel-entity get call.
    """
    runner = CliRunner()
    with patch.dict(os.environ, {"INDEMN_SERVICE_TOKEN": "tok"}), \
         patch(f"{client_module}.CLIClient") as mock_client_cls, \
         patch(f"{client_module}.render"):
        mock_client = MagicMock()
        mock_client.get.return_value = {"_id": arg, "name": "stub"}
        mock_client_cls.return_value = mock_client

        result = runner.invoke(app, ["get", arg])

        assert result.exit_code == 0, (
            f"{client_module}::get without --context-profile failed "
            f"(exit={result.exit_code}): {result.output}\n"
            f"Exception: {result.exception}"
        )

        # Default invocation should NOT add context_profile to params.
        params = mock_client.get.call_args.kwargs.get("params", {})
        assert "context_profile" not in params, (
            f"{client_module}::get added context_profile to params when flag "
            f"was not passed: got {params}"
        )
