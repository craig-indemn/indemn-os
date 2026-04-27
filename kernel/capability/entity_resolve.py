"""entity_resolve — given partial identity signals, return ranked candidate
entities the system already knows about.

Domain-agnostic kernel primitive. The Apr 24 GR Little trace surfaced the
need: associates processing inbound emails / meetings / documents had no
way to ask "which existing Company is this from?" — they improvised
`list --search`, fuzzy-matched in the LLM head, and fell back to `create`
when nothing matched. Result: the 446-Company auto-create explosion (Bug
#16). Without a kernel primitive, every associate skill reinvents this
badly and inconsistently.

Contract: returns ranked candidates with confidence scores. **Never
auto-picks.** Score 1.0 means "this candidate exactly matched a configured
key after normalization"; lower scores are fuzzy matches. The caller
(rule-driven path or LLM-driven path via `--auto`) decides what to do
with the candidates. If multiple candidates tie at score 1.0 — that's
ambiguity, surfaced honestly; the caller handles it (typically: surface
to human review, never silently pick).

Configuration lives in `activated_capabilities` on the entity definition,
matching the pattern used by `auto_classify` and `fetch_new`:

    {
      "capability": "entity_resolve",
      "config": {
        "strategies": [
          {"type": "field_equality", "field": "domain", "normalizer": "domain"},
          {"type": "fuzzy_string", "field": "name", "threshold": 0.85}
        ]
      }
    }

Built-in strategies (kernel-internal fixed set; can grow later):
- `field_equality` — normalize the candidate value, query by equality on
  the field. Score 1.0 per hit. Hits multiple docs → multiple candidates
  tied at 1.0 (the contract; ambiguity is the caller's problem).
- `fuzzy_string` — rapidfuzz token_set_ratio against the field across all
  existing entities of this type. Threshold-gated (default 0.85). Score
  is rapidfuzz ratio / 100 (so a 90% match is 0.9).

Combination: union of candidates by `_id`, max-score across strategies
(so multi-strategy hits don't stack to >1.0; instead the `matched_on`
list grows). Sort descending by score. Return the top N (default 20).

Vector / semantic strategy is intentionally NOT implemented in v1 —
deferred until field_equality + fuzzy_string prove insufficient against
real customer data (Apr 27 plan).
"""

import logging
from typing import Any, Optional

from kernel.capability.registry import register_capability
from kernel.observability.tracing import create_span

logger = logging.getLogger(__name__)


# --- Normalizers ---


def _normalize_email(value: str) -> str:
    """Lowercase, strip whitespace. Conservative — does NOT handle plus
    aliases (foo+tag@bar = foo@bar) because that's not always semantically
    correct (some systems treat aliases as distinct mailboxes)."""
    return value.strip().lower()


def _normalize_domain(value: str) -> str:
    """Lowercase, strip protocol, leading `www.`, trailing slash, path. The
    canonical comparable form of a domain — `https://www.Alliance.com/`
    and `alliance.com` both reduce to `alliance.com`."""
    s = value.strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
    if s.startswith("www."):
        s = s[4:]
    # Strip path/query — keep only the host portion.
    s = s.split("/", 1)[0]
    s = s.split("?", 1)[0]
    return s.rstrip(".")


def _normalize_lowercase_trim(value: str) -> str:
    return value.strip().lower()


def _normalize_none(value: Any) -> Any:
    return value


_NORMALIZERS = {
    "email": _normalize_email,
    "domain": _normalize_domain,
    "lowercase_trim": _normalize_lowercase_trim,
    "none": _normalize_none,
}


def _apply_normalizer(name: Optional[str], value: Any) -> Any:
    """Look up and apply a named normalizer. Unknown name → no-op (safer
    than raising; misconfiguration produces fewer matches, not silent
    crashes)."""
    if value is None:
        return None
    norm = _NORMALIZERS.get(name or "none", _normalize_none)
    if isinstance(value, str):
        return norm(value)
    return value


# --- Strategies ---


async def _strategy_field_equality(
    entity_cls, config: dict, candidate: dict
) -> list[dict]:
    """Field-equality strategy. Returns candidates with score 1.0.

    config keys:
      - `field`: the entity field to match on
      - `normalizer` (optional): one of `email`, `domain`, `lowercase_trim`, `none`

    candidate must contain the configured field (else this strategy
    contributes nothing). The candidate value is normalized; entities
    are loaded and their stored field value normalized too, then compared.
    Loading + normalizing in Python is necessary because the stored value
    may not be normalized at write time.
    """
    field = config.get("field")
    if not field:
        return []
    if field not in candidate:
        return []

    cand_value = _apply_normalizer(config.get("normalizer"), candidate[field])
    if cand_value is None:
        return []

    # Equality match in Mongo first to narrow the set, then re-check after
    # normalization in Python (because stored values may not be normalized).
    # If we knew the stored values were always-normalized we could query
    # directly with the normalized candidate. We don't, so load broader.
    matches: list[dict] = []
    # If the field has an index, Mongo returns fast even for large collections.
    # We pull all matching the un-normalized candidate value first; then we
    # sweep for stored values that normalize to the same canonical form.
    seen_ids: set[str] = set()

    # Direct (already-normalized at write time) hit.
    direct = await entity_cls.find_scoped({field: cand_value}).limit(50).to_list()
    for e in direct:
        eid = str(e.id) if hasattr(e, "id") else str(e._id)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        matches.append(
            {
                "_id": eid,
                "score": 1.0,
                "matched_on": [field],
                "matched_value": cand_value,
                "entity": e,
            }
        )

    # Best-effort sweep for stored values that normalize but weren't already
    # canonical (e.g., "https://Alliance.com" stored, "alliance.com"
    # candidate). Bounded to avoid scanning enormous collections.
    if config.get("normalizer") and config["normalizer"] != "none":
        # Only do this sweep when normalization is non-trivial.
        all_recent = await entity_cls.find_scoped({}).sort("-created_at").limit(500).to_list()
        for e in all_recent:
            eid = str(e.id) if hasattr(e, "id") else str(e._id)
            if eid in seen_ids:
                continue
            stored = getattr(e, field, None)
            if stored is None:
                continue
            stored_norm = _apply_normalizer(config.get("normalizer"), stored)
            if stored_norm == cand_value:
                seen_ids.add(eid)
                matches.append(
                    {
                        "_id": eid,
                        "score": 1.0,
                        "matched_on": [field],
                        "matched_value": cand_value,
                        "entity": e,
                    }
                )

    return matches


