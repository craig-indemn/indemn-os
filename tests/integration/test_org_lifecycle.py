"""Integration tests: Org lifecycle — export, import, clone, diff, deploy.

Tests the full org configuration management flow against Atlas dev cluster.
"""

import pytest
from bson import ObjectId

from kernel.api.org_lifecycle import (
    clone_org,
    deploy_org,
    diff_org_configs,
    export_org_config,
    import_org_config,
)
from kernel.entity.definition import EntityDefinition, FieldDefinition
from kernel.rule.lookup import Lookup
from kernel.rule.schema import Rule
from kernel.skill.integrity import compute_content_hash
from kernel.skill.schema import Skill
from kernel_entities import Organization


@pytest.mark.asyncio
async def test_export_empty_org(db, org_id, actor):
    """Exporting an org with no config returns empty categories."""
    config = await export_org_config(org_id)
    assert "entities" in config
    assert "rules" in config
    assert "skills" in config
    assert "lookups" in config
    assert "roles" in config


@pytest.mark.asyncio
async def test_export_with_content(db, org_id, actor):
    """Export includes entity defs, rules, skills, lookups, roles."""
    # Create entity definition
    defn = EntityDefinition(
        org_id=org_id,
        name="ExportEmail",
        collection_name="export_emails",
        fields={
            "subject": FieldDefinition(type="str"),
            "status": FieldDefinition(
                type="str",
                enum_values=["received", "processed"],
                is_state_field=True,
                default="received",
            ),
        },
        state_machine={"received": ["processed"]},
    )
    await defn.insert()

    # Create rule
    rule = Rule(
        org_id=org_id,
        entity_type="ExportEmail",
        capability="auto_classify",
        name="export-test-rule",
        conditions={"field": "subject", "op": "contains", "value": "test"},
        action="set_fields",
        sets={"status": "processed"},
        status="active",
        created_by=str(actor.id),
    )
    await rule.insert()

    # Create skill
    skill = Skill(
        org_id=org_id,
        name="export-test-skill",
        type="associate",
        content="# Test Skill\nDo something.",
        content_hash=compute_content_hash("# Test Skill\nDo something."),
        created_by=str(actor.id),
    )
    await skill.insert()

    # Create lookup
    lookup = Lookup(
        org_id=org_id,
        name="export-test-lookup",
        data={"key1": "val1", "key2": "val2"},
        created_by=str(actor.id),
    )
    await lookup.insert()

    config = await export_org_config(org_id)

    assert "ExportEmail" in config["entities"]
    assert "export-test-rule" in config["rules"]
    assert "export-test-skill" in config["skills"]
    assert "export-test-lookup" in config["lookups"]

    # Verify entity def content
    entity_config = config["entities"]["ExportEmail"]
    assert "subject" in entity_config["fields"]
    assert entity_config["state_machine"] == {"received": ["processed"]}


@pytest.mark.asyncio
async def test_import_creates_org(db, org_id, actor):
    """Import creates a new org with all config items."""
    config = {
        "entities": {
            "ImportEmail": {
                "name": "ImportEmail",
                "collection_name": "import_emails",
                "fields": {
                    "subject": {"type": "str"},
                },
            }
        },
        "rules": {
            "import-rule": {
                "entity_type": "ImportEmail",
                "capability": "auto_classify",
                "name": "import-rule",
                "conditions": {"field": "subject", "op": "contains", "value": "x"},
                "action": "set_fields",
                "sets": {"subject": "classified"},
                "status": "active",
            }
        },
        "lookups": {
            "import-lookup": {
                "name": "import-lookup",
                "data": {"a": "b"},
            }
        },
        "skills": {
            "import-skill": {
                "name": "import-skill",
                "type": "associate",
                "content": "# Imported",
                "content_hash": compute_content_hash("# Imported"),
                "status": "active",
            }
        },
        "roles": {},
        "actors": {},
        "integrations": {},
    }

    result = await import_org_config("Import Test Org", config)
    assert result["items_imported"] == 4  # 1 entity + 1 rule + 1 lookup + 1 skill
    assert result["org_slug"] == "import-test-org"

    # Verify items were created in the new org
    new_org_id = ObjectId(result["org_id"])
    defs = await EntityDefinition.find({"org_id": new_org_id}).to_list()
    assert len(defs) == 1
    assert defs[0].name == "ImportEmail"


