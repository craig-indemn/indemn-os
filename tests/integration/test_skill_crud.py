"""Integration tests: Skill CRUD — create, list, get, update, deprecate.

Tests the full lifecycle of skill documents against Atlas dev cluster.
"""

import pytest
from bson import ObjectId

from kernel.skill.integrity import compute_content_hash
from kernel.skill.schema import Skill


@pytest.mark.asyncio
async def test_skill_create_and_retrieve(db, org_id, actor):
    """Create a skill and retrieve it by name."""
    content = "# Meeting Extraction\nExtract intelligence from meetings."
    skill = Skill(
        org_id=org_id,
        name="meeting-extraction",
        type="associate",
        content=content,
        content_hash=compute_content_hash(content),
        created_by=str(actor.id),
    )
    await skill.insert()

    # Retrieve by name
    found = await Skill.find_one(
        {"name": "meeting-extraction", "org_id": org_id}
    )
    assert found is not None
    assert found.content == content
    assert found.type == "associate"
    assert found.version == 1
    assert found.status == "active"


@pytest.mark.asyncio
async def test_skill_update_increments_version(db, org_id, actor):
    """Updating skill content increments version and recomputes hash."""
    content_v1 = "# Skill v1\nOriginal content."
    skill = Skill(
        org_id=org_id,
        name="versioned-skill",
        type="associate",
        content=content_v1,
        content_hash=compute_content_hash(content_v1),
        created_by=str(actor.id),
    )
    await skill.insert()

    hash_v1 = skill.content_hash

    # Update content
    content_v2 = "# Skill v2\nImproved content."
    skill.content = content_v2
    skill.content_hash = compute_content_hash(content_v2)
    skill.version += 1
    await skill.save()

    # Verify
    loaded = await Skill.get(skill.id)
    assert loaded.version == 2
    assert loaded.content == content_v2
    assert loaded.content_hash != hash_v1
    assert loaded.content_hash == compute_content_hash(content_v2)


@pytest.mark.asyncio
async def test_skill_deprecation(db, org_id, actor):
    """Deprecating a skill excludes it from active queries."""
    content = "# Deprecated Skill\nNo longer used."
    skill = Skill(
        org_id=org_id,
        name="old-skill",
        type="associate",
        content=content,
        content_hash=compute_content_hash(content),
        created_by=str(actor.id),
    )
    await skill.insert()

    skill.status = "deprecated"
    await skill.save()

    # Active-only query should NOT find it
    active = await Skill.find(
        {"org_id": org_id, "status": "active"}
    ).to_list()
    assert all(s.name != "old-skill" for s in active)


@pytest.mark.asyncio
async def test_skill_org_isolation(db, org_id, actor):
    """Skills from one org are not visible to another."""
    content = "# Isolated Skill"
    skill = Skill(
        org_id=org_id,
        name="org-specific",
        type="associate",
        content=content,
        content_hash=compute_content_hash(content),
        created_by=str(actor.id),
    )
    await skill.insert()

    other_org = ObjectId()
    found = await Skill.find_one(
        {"name": "org-specific", "org_id": other_org}
    )
    assert found is None


@pytest.mark.asyncio
async def test_entity_skill_auto_generation(db, org_id, actor):
    """Entity skills can be generated from entity definitions."""
    from kernel.entity.definition import EntityDefinition, FieldDefinition
    from kernel.skill.generator import generate_entity_skill

    defn = EntityDefinition(
        org_id=org_id,
        name="TestContact",
        collection_name="test_contacts",
        fields={
            "name": FieldDefinition(type="str", required=True),
            "email": FieldDefinition(type="str"),
        },
    )

    markdown = generate_entity_skill("TestContact", defn)
    assert "# TestContact" in markdown
    assert "| name | str | Yes |" in markdown
    assert "| email | str | No |" in markdown
    assert "indemn testcontact list" in markdown
