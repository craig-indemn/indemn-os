"""Tests for Bug C — correlation_id hygiene.

**Bug C1** (defensive normalization): A small number of historical Trace entries
had 16-byte OTEL TraceId binary stored as UTF-8 (with replacement chars). The
source code path is no longer active, but now that A+B propagate correlation_id
through every CLI subprocess in the cascade, a binary cid on input would spread
to every change record. `_normalize_correlation_id` catches the corruption at
the activity entry and the Trace creation entry, returning a fresh UUID with a
warning log. These tests pin the helper's behavior.

**Bug C2** (eval Message methodology): the prospective/batch/retroactive paths
in `eval_routes.py` used to set `Message.correlation_id = run_id_str`
(EvaluationRun._id). That broke `indemn trace cascade <id>` for the original
cascade the Trace belongs to, since the Evaluator's run wouldn't share the
cascade root. Fix: each Evaluator message inherits its target Trace's
correlation_id (batch/retroactive) or a fresh cascade cid set as contextvar
before save_tracked (prospective). Batch grouping moves to `causation_id`.
"""

import inspect
import sys
from pathlib import Path

import pytest

HARNESS_DIR = Path(__file__).resolve().parents[2] / "harnesses" / "async-deepagents"
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))


# --- C1 — _normalize_correlation_id ---


def _load_normalize():
    """Pull _normalize_correlation_id by source-parsing main.py.

    main.py imports temporalio + harness + deepagents at module load, none
    available in the test venv. We extract the helper's source and exec it
    in an isolated namespace with stub deps.
    """
    import logging
    import re as re_mod
    import uuid as uuid_mod

    main_py = HARNESS_DIR / "main.py"
    src = main_py.read_text()
    # Locate the helper + its regex pin
    start = src.index("_VALID_CORRELATION_ID_RE = ")
    end = src.index("\n\nasync def _create_trace(", start)
    helper_src = src[start:end]

    ns = {"re": re_mod, "uuid": uuid_mod, "log": logging.getLogger("test")}
    exec(helper_src, ns)
    return ns["_normalize_correlation_id"]


_normalize_correlation_id = _load_normalize()


def test_normalize_passes_through_uuid4():
    """A canonical UUID4 with dashes — pass through unchanged."""
    cid = "1ca24572-f933-489d-83c5-fd7dcba040f2"
    assert _normalize_correlation_id(cid) == cid


def test_normalize_passes_through_hex32():
    """A 32-char hex (no dashes, from uuid.uuid4().hex) — pass through."""
    cid = "1ca24572f933489d83c5fd7dcba040f2"
    assert _normalize_correlation_id(cid) == cid


def test_normalize_passes_through_objectid_hex():
    """24-char ObjectId hex (the Evaluator batch-grouping format pre-C2-fix,
    and still valid as a hex correlation_id) — pass through."""
    cid = "6a07660caf2be37ff6c38dd3"
    assert _normalize_correlation_id(cid) == cid


def test_normalize_replaces_binary_garbage():
    """A string containing U+FFFD replacement chars (the Bug C1 pattern) —
    replaced with a fresh UUID."""
    binary_cid = "Ǹ(#n�G���(_�8�g"
    result = _normalize_correlation_id(binary_cid)
    assert result != binary_cid
    # Result should be a valid UUID4
    import re
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", result)


def test_normalize_replaces_arbitrary_non_hex():
    """Plain non-hex string — replaced. Catches cases like accidentally
    passing an entity name instead of a correlation_id."""
    cid = "this-is-not-hex!"
    result = _normalize_correlation_id(cid)
    assert result != cid


def test_normalize_replaces_non_string():
    """bytes / int input — replaced with fresh UUID. Defense against bypass
    of typing."""
    result_bytes = _normalize_correlation_id(b"\xc7\xb8\x28\x23")
    assert isinstance(result_bytes, str) and len(result_bytes) == 36
    result_int = _normalize_correlation_id(12345)
    assert isinstance(result_int, str) and len(result_int) == 36


def test_normalize_preserves_none():
    """None passes through — callers may pass None when correlation_id is
    unknown; downstream code handles the missing case explicitly."""
    assert _normalize_correlation_id(None) is None


