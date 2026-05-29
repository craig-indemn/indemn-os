"""Shared test setup for chat-deepagents tests.

Runs before any test module is imported. Two responsibilities:

1. Add `harnesses/_base/` to sys.path so the real `harness_common` package
   loads (Phase 4 tests need the real `derive_checkpointer_thread_id`).

2. Pre-import `harness_common.thread_id` so the real module lands in
   sys.modules BEFORE individual test files stub other `harness_common.*`
   submodules. Without this, the first test file's `sys.modules.setdefault
   ("harness_common", MagicMock())` pollutes the session and blocks the
   real package import from resolving.

Also adds the harness package dir to sys.path so `from agent import ...`
and `from session import ...` work in tests (mirrors the pattern test files
were doing individually).
"""

import sys
from pathlib import Path

CHAT_HARNESS_DIR = Path(__file__).resolve().parents[1]
HARNESSES_BASE_DIR = CHAT_HARNESS_DIR.parent / "_base"

if str(CHAT_HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_HARNESS_DIR))
if str(HARNESSES_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESSES_BASE_DIR))

# Real langchain_core.messages — isinstance() needs the real types.
# Stub the heavy runtime deps that session.py + agent.py import at module load
# (deepagents, harness.agent, harness_common submodules, langchain, starlette,
# langgraph checkpointer libs, motor). Per-file stubs would duplicate this; the
# conftest centralizes so all chat tests benefit + ordering issues don't recur.
from unittest.mock import MagicMock  # noqa: E402

# Real harness_common.thread_id — Phase 4 tests + the session.py module use
# derive_checkpointer_thread_id. Pre-loading here makes the real package land
# in sys.modules before any test's MagicMock setdefault would conflict.
from harness_common.thread_id import derive_checkpointer_thread_id  # noqa: E402,F401
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402,F401

for mod in [
    "deepagents",
    "harness",
    "harness.agent",
    "harness_common.backend",
    "harness_common.cli",
    "harness_common.runtime",
    "harness_common.attention",
    "harness_common.interaction",
    "langchain",
    "langchain.chat_models",
    "starlette",
    "starlette.websockets",
    "langgraph",
    "langgraph.checkpoint",
    "langgraph.checkpoint.memory",
    "langgraph.checkpoint.mongodb",
    "motor",
    "motor.motor_asyncio",
]:
    sys.modules.setdefault(mod, MagicMock())
