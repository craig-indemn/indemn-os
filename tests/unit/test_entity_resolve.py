"""Tests for kernel.capability.entity_resolve.

The Apr 24 GR Little trace surfaced the need for this primitive:
associates processing inbound emails / meetings had no way to ask
"which existing Company is this from?" — they improvised, fuzzy-matched
in the LLM head, and fell back to `create`. The 446-Company explosion
(Bug #16) was the symptom; the missing kernel primitive was the cause.

This module pins the new primitive's behavior:
  - normalizers (email, domain, lowercase_trim, none)
  - field_equality strategy (exact match after normalization, score 1.0)
  - fuzzy_string strategy (rapidfuzz token_set_ratio, threshold-gated)
  - multi-strategy combination (max score per _id, union of matched_on)
  - the contract that the capability NEVER auto-picks — even ties at
    score 1.0 are returned as multiple candidates so the caller decides.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernel.capability.entity_resolve import (
    _apply_normalizer,
    _normalize_domain,
    _normalize_email,
    _normalize_lowercase_trim,
    _strategy_field_equality,
    _strategy_fuzzy_string,
    entity_resolve,
)


# --- Normalizer tests ---


class TestNormalizers:
    def test_email_lowercases_and_strips(self):
        assert _normalize_email("  Foo@Bar.COM  ") == "foo@bar.com"

    def test_email_preserves_plus_aliases(self):
        # Conservative: don't collapse foo+tag@bar to foo@bar — some systems
        # treat aliases as distinct mailboxes.
        assert _normalize_email("foo+tag@bar.com") == "foo+tag@bar.com"

    def test_domain_strips_protocol_www_path(self):
        assert _normalize_domain("https://www.Alliance.com/about?x=1") == "alliance.com"

    def test_domain_handles_bare_domain(self):
        assert _normalize_domain("alliance.com") == "alliance.com"

    def test_domain_handles_http_no_www(self):
        assert _normalize_domain("http://alliance.com") == "alliance.com"

    def test_domain_strips_trailing_dot(self):
        assert _normalize_domain("alliance.com.") == "alliance.com"

    def test_lowercase_trim(self):
        assert _normalize_lowercase_trim("  Alliance Insurance  ") == "alliance insurance"

    def test_apply_normalizer_unknown_name_falls_back_to_no_op(self):
        """Unknown normalizer name — no-op rather than crash, so a
        misconfiguration produces fewer matches (never wrong matches)."""
        assert _apply_normalizer("not-a-real-normalizer", "Foo") == "Foo"

    def test_apply_normalizer_handles_none(self):
        assert _apply_normalizer("email", None) is None

    def test_apply_normalizer_passes_through_non_strings(self):
        """ObjectId / int / dict values shouldn't be string-normalized."""
        assert _apply_normalizer("email", 42) == 42


# --- Test helpers for strategies ---


def _entity(eid: str, **fields):
    """Build an entity stand-in. Real domain entities have id + arbitrary
    fields; SimpleNamespace mirrors the relevant access shape."""
    e = SimpleNamespace(id=eid, _id=eid, **fields)
    return e


def _entity_cls(by_filter: dict = None, all_recent: list = None):
    """Build an entity class stand-in with `find_scoped(filter)` returning
    a chainable mock that ends in `.to_list()` returning the configured set.

    by_filter: dict mapping JSON-stringified filter → list of entities
    all_recent: list returned for find_scoped({}).sort(...).limit(...).to_list()
    """
    by_filter = by_filter or {}
    all_recent = all_recent or []

    cls = MagicMock()
    cls.__name__ = "Sample"

    def _find_scoped(filt):
        import json

        key = json.dumps(filt, sort_keys=True, default=str)

        chain = MagicMock()
        chain.sort = MagicMock(return_value=chain)
        chain.limit = MagicMock(return_value=chain)
        if filt == {}:
            chain.to_list = AsyncMock(return_value=all_recent)
        else:
            chain.to_list = AsyncMock(return_value=by_filter.get(key, []))
        return chain

    cls.find_scoped = _find_scoped
    return cls


# --- field_equality strategy ---


