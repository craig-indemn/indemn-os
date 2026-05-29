"""Shared test setup for voice-deepagents tests.

Runs before any test module is imported. Mirrors the chat-deepagents
conftest pattern (Phase 2A deviation #3 — `chat-deepagents/tests/conftest.py`):
pre-load real modules where available, centralize stubs for what's not
installed locally, so individual test files stop polluting sys.modules
across runs (the symptom this fixes: test_checkpointer_wiring stubbing
livekit.agents as MagicMock caused test_llm_adapter's
`pytest.importorskip("livekit.agents.llm")` to skip).

Responsibilities:

1. Add `harnesses/voice-deepagents/` to sys.path so `from agent import ...`,
   `from session import ...`, `from main import ...`, `from llm_adapter import ...`
   work in tests (the harness dir has hyphens — not a valid Python package
   name — and in the Docker image is COPYed to /app/harness/).

2. Add `harnesses/_base/` to sys.path so the real `harness_common` package
   loads (Phase 4 tests need real types where they pin behavior).

3. Pre-load real modules (langchain_core.messages, harness_common.thread_id,
   harness_common.sanitize) into sys.modules so they're guaranteed real
   regardless of test file ordering.

4. Stub the runtime deps NOT installed in the local .venv (harness.* Docker
   package path, motor, langgraph-checkpoint-mongodb). Real modules
   (langchain, livekit.agents, deepagents, harness_common) are NOT stubbed
   — sys.modules.setdefault is idempotent so real packages take precedence.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

VOICE_HARNESS_DIR = Path(__file__).resolve().parents[1]
HARNESSES_BASE_DIR = VOICE_HARNESS_DIR.parent / "_base"

if str(VOICE_HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(VOICE_HARNESS_DIR))
if str(HARNESSES_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESSES_BASE_DIR))

# Real langchain_core.messages — isinstance() needs the real types.
# Pre-loaded so any test that needs HumanMessage/SystemMessage/AIMessage gets
# the real classes regardless of stub ordering.
from harness_common.cli import CLIError  # noqa: E402,F401
from harness_common.sanitize import sanitize_dynamic_params  # noqa: E402,F401

# Real harness_common modules — installed via uv workspace.
from harness_common.thread_id import derive_checkpointer_thread_id  # noqa: E402,F401
from langchain_core.messages import (  # noqa: E402,F401
    AIMessage,
    HumanMessage,
    SystemMessage,
)

# Stub modules NOT in the local .venv:
# - `harness.*` package: the Docker /app/harness/ path; locally session.py + main.py
#   live flat in voice-deepagents/. Stub so `from harness.session import VoiceSession`
#   (in main.py) + `from harness.agent import build_agent` (in session.py) resolve.
# - motor / motor.motor_asyncio: pyproject.toml declares motor>=3.5 but the local
#   .venv was created before that — adding to .venv would require uv sync (blocked
#   by the multi-module pyproject layout). Stub for tests.
# - langgraph.checkpoint.mongodb: same situation (declared post-venv-create).
for mod in [
    "harness",
    "harness.agent",
    "harness.llm_adapter",
    "harness.session",
    "motor",
    "motor.motor_asyncio",
    "langgraph.checkpoint.mongodb",
]:
    sys.modules.setdefault(mod, MagicMock())
