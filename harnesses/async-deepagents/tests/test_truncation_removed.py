"""Pin: the async-deepagents harness no longer applies field-level
truncation client-side.

Truncation moved to the kernel response serializer driven by FieldDefinition
`content_size_hint` + `?context_profile=llm` query param. The harness now
trusts the kernel's response.

These tests fail if anyone reintroduces `_truncate_large_fields`, or
forgets to pass `--context-profile llm` on the entity fetch, or removes
the Trace-entity pop (which is SEPARATE from field truncation — it drops
JSON-structured fields that the size-hint policy can't reach).
"""

import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mirror the import setup used by sibling test_load_message_context.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for mod in [
    "deepagents",
    "harness",
    "harness.agent",
    "harness.completion_logic",
    "harness.cron_runner",
    "harness.trace_helpers",
    "harness_common",
    "harness_common.backend",
    "harness_common.cli",
    "harness_common.runtime",
    "langchain",
    "langchain.chat_models",
]:
    sys.modules.setdefault(mod, MagicMock())

sys.modules["harness_common.runtime"].RUNTIME_ID = "test-runtime"

import main as main_module  # noqa: E402


def test_harness_does_not_define_truncate_large_fields():
    """Pin: `_truncate_large_fields` and `_FIELD_TRUNCATE_LIMIT` removed.
    Truncation is now the kernel's job per the architectural shift.
    Reintroducing either symbol re-couples the harness to per-entity policy."""
    src = inspect.getsource(main_module)
    assert "_truncate_large_fields" not in src
    assert "_FIELD_TRUNCATE_LIMIT" not in src


def test_harness_passes_context_profile_llm_on_fetch():
    """Pin: the `_load_message_context` function requests
    `--context-profile llm` so the kernel caps per-field per the
    FieldDefinition.content_size_hint metadata."""
    src = inspect.getsource(main_module)
    assert '"--context-profile"' in src
    assert '"llm"' in src


def test_harness_preserves_trace_field_pop():
    """Pin: the Trace-entity pop of `inputs / outputs / child_runs`
    stays. These are JSON-structured fields (lists of dicts), not plain
    strings — kernel truncation can't help. The harness must still drop
    them before injecting Trace context into the LLM prompt."""
    src = inspect.getsource(main_module)
    assert 'if entity_type == "Trace":' in src
    for field in ("inputs", "outputs", "child_runs"):
        assert f'"{field}"' in src