def test_normalize_replaces_empty_string():
    """Empty string is technically valid under the regex (* matches zero+),
    so verify the regex requires non-empty content.

    The fullmatch regex uses + (one or more), so empty string fails and is
    replaced with a fresh UUID."""
    result = _normalize_correlation_id("")
    assert len(result) == 36  # Fresh UUID


# --- C2 — eval_routes Message construction ---


def _read_eval_routes_source():
    return (Path(__file__).resolve().parents[2] / "kernel" / "api" / "eval_routes.py").read_text()


def test_eval_routes_prospective_uses_cascade_cid_not_run_id():
    """Prospective path: Message.correlation_id is the fresh cascade_cid
    that was set as contextvar before save_tracked — NOT run_id_str.
    Pinned via source inspection because eval_routes is async + DB-coupled."""
    src = _read_eval_routes_source()
    # Find the prospective Message constructor
    prospective_start = src.index("event_type=\"prospective_eval\"")
    prospective_end = src.index(")", prospective_start + 200)
    block = src[prospective_start:prospective_end]
    assert "correlation_id=cascade_cid" in block, (
        f"Prospective Message must use cascade_cid (Bug C2 fix). Block:\n{block}"
    )
    assert "correlation_id=run_id_str" not in block, (
        f"Prospective Message must NOT use run_id_str as correlation_id (Bug C2). Block:\n{block}"
    )


def test_eval_routes_batch_uses_trace_correlation_id_not_run_id():
    """Batch/retroactive path: Message.correlation_id is fetched from the
    target Trace's correlation_id field — NOT run_id_str."""
    src = _read_eval_routes_source()
    batch_start = src.index("event_type=\"batch_eval\"")
    batch_end = src.index(")", batch_start + 200)
    block = src[batch_start:batch_end]
    assert "correlation_id=cascade_cid" in block, (
        f"Batch Message must use cascade_cid (Bug C2 fix). Block:\n{block}"
    )
    assert "correlation_id=run_id_str" not in block, (
        f"Batch Message must NOT use run_id_str as correlation_id (Bug C2). Block:\n{block}"
    )


def test_eval_routes_batch_grouping_moves_to_causation_id():
    """run_id_str still carries batch identity, but as causation_id (not
    correlation_id). Pin both eval Message constructors carry it on
    causation_id so 'show me all Evaluator runs for batch X' queries still
    work via causation_id."""
    src = _read_eval_routes_source()
    for event_type in ("prospective_eval", "batch_eval"):
        start = src.index(f'event_type="{event_type}"')
        end = src.index(")", start + 200)
        block = src[start:end]
        assert "causation_id=run_id_str" in block, (
            f"{event_type} Message must carry run_id_str as causation_id. Block:\n{block}"
        )


def test_eval_routes_prospective_sets_contextvar_before_save():
    """Prospective path uses current_correlation_id.set() to inject the
    cascade cid before save_tracked, so the test_entity's creation cascade
    inherits the cascade_cid. Pins the cascade-rooting mechanism."""
    src = _read_eval_routes_source()
    prospective_start = src.index("# Set the cascade correlation_id BEFORE save_tracked")
    prospective_end = src.index("await msg.insert()", prospective_start)
    block = src[prospective_start:prospective_end]
    assert "current_correlation_id.set(cascade_cid)" in block, (
        f"Prospective must set contextvar before save_tracked. Block:\n{block[:500]}"
    )
    # And reset after — required to scope the contextvar to this iteration
    reset_block = src[prospective_end:prospective_end + 200]
    assert "current_correlation_id.reset" in reset_block, (
        f"Prospective must reset contextvar after the iteration. Block:\n{reset_block}"
    )


def test_eval_routes_batch_fetches_trace_correlation_ids():
    """Batch/retroactive path fetches each Trace's correlation_id before
    creating Messages. Pin the trace_cid_map construction."""
    src = _read_eval_routes_source()
    assert "trace_cids_docs = await traces_coll.find(" in src, (
        "Batch path must fetch each trace's correlation_id"
    )
    assert "trace_cid_map = {" in src, (
        "Batch path must build a {_id -> correlation_id} map for lookup"
    )
