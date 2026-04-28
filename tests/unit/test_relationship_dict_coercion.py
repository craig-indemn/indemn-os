"""Tests for the API-boundary relationship-field coercion (Bug #9).

LLMs routinely call `indemn email update <id> --data '{"company": {"name":
"Acme"}}'` — passing a dict where an ObjectId is expected. Pre-fix the kernel
returned a Pydantic `is_instance_of` error and the associate's message
dead-lettered. The fix catches the dict at the API boundary BEFORE construction
and either:

  - rejects with HTTPException 400 + a shape hint pointing to entity-resolve
  - (when the field opts in via `auto_resolve=true`) calls the target's
    entity_resolve capability; auto-links only on a single 1.0 candidate;
    otherwise 400 listing the ambiguous / fuzzy candidates so the caller
    sees what's missing

These tests pin both paths plus the no-op cases (string hex, ObjectId, None,
non-relationship fields). Mock entity_resolve so the unit test stays pure
and doesn't require MongoDB.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

from kernel.api.registration import _resolve_relationship_dict_inputs


# --- Fixtures ---


def _entity_cls(
    name="Email",
    relationships=None,
    auto_resolve=None,
):
    """Build an entity_cls stand-in. relationships maps field_name -> target_name.
    auto_resolve is a set of field names that opt into entity_resolve fallback."""
    cls = SimpleNamespace(
        __name__=name,
        _relationship_targets=relationships or {},
        _auto_resolve_fields=auto_resolve or set(),
    )
    return cls


def _target_cls(activated=None):
    """Build a target entity_cls stand-in with activated_capabilities."""
    return SimpleNamespace(_activated_capabilities=activated or [])


def _activation(name, config=None):
    return SimpleNamespace(capability=name, config=config or {})


# --- No-op cases (idempotent) ---


@pytest.mark.asyncio
async def test_non_relationship_dict_passes_through():
    """A dict on a NON-relationship field (e.g., a `data` blob) is fine — the
    coercion only triggers on relationship fields."""
    cls = _entity_cls(relationships={})
    data = {"data": {"foo": "bar"}}
    result = await _resolve_relationship_dict_inputs(cls, "Email", data)
    assert result == {"data": {"foo": "bar"}}


@pytest.mark.asyncio
async def test_string_hex_on_relationship_passes_through():
    """A 24-char hex string is the canonical happy-path value — _coerce_objectid_fields
    handles it elsewhere; this helper is for the dict case only."""
    cls = _entity_cls(relationships={"company": "Company"})
    data = {"company": "69eb95f22b0a508618923977"}
    result = await _resolve_relationship_dict_inputs(cls, "Email", data)
    assert result == {"company": "69eb95f22b0a508618923977"}


@pytest.mark.asyncio
async def test_objectid_value_passes_through():
    cls = _entity_cls(relationships={"company": "Company"})
    oid = ObjectId()
    data = {"company": oid}
    result = await _resolve_relationship_dict_inputs(cls, "Email", data)
    assert result["company"] is oid


@pytest.mark.asyncio
async def test_none_value_passes_through():
    """null on a relationship field means "unset" — don't coerce or reject."""
    cls = _entity_cls(relationships={"company": "Company"})
    data = {"company": None}
    result = await _resolve_relationship_dict_inputs(cls, "Email", data)
    assert result == {"company": None}


@pytest.mark.asyncio
async def test_field_not_in_data_skipped():
    """If `data` doesn't include the field at all, the helper does nothing."""
    cls = _entity_cls(relationships={"company": "Company"})
    data = {"subject": "test"}
    result = await _resolve_relationship_dict_inputs(cls, "Email", data)
    assert result == {"subject": "test"}


# --- Default reject path (no auto_resolve) ---


@pytest.mark.asyncio
async def test_dict_on_relationship_field_400_with_shape_hint():
    """The Bug #9 symptom — LLM passes {"name": "Acme"} for an ObjectId field.
    No auto_resolve → 400 with a shape hint that includes the canonical
    `_id` form AND a pointer to entity-resolve."""
    cls = _entity_cls(relationships={"company": "Company"}, auto_resolve=set())
    data = {"company": {"name": "Acme"}}
    with pytest.raises(HTTPException) as exc:
        await _resolve_relationship_dict_inputs(cls, "Email", data)
    assert exc.value.status_code == 400
    detail = str(exc.value.detail)
    assert "company" in detail
    assert "Company" in detail
    assert "_id" in detail or "hex" in detail
    assert "entity-resolve" in detail or "entity_resolve" in detail


