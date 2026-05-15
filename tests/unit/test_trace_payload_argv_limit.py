"""Pin _create_trace's always-use-tempfile behavior.

Linux caps a single argv entry at MAX_ARG_STRLEN = 32 * PAGE_SIZE = 131072
bytes regardless of total ARG_MAX. Trace payloads (full agent conversation
messages + child_runs + entity inputs) frequently exceed 128KB — Evaluator
runs especially, because their input context includes the full target Trace.

Pre-fix, _create_trace had a 200K threshold: payloads >200K used a tempfile,
else passed --data directly. Payloads between 128K and 200K hit
`[Errno 7] Argument list too long`. The Trace creation logged a non-blocking
warning ("Trace creation failed: ...") and the Trace entity was silently lost.

Fix: always tempfile. One code path. The threshold (and the bug class it
enabled) is gone. These tests pin that the branch was removed so a future
refactor doesn't reintroduce it.
"""

from pathlib import Path


_MAIN_PY = (
    Path(__file__).resolve().parents[2]
    / "harnesses"
    / "async-deepagents"
    / "main.py"
)


def _get_create_trace_source() -> str:
    """Extract just the _create_trace function source from main.py."""
    src = _MAIN_PY.read_text()
    start = src.index("async def _create_trace(")
    end = src.index("\n\nasync def ", start + 100)  # next async function
    return src[start:end]


def test_create_trace_always_uses_data_file():
    """_create_trace must use --data-file (tempfile path). The pre-fix
    code also had a branch using --data directly; that branch is gone."""
    src = _get_create_trace_source()
    assert "--data-file" in src, "_create_trace must use --data-file path"


def test_create_trace_has_no_inline_data_branch():
    """Pin against regression: no `--data` (without -file) call inside
    _create_trace. The old code had `indemn(..., "--data", payload, ...)`
    which fails for payloads >128KB on Linux."""
    src = _get_create_trace_source()
    # Look for any '"--data"' string followed by a non-"-file" suffix in the
    # function. The valid call is '"--data-file"'. We want NO bare '"--data"'.
    import re
    # Match `"--data"` not followed by `-file`
    bad_match = re.search(r'"--data"(?!-file)', src)
    assert bad_match is None, (
        f"Inline --data branch detected — reintroduces argv-too-long bug "
        f"for payloads between 128KB and the old threshold. Found near: "
        f"{src[max(0, bad_match.start()-50):bad_match.end()+50]!r}"
    )


def test_create_trace_has_no_size_threshold():
    """The 200_000 size threshold was the footgun. Verify it's gone so we
    don't accidentally reintroduce branching on payload size."""
    src = _get_create_trace_source()
    # Defensive: catch the literal magic number AND the common refactors
    bad_patterns = ["200_000", "200000", "131_072", "131072"]
    for pat in bad_patterns:
        assert pat not in src, (
            f"Size threshold {pat!r} reintroduced. Tempfile is the only "
            f"path — no branching."
        )


def test_create_trace_cleans_up_tempfile():
    """The tempfile must be unlinked in a finally block — without this,
    long-running runtimes accumulate orphan .json files in /tmp."""
    src = _get_create_trace_source()
    assert "finally:" in src and "os.unlink(tmp_path)" in src, (
        "_create_trace must unlink the tempfile in a finally block"
    )
