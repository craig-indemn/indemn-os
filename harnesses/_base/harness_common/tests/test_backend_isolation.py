"""Tests for per-activity sandbox root_dir isolation.

Bug #3 (cross-invocation tool-cache leak from os-bugs-and-shakeout): the
LocalShellBackend was constructed with a hardcoded `root_dir="/workspace"`
shared across all agent activities in the same runtime container. An agent's
grep matched content from a different prior agent's cached tool results
(`/large_tool_results/...` is shared inside `/workspace`).

The fix is to scope the sandbox root per activity_id so each agent gets its
own filesystem. This module tests the path-computation helper that drives
that scoping. The actual LocalShellBackend integration is covered by
the deepagents library's own tests; we just verify the wrapper passes the
right root_dir.
"""

import sys
from pathlib import Path

# harness_common is namespace-packaged into harnesses/_base/. Add it to path
# so we can import without the full harness install.
HARNESS_BASE = Path(__file__).resolve().parents[2]
if str(HARNESS_BASE) not in sys.path:
    sys.path.insert(0, str(HARNESS_BASE))

from harness_common.backend import _root_dir_for_activity  # noqa: E402


def test_root_dir_with_activity_id():
    """activity_id given → returns /workspace/{activity_id}."""
    assert _root_dir_for_activity("act-abc123") == "/workspace/act-abc123"


def test_root_dir_empty_string_falls_back_to_workspace():
    """Empty activity_id → /workspace (back-compat for chat/voice sessions)."""
    assert _root_dir_for_activity("") == "/workspace"


def test_root_dir_none_falls_back_to_workspace():
    """None activity_id → /workspace (defensive default)."""
    assert _root_dir_for_activity(None) == "/workspace"


def test_root_dir_isolates_different_activities():
    """Two different activity_ids must give different directories.

    This is the core property that prevents cross-invocation tool-cache leaks.
    """
    a = _root_dir_for_activity("act-abc123")
    b = _root_dir_for_activity("act-def456")
    assert a != b


def test_root_dir_deterministic():
    """Same activity_id always gives same directory (idempotent)."""
    assert _root_dir_for_activity("act-xyz") == _root_dir_for_activity("act-xyz")


def test_root_dir_uses_workspace_prefix():
    """Every activity directory lives under /workspace (matches container layout)."""
    result = _root_dir_for_activity("act-abc")
    assert result.startswith("/workspace/")