@pytest.mark.asyncio
async def test_dict_value_includes_field_value_in_error():
    """The error message includes the raw dict so the caller sees what they
    passed (helpful when an LLM is logging the failure)."""
    cls = _entity_cls(relationships={"company": "Company"})
    data = {"company": {"name": "Oneleet"}}
    with pytest.raises(HTTPException) as exc:
        await _resolve_relationship_dict_inputs(cls, "Email", data)
    assert "Oneleet" in str(exc.value.detail)


# --- auto_resolve path ---


@pytest.mark.asyncio
async def test_auto_resolve_single_1_0_match_coerces_to_objectid():
    """The happy auto_resolve path: dict triggers entity_resolve, exactly one
    candidate at score 1.0 → coerce to that ObjectId. The Bug #31 contract
    is preserved (only auto-pick on unambiguous exact matches)."""
    target_id = "69eb95f22b0a508618923977"
    cls = _entity_cls(
        relationships={"company": "Company"}, auto_resolve={"company"}
    )
    target = _target_cls(
        activated=[_activation("entity_resolve", {"strategies": [{"type": "field_equality"}]})]
    )
    fake_resolve = AsyncMock(
        return_value={
            "candidates": [
                {"_id": target_id, "score": 1.0, "matched_on": ["name"], "summary": {}}
            ],
            "strategy_count": 1,
            "candidate_keys": ["name"],
        }
    )
    data = {"company": {"name": "Acme"}}
    with (
        patch.dict("kernel.db.ENTITY_REGISTRY", {"Company": target}, clear=False),
        patch("kernel.capability.registry.get_capability", return_value=fake_resolve),
    ):
        result = await _resolve_relationship_dict_inputs(cls, "Email", data)
    assert isinstance(result["company"], ObjectId)
    assert str(result["company"]) == target_id
    fake_resolve.assert_called_once()


@pytest.mark.asyncio
async def test_auto_resolve_zero_candidates_raises_400():
    """auto_resolve found no matches → 400 with a "no match" hint pointing
    to creating the target entity first."""
    cls = _entity_cls(
        relationships={"company": "Company"}, auto_resolve={"company"}
    )
    target = _target_cls(activated=[_activation("entity_resolve", {})])
    fake_resolve = AsyncMock(
        return_value={"candidates": [], "strategy_count": 1, "candidate_keys": ["name"]}
    )
    with (
        patch.dict("kernel.db.ENTITY_REGISTRY", {"Company": target}, clear=False),
        patch("kernel.capability.registry.get_capability", return_value=fake_resolve),
    ):
        with pytest.raises(HTTPException) as exc:
            await _resolve_relationship_dict_inputs(
                cls, "Email", {"company": {"name": "DoesNotExist"}}
            )
    assert exc.value.status_code == 400
    detail = str(exc.value.detail)
    assert "no" in detail.lower() and "match" in detail.lower()
    assert "DoesNotExist" in detail


@pytest.mark.asyncio
async def test_auto_resolve_multiple_1_0_candidates_raises_400_with_list():
    """Multiple 1.0 candidates → ambiguous → 400 listing them. Honors Bug #31's
    "never auto-pick" contract — the kernel surfaces ambiguity, the caller
    decides."""
    cls = _entity_cls(
        relationships={"company": "Company"}, auto_resolve={"company"}
    )
    target = _target_cls(activated=[_activation("entity_resolve", {})])
    fake_resolve = AsyncMock(
        return_value={
            "candidates": [
                {"_id": "69e000000000000000000001", "score": 1.0, "matched_on": ["name"], "summary": {"name": "Acme A"}},
                {"_id": "69e000000000000000000002", "score": 1.0, "matched_on": ["name"], "summary": {"name": "Acme B"}},
            ],
            "strategy_count": 1,
            "candidate_keys": ["name"],
        }
    )
    with (
        patch.dict("kernel.db.ENTITY_REGISTRY", {"Company": target}, clear=False),
        patch("kernel.capability.registry.get_capability", return_value=fake_resolve),
    ):
        with pytest.raises(HTTPException) as exc:
            await _resolve_relationship_dict_inputs(
                cls, "Email", {"company": {"name": "Acme"}}
            )
    detail = str(exc.value.detail)
    assert exc.value.status_code == 400
    assert "ambiguous" in detail.lower() or "2 " in detail
    assert "Acme A" in detail or "69e000000000000000000001" in detail