@pytest.mark.asyncio
class TestFieldEqualityStrategy:
    async def test_returns_empty_when_field_not_in_candidate(self):
        cls = _entity_cls()
        result = await _strategy_field_equality(
            cls, {"field": "domain", "normalizer": "domain"}, {"name": "Acme"}
        )
        assert result == []

    async def test_returns_empty_when_no_field_configured(self):
        cls = _entity_cls()
        result = await _strategy_field_equality(cls, {}, {"domain": "alliance.com"})
        assert result == []

    async def test_returns_empty_when_candidate_value_is_none(self):
        cls = _entity_cls()
        result = await _strategy_field_equality(
            cls, {"field": "domain"}, {"domain": None}
        )
        assert result == []

    async def test_returns_score_1_for_normalized_match(self):
        """Candidate `https://www.Alliance.com/` and stored `alliance.com`
        both normalize to `alliance.com` — score 1.0 hit."""
        import json

        # Direct equality query for the normalized value returns the entity.
        match_entity = _entity("69abc...", domain="alliance.com")
        cls = _entity_cls(
            by_filter={
                json.dumps({"domain": "alliance.com"}, sort_keys=True): [match_entity]
            }
        )
        result = await _strategy_field_equality(
            cls,
            {"field": "domain", "normalizer": "domain"},
            {"domain": "https://www.Alliance.com/"},
        )
        assert len(result) == 1
        assert result[0]["_id"] == "69abc..."
        assert result[0]["score"] == 1.0
        assert result[0]["matched_on"] == ["domain"]
        assert result[0]["matched_value"] == "alliance.com"

    async def test_returns_multiple_candidates_when_multiple_matches(self):
        """Two entities with the same normalized value → both returned at
        score 1.0. Ambiguity is the caller's problem; the capability
        surfaces it honestly."""
        import json

        e1 = _entity("a", domain="alliance.com")
        e2 = _entity("b", domain="alliance.com")
        cls = _entity_cls(
            by_filter={
                json.dumps({"domain": "alliance.com"}, sort_keys=True): [e1, e2]
            }
        )
        result = await _strategy_field_equality(
            cls,
            {"field": "domain", "normalizer": "domain"},
            {"domain": "alliance.com"},
        )
        assert len(result) == 2
        assert {r["_id"] for r in result} == {"a", "b"}
        assert all(r["score"] == 1.0 for r in result)


# --- fuzzy_string strategy ---


@pytest.mark.asyncio
class TestFuzzyStringStrategy:
    async def test_returns_empty_when_field_not_in_candidate(self):
        cls = _entity_cls()
        result = await _strategy_fuzzy_string(
            cls, {"field": "name", "threshold": 0.85}, {"domain": "x"}
        )
        assert result == []

    async def test_returns_empty_when_no_field_configured(self):
        cls = _entity_cls()
        result = await _strategy_fuzzy_string(cls, {}, {"name": "Acme"})
        assert result == []

    async def test_returns_empty_when_value_is_not_string(self):
        cls = _entity_cls()
        result = await _strategy_fuzzy_string(
            cls, {"field": "name"}, {"name": 12345}
        )
        assert result == []

    async def test_filters_below_threshold(self):
        """Stored "Banana Bread" vs candidate "Alliance Insurance" — well
        below any reasonable threshold; not returned."""
        e = _entity("a", name="Banana Bread")
        cls = _entity_cls(all_recent=[e])
        result = await _strategy_fuzzy_string(
            cls,
            {"field": "name", "threshold": 0.85},
            {"name": "Alliance Insurance"},
        )
        assert result == []

    async def test_returns_high_score_for_close_match(self):
        """Stored "Alliance Insurance Services Inc" vs candidate
        "Alliance Insurance" — close enough that token_set_ratio hits
        well above 0.85."""
        e = _entity("a", name="Alliance Insurance Services Inc")
        cls = _entity_cls(all_recent=[e])
        result = await _strategy_fuzzy_string(
            cls,
            {"field": "name", "threshold": 0.85},
            {"name": "Alliance Insurance"},
        )
        assert len(result) == 1
        assert result[0]["_id"] == "a"
        assert 0.85 <= result[0]["score"] <= 1.0
        assert result[0]["matched_on"] == ["name"]

    async def test_skips_entities_with_missing_field(self):
        """Entity with the field unset is silently skipped (not crashed on)."""
        e1 = _entity("a", name=None)
        e2 = _entity("b", name="Alliance")
        cls = _entity_cls(all_recent=[e1, e2])
        result = await _strategy_fuzzy_string(
            cls, {"field": "name", "threshold": 0.5}, {"name": "Alliance"}
        )
        assert {r["_id"] for r in result} == {"b"}


# --- entity_resolve combination ---