async def _strategy_fuzzy_string(
    entity_cls, config: dict, candidate: dict
) -> list[dict]:
    """Fuzzy-string strategy via rapidfuzz `token_set_ratio`.

    config keys:
      - `field`: the entity field to compare against
      - `threshold` (optional, default 0.85): ratio below this → not a match
      - `limit` (optional, default 500): max entities to compare against
        per call. Bounded so a 100k-row collection doesn't make this O(n)
        on every call. If a domain needs more, vector search is the next
        layer.

    Score is `ratio / 100` so the caller can compare equally against
    field_equality scores (which are 1.0).
    """
    from rapidfuzz import fuzz

    field = config.get("field")
    if not field or field not in candidate:
        return []
    cand_value = candidate[field]
    if not isinstance(cand_value, str) or not cand_value.strip():
        return []

    threshold = float(config.get("threshold", 0.85))
    limit = int(config.get("limit", 500))

    # Pull candidates to compare against. Sort by recency so the most
    # likely matches are checked even if the limit caps before scanning
    # the whole collection.
    pool = await entity_cls.find_scoped({}).sort("-created_at").limit(limit).to_list()

    results: list[dict] = []
    for e in pool:
        stored = getattr(e, field, None)
        if not isinstance(stored, str) or not stored.strip():
            continue
        ratio = fuzz.token_set_ratio(cand_value, stored)
        score = ratio / 100.0
        if score < threshold:
            continue
        eid = str(e.id) if hasattr(e, "id") else str(e._id)
        results.append(
            {
                "_id": eid,
                "score": score,
                "matched_on": [field],
                "matched_value": stored,
                "entity": e,
            }
        )
    return results


_STRATEGY_DISPATCH = {
    "field_equality": _strategy_field_equality,
    "fuzzy_string": _strategy_fuzzy_string,
}


# --- Capability entry point ---


def _summarize_entity(e) -> dict:
    """Return a small summary of the entity suitable for inclusion in
    the resolve response. Just identifying fields so the caller doesn't
    have to follow up with another GET. Full entity is fetchable via
    `indemn <slug> get <id>` when needed."""
    summary: dict = {}
    for attr in ("name", "title", "domain", "email", "subject"):
        val = getattr(e, attr, None)
        if val is not None:
            summary[attr] = val
    return summary


async def entity_resolve(entity_cls, config: dict, org_id, params: dict = {}) -> dict:
    """Run configured strategies, combine results, return ranked candidates.

    params: {"candidate": {<field>: <value>, ...}, "limit": int (optional)}
    config: {"strategies": [{...}, ...]}

    Returns:
        {
            "candidates": [
                {"_id": "...", "score": 1.0, "matched_on": ["domain"],
                 "summary": {"name": "...", "domain": "..."}},
                ...
            ],
            "strategy_count": N,
            "candidate_keys": ["domain", "name"]
        }
    """
    with create_span("capability.entity_resolve", entity_type=entity_cls.__name__):
        candidate = params.get("candidate", {})
        if not isinstance(candidate, dict) or not candidate:
            return {"candidates": [], "strategy_count": 0, "candidate_keys": []}

        strategies = config.get("strategies", [])
        if not strategies:
            return {"candidates": [], "strategy_count": 0, "candidate_keys": list(candidate.keys())}

        # Run each strategy, accumulate by _id with max-score + union of matched_on.
        merged: dict[str, dict] = {}
        for strat in strategies:
            stype = strat.get("type")
            fn = _STRATEGY_DISPATCH.get(stype)
            if fn is None:
                logger.warning("entity_resolve: unknown strategy type %r — skipping", stype)
                continue
            try:
                hits = await fn(entity_cls, strat, candidate)
            except Exception as e:  # noqa: BLE001
                logger.warning("entity_resolve: strategy %s raised %s — skipping", stype, e)
                continue
            for hit in hits:
                eid = hit["_id"]
                if eid in merged:
                    existing = merged[eid]
                    existing["score"] = max(existing["score"], hit["score"])
                    # Union of matched_on, preserving order seen.
                    for f in hit["matched_on"]:
                        if f not in existing["matched_on"]:
                            existing["matched_on"].append(f)
                else:
                    merged[eid] = {
                        "_id": eid,
                        "score": hit["score"],
                        "matched_on": list(hit["matched_on"]),
                        "summary": _summarize_entity(hit["entity"]),
                    }

        ranked = sorted(merged.values(), key=lambda c: c["score"], reverse=True)
        limit = int(params.get("limit", 20))
        return {
            "candidates": ranked[:limit],
            "strategy_count": len(strategies),
            "candidate_keys": list(candidate.keys()),
        }


register_capability("entity_resolve", entity_resolve)