@pytest.mark.asyncio
async def test_clone_org(db, org_id, actor):
    """Clone copies config to a new org."""
    # Set up source config
    defn = EntityDefinition(
        org_id=org_id,
        name="CloneEntity",
        collection_name="clone_entities",
        fields={"name": FieldDefinition(type="str", required=True)},
    )
    await defn.insert()

    result = await clone_org(org_id, "Cloned Org")
    assert result["items_copied"] >= 1

    # Verify the cloned org has the entity
    new_org_id = ObjectId(result["org_id"])
    defs = await EntityDefinition.find({"org_id": new_org_id}).to_list()
    entity_names = {d.name for d in defs}
    assert "CloneEntity" in entity_names


@pytest.mark.asyncio
async def test_diff_identical_orgs(db, org_id, actor):
    """Diff of org against itself shows no differences."""
    result = await diff_org_configs(org_id, org_id)
    assert result["differences"] == []


@pytest.mark.asyncio
async def test_diff_detects_changes(db, org_id, actor):
    """Diff detects items in one org but not the other."""
    # Create a second org
    org2_id = ObjectId()
    org2 = Organization(
        id=org2_id, org_id=org2_id, name="Diff Org 2",
        slug="diff-org-2", status="active",
    )
    await org2.insert()

    # Add entity only in org1
    defn = EntityDefinition(
        org_id=org_id,
        name="DiffOnlyInA",
        collection_name="diff_only_a",
        fields={"name": FieldDefinition(type="str")},
    )
    await defn.insert()

    # Add entity only in org2
    defn2 = EntityDefinition(
        org_id=org2_id,
        name="DiffOnlyInB",
        collection_name="diff_only_b",
        fields={"name": FieldDefinition(type="str")},
    )
    await defn2.insert()

    result = await diff_org_configs(org_id, org2_id)
    changes = result["differences"]

    entity_diffs = [d for d in changes if d["type"] == "entities"]
    names = {d["name"] for d in entity_diffs}
    assert "DiffOnlyInA" in names
    assert "DiffOnlyInB" in names


@pytest.mark.asyncio
async def test_deploy_dry_run(db, org_id, actor):
    """Deploy dry run shows changes without applying."""
    # Create source with one entity
    defn = EntityDefinition(
        org_id=org_id,
        name="DeployEntity",
        collection_name="deploy_entities",
        fields={"name": FieldDefinition(type="str")},
    )
    await defn.insert()

    # Create target org
    target_id = ObjectId()
    target = Organization(
        id=target_id, org_id=target_id, name="Deploy Target",
        slug="deploy-target", status="active",
    )
    await target.insert()

    result = await deploy_org(org_id, target_id, dry_run=True)
    assert result["dry_run"] is True
    assert len(result["changes"]) >= 1


@pytest.mark.asyncio
async def test_deploy_apply(db, org_id, actor):
    """Deploy apply actually creates items in target org."""
    # Create source config
    defn = EntityDefinition(
        org_id=org_id,
        name="DeployApplyEntity",
        collection_name="deploy_apply_entities",
        fields={"name": FieldDefinition(type="str")},
    )
    await defn.insert()

    # Create target org
    target_id = ObjectId()
    target = Organization(
        id=target_id, org_id=target_id, name="Deploy Apply Target",
        slug="deploy-apply-target", status="active",
    )
    await target.insert()

    result = await deploy_org(org_id, target_id, dry_run=False)
    assert result["dry_run"] is False
    assert len(result["applied"]) >= 1

    # Verify entity was deployed
    defs = await EntityDefinition.find({"org_id": target_id}).to_list()
    entity_names = {d.name for d in defs}
    assert "DeployApplyEntity" in entity_names
