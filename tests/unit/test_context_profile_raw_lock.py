"""Regression test: `PROFILE_CAPS["raw"]` must return `None` for every hint.

Per D-T (LOW resolution from Group D 2026-05-24) — the eval framework's slice_resolver
uses `--context-profile raw` precisely because it wants UNCAPPED entity content for
grounding judgments (IE-5, IE-6 etc.). Any future change that accidentally caps `raw`
would silently truncate source content in the SystemMessage, breaking groundedness
evaluation.

This test catches drift if a future fix accidentally adds a cap to `raw`.
"""

from kernel.api.context_profile import PROFILE_CAPS, cap_for


def test_raw_profile_uncapped_for_every_hint_level():
    """`PROFILE_CAPS["raw"]` returns None for every hint level + None (default)."""
    raw = PROFILE_CAPS["raw"]
    for hint in ("short", "medium", "long", "rich", None):
        assert raw[hint] is None, (
            f"raw profile must not cap any hint level — found cap {raw[hint]!r} "
            f"for hint={hint!r}. Per D-T, eval slice retrieval depends on uncapped raw."
        )


def test_cap_for_raw_returns_none_for_every_hint():
    """The cap_for() function returns None for raw across all hints."""
    for hint in ("short", "medium", "long", "rich", None):
        assert cap_for(hint, "raw") is None, (
            f"cap_for({hint!r}, 'raw') must be None"
        )


def test_llm_profile_caps_remain_set():
    """Sanity check: `PROFILE_CAPS['llm']` IS capped (the contrast to raw)."""
    llm = PROFILE_CAPS["llm"]
    # All hint levels (and None default) should have a cap (numeric).
    for hint in ("short", "medium", "long", "rich", None):
        assert llm[hint] is not None
        assert isinstance(llm[hint], int)
        assert llm[hint] > 0
