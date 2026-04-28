"""Tests for entity-skill freshness — _refresh_entity_skill (Bug — stale
entity-skill rendering).

Improvements to `kernel/skill/generator.py` (filter recipes, JSON examples,
entity_resolve section, Bug #6 collection-level capability shapes, etc.)
landed across this round but only reached entities whose definitions were
touched after each deploy. Older entities (Contact `updated_at: 2026-04-18`,
Touchpoint, etc.) served pre-improvement content forever — every associate
reading those skills got stale instructions.

The fix re-renders entity skills at GET time from the current
EntityDefinition + current generator. The stored Skill document is treated
as a cache, not source of truth. These tests pin:

  - entity skills get rewritten when the generator output differs from stored
  - the rewrite is in-memory only (no DB write per read; the helper mutates
    the loaded object but does NOT persist it)
  - associate skills are NEVER touched — their content is authored, not
    generated, and tamper detection still applies
  - if the EntityDefinition has been deleted but the Skill record persists,
    we return the stored fallback content (the only path that still surfaces
    the cached copy)
  - the content_hash is recomputed alongside the content so callers
    inspecting it see a consistent (content, hash) pair
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kernel.api.skill_routes import _refresh_entity_skill


def _skill(
    type="entity",
    name="Email",
    content="OLD CONTENT",
    content_hash="OLD_HASH",
    org_id=None,
):
    """Build a Skill stand-in. We don't instantiate the real Beanie Document
    because it requires init_beanie context — same pattern as the other
    pure-unit tests in this round."""
    return SimpleNamespace(
        type=type,
        name=name,
        content=content,
        content_hash=content_hash,
        org_id=org_id,
    )


# --- Entity skills always re-render ---


@pytest.mark.asyncio
async def test_entity_skill_rerendered_when_generator_output_differs():
    """Generator returns different content → skill.content updates in-memory.
    This is the core property that makes generator improvements propagate
    across all entities."""
    skill = _skill(type="entity", name="Email", content="OLD")
    fake_defn = SimpleNamespace(name="Email", fields={})
    with (
        patch(
            "kernel.entity.definition.EntityDefinition.find_one",
            new=AsyncMock(return_value=fake_defn),
        ),
        patch(
            "kernel.skill.generator.generate_entity_skill",
            return_value="FRESH WITH NEW EXAMPLES",
        ),
    ):
        result = await _refresh_entity_skill(skill)
    assert result.content == "FRESH WITH NEW EXAMPLES"
    assert result.content_hash != "OLD_HASH"


@pytest.mark.asyncio
async def test_entity_skill_no_change_when_generator_matches_stored():
    """If the generator output matches stored content (e.g., the entity was
    just touched), don't recompute the hash — keep the stored values
    unchanged. Hash recomputation only happens when content actually changes."""
    skill = _skill(type="entity", name="Email", content="MATCHING", content_hash="EXISTING_HASH")
    fake_defn = SimpleNamespace(name="Email", fields={})
    with (
        patch(
            "kernel.entity.definition.EntityDefinition.find_one",
            new=AsyncMock(return_value=fake_defn),
        ),
        patch(
            "kernel.skill.generator.generate_entity_skill",
            return_value="MATCHING",
        ),
    ):
        result = await _refresh_entity_skill(skill)
    assert result.content == "MATCHING"
    assert result.content_hash == "EXISTING_HASH"  # untouched


@pytest.mark.asyncio
async def test_entity_skill_hash_updated_alongside_content():
    """When content changes, the hash must change too — callers inspecting
    the (content, hash) pair should see a consistent rewrite."""
    skill = _skill(type="entity", content="OLD", content_hash="OLD_HASH")
    fake_defn = SimpleNamespace(name="Email", fields={})
    with (
        patch(
            "kernel.entity.definition.EntityDefinition.find_one",
            new=AsyncMock(return_value=fake_defn),
        ),
        patch(
            "kernel.skill.generator.generate_entity_skill",
            return_value="NEW MARKDOWN",
        ),
    ):
        result = await _refresh_entity_skill(skill)

    from kernel.skill.integrity import compute_content_hash

    expected_hash = compute_content_hash("NEW MARKDOWN")
    assert result.content_hash == expected_hash


# --- Associate skills are never touched ---


@pytest.mark.asyncio
async def test_associate_skill_passes_through_unchanged():
    """Associate skills are authored — never re-render them. If we did, an
    associate skill update could be silently overwritten by a phantom
    'generator' that doesn't exist for authored content."""
    skill = _skill(type="associate", name="email-classifier", content="HUMAN AUTHORED")
    # No mocks needed — the helper must short-circuit before touching the
    # generator or EntityDefinition. If it doesn't, the test would hit a
    # real init_beanie call and fail.
    result = await _refresh_entity_skill(skill)
    assert result.content == "HUMAN AUTHORED"
    assert result.content_hash == "OLD_HASH"


# --- Fallback when EntityDefinition is missing ---


@pytest.mark.asyncio
async def test_entity_skill_falls_back_to_stored_when_def_deleted():
    """If the EntityDefinition was deleted but the Skill record persists
    (data integrity gap, but possible during cleanup), serve the stored
    content rather than 500-ing or returning empty. The skill listing was
    going to surface SOMETHING anyway; better the cached content than nothing."""
    skill = _skill(type="entity", name="GhostEntity", content="STORED_FALLBACK")
    with patch(
        "kernel.entity.definition.EntityDefinition.find_one",
        new=AsyncMock(return_value=None),
    ):
        result = await _refresh_entity_skill(skill)
    assert result.content == "STORED_FALLBACK"
    assert result.content_hash == "OLD_HASH"


# --- Org isolation in EntityDefinition lookup ---


@pytest.mark.asyncio
async def test_entity_def_lookup_scoped_to_skill_org_id():
    """The EntityDefinition lookup uses the SKILL's org_id, not the request
    contextvar — so two orgs with the same entity name don't cross-contaminate
    each other's skills. Pin the lookup arg shape so this can't drift."""
    skill_org = "69e000000000000000000abc"
    skill = _skill(type="entity", name="Email", org_id=skill_org)
    fake_defn = SimpleNamespace(name="Email", fields={})
    captured_filter = []

    async def fake_find_one(filter_doc):
        captured_filter.append(filter_doc)
        return fake_defn

    with (
        patch(
            "kernel.entity.definition.EntityDefinition.find_one",
            new=fake_find_one,
        ),
        patch("kernel.skill.generator.generate_entity_skill", return_value="X"),
    ):
        await _refresh_entity_skill(skill)
    assert captured_filter == [{"name": "Email", "org_id": skill_org}]