@pytest.mark.asyncio
async def test_auto_resolve_only_fuzzy_candidates_raises_400():
    """No exact (1.0) match, only fuzzy → 400 with top fuzzy listed. Same
    "never auto-pick" rationale: <1.0 means probabilistic, the kernel doesn't
    decide."""
    cls = _entity_cls(
        relationships={"company": "Company"}, auto_resolve={"company"}
    )
    target = _target_cls(activated=[_activation("entity_resolve", {})])
    fake_resolve = AsyncMock(
        return_value={
            "candidates": [
                {"_id": "69e000000000000000000003", "score": 0.94, "matched_on": ["name"], "summary": {"name": "Acmey"}},
                {"_id": "69e000000000000000000004", "score": 0.88, "matched_on": ["name"], "summary": {"name": "Acmé"}},
            ],
            "strategy_count": 1,
            "candidate_keys": ["name"],
        }
    )
    with (
        patch.dict("kernel.db.ENTITY_REGISTRY", {"Company": target}, clear=False),
        patch("kernel.capability.registry.get_capability", return_value=fake_resolve),
    ):
        with pytest.raises(HTTPException) as exc:
            await _resolve_relationship_dict_inputs(
                cls, "Email", {"company": {"name": "Acme"}}
            )
    detail = str(exc.value.detail)
    assert exc.value.status_code == 400
    assert "fuzzy" in detail.lower() or "0.9" in detail
    assert "Acmey" in detail or "69e000000000000000000003" in detail


# --- auto_resolve config edge cases ---


@pytest.mark.asyncio
async def test_auto_resolve_target_not_registered_raises_400():
    """Field has auto_resolve=true but the target entity type isn't registered
    in the org → 400 explaining the situation. (Edge case, but possible during
    org migrations.)"""
    cls = _entity_cls(
        relationships={"company": "Company"}, auto_resolve={"company"}
    )
    with patch.dict("kernel.db.ENTITY_REGISTRY", {}, clear=True):
        with pytest.raises(HTTPException) as exc:
            await _resolve_relationship_dict_inputs(
                cls, "Email", {"company": {"name": "Acme"}}
            )
    assert exc.value.status_code == 400
    assert "not registered" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_auto_resolve_target_lacks_entity_resolve_capability_raises_400():
    """Field has auto_resolve=true but the target hasn't activated
    entity_resolve → 400 telling the caller to activate it. Avoids silent
    fallthrough where the API just rejects with no useful hint."""
    cls = _entity_cls(
        relationships={"company": "Company"}, auto_resolve={"company"}
    )
    target = _target_cls(
        activated=[_activation("auto_classify", {})]  # different capability
    )
    with patch.dict("kernel.db.ENTITY_REGISTRY", {"Company": target}, clear=False):
        with pytest.raises(HTTPException) as exc:
            await _resolve_relationship_dict_inputs(
                cls, "Email", {"company": {"name": "Acme"}}
            )
    assert exc.value.status_code == 400
    detail = str(exc.value.detail)
    assert "entity_resolve" in detail
    assert "activate" in detail.lower() or "enable" in detail.lower()


# --- Multiple relationship fields in one payload ---


@pytest.mark.asyncio
async def test_one_bad_field_among_many_still_rejects():
    """If any relationship field is dict-shaped, the helper raises on it.
    No silent partial-coercion."""
    cls = _entity_cls(relationships={"company": "Company", "deal": "Deal"})
    data = {
        "company": "69eb95f22b0a508618923977",  # OK
        "deal": {"name": "Some deal"},  # BAD
    }
    with pytest.raises(HTTPException) as exc:
        await _resolve_relationship_dict_inputs(cls, "Email", data)
    assert "deal" in str(exc.value.detail)