@pytest.mark.asyncio
class TestEntityResolveCombination:
    async def test_no_strategies_returns_empty(self):
        cls = _entity_cls()
        result = await entity_resolve(
            cls, {"strategies": []}, "org_x", {"candidate": {"name": "x"}}
        )
        assert result["candidates"] == []
        assert result["strategy_count"] == 0

    async def test_no_candidate_returns_empty(self):
        cls = _entity_cls()
        result = await entity_resolve(
            cls,
            {"strategies": [{"type": "field_equality", "field": "name"}]},
            "org_x",
            {},
        )
        assert result["candidates"] == []

    async def test_unknown_strategy_skipped_not_crashed(self):
        cls = _entity_cls()
        result = await entity_resolve(
            cls,
            {"strategies": [{"type": "no_such_strategy"}]},
            "org_x",
            {"candidate": {"name": "x"}},
        )
        assert result["candidates"] == []
        assert result["strategy_count"] == 1  # Configured count; one was unknown

    async def test_multi_strategy_max_score_per_id(self):
        """Same _id hit by both field_equality (1.0) and fuzzy_string (0.9)
        results in one candidate at score 1.0 with matched_on union."""
        import json

        e = _entity("shared", domain="alliance.com", name="Alliance Insurance")
        cls = _entity_cls(
            by_filter={
                json.dumps({"domain": "alliance.com"}, sort_keys=True): [e]
            },
            all_recent=[e],
        )
        result = await entity_resolve(
            cls,
            {
                "strategies": [
                    {"type": "field_equality", "field": "domain", "normalizer": "domain"},
                    {"type": "fuzzy_string", "field": "name", "threshold": 0.85},
                ]
            },
            "org_x",
            {"candidate": {"domain": "alliance.com", "name": "Alliance Insurance Inc"}},
        )
        assert len(result["candidates"]) == 1
        c = result["candidates"][0]
        assert c["_id"] == "shared"
        assert c["score"] == 1.0  # max across both, never stacks above 1.0
        assert set(c["matched_on"]) == {"domain", "name"}

    async def test_results_sorted_descending_by_score(self):
        """Higher-scoring candidates come first."""
        e_exact = _entity("exact", domain="alliance.com", name="Z")
        e_fuzzy = _entity("fuzzy", domain="other.com", name="Alliance Insurance Inc")
        import json

        cls = _entity_cls(
            by_filter={
                json.dumps({"domain": "alliance.com"}, sort_keys=True): [e_exact]
            },
            all_recent=[e_exact, e_fuzzy],
        )
        result = await entity_resolve(
            cls,
            {
                "strategies": [
                    {"type": "field_equality", "field": "domain", "normalizer": "domain"},
                    {"type": "fuzzy_string", "field": "name", "threshold": 0.85},
                ]
            },
            "org_x",
            {"candidate": {"domain": "alliance.com", "name": "Alliance Insurance"}},
        )
        scores = [c["score"] for c in result["candidates"]]
        assert scores == sorted(scores, reverse=True)
        assert result["candidates"][0]["_id"] == "exact"
        assert result["candidates"][0]["score"] == 1.0

    async def test_response_includes_summary_for_caller_context(self):
        """Each candidate carries a small summary so the caller doesn't
        have to follow up with a GET. Summary includes name/title/domain
        when present."""
        import json

        e = _entity("a", domain="alliance.com", name="Alliance Insurance")
        cls = _entity_cls(
            by_filter={
                json.dumps({"domain": "alliance.com"}, sort_keys=True): [e]
            }
        )
        result = await entity_resolve(
            cls,
            {"strategies": [{"type": "field_equality", "field": "domain"}]},
            "org_x",
            {"candidate": {"domain": "alliance.com"}},
        )
        c = result["candidates"][0]
        assert c["summary"]["name"] == "Alliance Insurance"
        assert c["summary"]["domain"] == "alliance.com"

    async def test_limit_param_caps_result_count(self):
        """Default limit is 20; explicit limit overrides."""
        entities = [_entity(f"id_{i}", name=f"Acme {i}") for i in range(50)]
        cls = _entity_cls(all_recent=entities)
        result = await entity_resolve(
            cls,
            {"strategies": [{"type": "fuzzy_string", "field": "name", "threshold": 0.5}]},
            "org_x",
            {"candidate": {"name": "Acme 1"}, "limit": 5},
        )
        assert len(result["candidates"]) <= 5

    async def test_two_candidates_tied_at_1_0_both_returned(self):
        """The 'no silent wrong picks' contract: when two entities tie at
        score 1.0, the capability returns both. The caller (rule or LLM)
        deals with the ambiguity — we never auto-pick."""
        import json

        e1 = _entity("a", domain="alliance.com")
        e2 = _entity("b", domain="alliance.com")
        cls = _entity_cls(
            by_filter={
                json.dumps({"domain": "alliance.com"}, sort_keys=True): [e1, e2]
            }
        )
        result = await entity_resolve(
            cls,
            {"strategies": [{"type": "field_equality", "field": "domain"}]},
            "org_x",
            {"candidate": {"domain": "alliance.com"}},
        )
        assert len(result["candidates"]) == 2
        assert all(c["score"] == 1.0 for c in result["candidates"])
        assert {c["_id"] for c in result["candidates"]} == {"a", "b"}
