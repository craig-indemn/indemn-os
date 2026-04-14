"""Integration tests: Organization scoping — cross-tenant isolation.

Acceptance test #3: Two orgs, entities in each, queries return only same-org data.
"""

import pytest
from bson import ObjectId

from kernel.context import current_org_id
from kernel_entities import Actor, Organization, Role


@pytest.mark.asyncio
async def test_find_scoped_returns_only_same_org(db, org_id):
    """Entities in org A are invisible to queries scoped to org B."""
    # Create org B
    org_b_id = ObjectId()
    org_b = Organization(
        id=org_b_id, org_id=org_b_id, name="Org B", slug="org-b", status="active",
    )
    await org_b.insert()

    # Create actors in each org
    actor_a = Actor(
        org_id=org_id, name="Actor A", type="human", status="active",
    )
    await actor_a.insert()

    actor_b = Actor(
        org_id=org_b_id, name="Actor B", type="human", status="active",
    )
    await actor_b.insert()

    # Scoped to org A — should only see Actor A
    current_org_id.set(org_id)
    actors_a = await Actor.find_scoped({}).to_list()
    actor_names_a = {a.name for a in actors_a}
    assert "Actor A" in actor_names_a
    assert "Actor B" not in actor_names_a

    # Scoped to org B — should only see Actor B
    current_org_id.set(org_b_id)
    actors_b = await Actor.find_scoped({}).to_list()
    actor_names_b = {a.name for a in actors_b}
    assert "Actor B" in actor_names_b
    assert "Actor A" not in actor_names_b


@pytest.mark.asyncio
async def test_get_scoped_denies_cross_org_access(db, org_id):
    """get_scoped raises PermissionError when accessing entity from wrong org."""
    # Create org B and an actor in it
    org_b_id = ObjectId()
    org_b = Organization(
        id=org_b_id, org_id=org_b_id, name="Org B2", slug="org-b2", status="active",
    )
    await org_b.insert()

    actor_in_b = Actor(
        org_id=org_b_id, name="Secret Actor", type="human", status="active",
    )
    await actor_in_b.insert()

    # Try to access org B's actor while scoped to org A
    current_org_id.set(org_id)
    with pytest.raises(PermissionError, match="Cross-org access denied"):
        await Actor.get_scoped(actor_in_b.id)
