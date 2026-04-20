"""Integration tests: Rule CRUD and evaluation.

Tests rule creation, listing, evaluation via the rules engine,
lookup resolution, and the full auto_classify flow.
"""

import pytest

from kernel.rule.lookup import Lookup
from kernel.rule.schema import Rule


@pytest.mark.asyncio
async def test_rule_create_and_list(db, org_id, actor):
    """Create rules and list them by entity type."""
    rule1 = Rule(
        org_id=org_id,
        entity_type="Email",
        capability="auto_classify",
        name="usli-from-domain",
        conditions={"field": "from_address", "op": "ends_with", "value": "@usli.com"},
        action="set_fields",
        sets={"classification": "usli_quote"},
        priority=100,
        status="active",
        created_by=str(actor.id),
    )
    rule2 = Rule(
        org_id=org_id,
        entity_type="Email",
        capability="auto_classify",
        name="hiscox-from-domain",
        conditions={"field": "from_address", "op": "ends_with", "value": "@hiscox.com"},
        action="set_fields",
        sets={"classification": "hiscox_quote"},
        priority=100,
        status="active",
        created_by=str(actor.id),
    )
    await rule1.insert()
    await rule2.insert()

    # List by entity type
    rules = await Rule.find(
        {"org_id": org_id, "entity_type": "Email", "status": "active"}
    ).to_list()
    assert len(rules) >= 2
    names = {r.name for r in rules}
    assert "usli-from-domain" in names
    assert "hiscox-from-domain" in names


@pytest.mark.asyncio
async def test_rule_evaluation_match(db, org_id, actor):
    """Rule engine matches and returns correct sets."""
    from kernel.rule.engine import evaluate_rules

    rule = Rule(
        org_id=org_id,
        entity_type="Email",
        capability="auto_classify",
        name="test-match",
        conditions={"field": "from_address", "op": "ends_with", "value": "@test.com"},
        action="set_fields",
        sets={"classification": "test_type"},
        priority=100,
        status="active",
        created_by=str(actor.id),
    )
    await rule.insert()

    result = await evaluate_rules(
        org_id=org_id,
        entity_type="Email",
        capability="auto_classify",
        entity_data={"from_address": "user@test.com", "subject": "Hello"},
    )

    assert result["matched"] is True
    assert result["vetoed"] is False
    assert result["winning_rule"]["sets"]["classification"] == "test_type"


@pytest.mark.asyncio
async def test_rule_veto(db, org_id, actor):
    """Veto rule overrides positive matches."""
    from kernel.rule.engine import evaluate_rules

    # Positive rule
    pos = Rule(
        org_id=org_id,
        entity_type="Email",
        capability="auto_classify",
        name="positive",
        conditions={"field": "from_address", "op": "ends_with", "value": "@usli.com"},
        action="set_fields",
        sets={"classification": "usli_quote"},
        priority=100,
        status="active",
        created_by=str(actor.id),
    )
    # Veto rule (higher priority check: both conditions)
    veto = Rule(
        org_id=org_id,
        entity_type="Email",
        capability="auto_classify",
        name="veto",
        conditions={
            "all": [
                {"field": "from_address", "op": "ends_with", "value": "@usli.com"},
                {"field": "subject", "op": "contains", "value": "Decline"},
            ]
        },
        action="force_reasoning",
        forces_reasoning_reason="USLI decline needs human review",
        priority=200,
        status="active",
        created_by=str(actor.id),
    )
    await pos.insert()
    await veto.insert()

    result = await evaluate_rules(
        org_id=org_id,
        entity_type="Email",
        capability="auto_classify",
        entity_data={
            "from_address": "quotes@usli.com",
            "subject": "Decline - Policy XYZ",
        },
    )
    assert result["vetoed"] is True
    assert "USLI decline" in result["veto_reason"]


@pytest.mark.asyncio
async def test_lookup_resolution(db, org_id, actor):
    """Lookup references in rule sets resolve correctly."""
    from kernel.rule.lookup import resolve_lookup_references

    lookup = Lookup(
        org_id=org_id,
        name="usli-prefix-lob",
        data={"MGL": "general_liability", "XPL": "excess_personal_liability"},
        created_by=str(actor.id),
    )
    await lookup.insert()

    sets = {
        "lob": {"lookup": "usli-prefix-lob", "from_field": "quote_prefix"},
    }
    entity_data = {"quote_prefix": "MGL"}

    resolved = await resolve_lookup_references(sets, org_id, entity_data)
    assert resolved["lob"] == "general_liability"


@pytest.mark.asyncio
async def test_rule_archive(db, org_id, actor):
    """Archived rules are excluded from evaluation."""
    from kernel.rule.engine import evaluate_rules

    rule = Rule(
        org_id=org_id,
        entity_type="Email",
        capability="auto_classify",
        name="archived-rule",
        conditions={"field": "subject", "op": "equals", "value": "test"},
        action="set_fields",
        sets={"classification": "archived"},
        status="archived",
        created_by=str(actor.id),
    )
    await rule.insert()

    result = await evaluate_rules(
        org_id=org_id,
        entity_type="Email",
        capability="auto_classify",
        entity_data={"subject": "test"},
    )
    # Archived rules should not match
    assert result["matched"] is False or (
        result.get("winning_rule", {}).get("name") != "archived-rule"
    )
