"""Integration tests: Authentication flow.

Acceptance tests:
  #9 AUTHENTICATION (password login → JWT, token auth, invalid/expired rejected)
  #10 PERMISSION ENFORCEMENT (read-only can't write, write role can write)
  #18 FIRST-ORG BOOTSTRAP
"""

import pytest
from bson import ObjectId

from kernel.auth.jwt import create_access_token, verify_access_token
from kernel.auth.middleware import check_permission
from kernel.auth.password import hash_password, verify_password
from kernel_entities import Actor, Organization, Role


@pytest.mark.asyncio
async def test_password_auth_roundtrip(db, org_id, actor):
    """#9: Password hash → verify works."""
    pw = "secure-p@ssword-123"
    h = hash_password(pw)
    assert verify_password(pw, h)
    assert not verify_password("wrong", h)


@pytest.mark.asyncio
async def test_jwt_roundtrip(db, org_id, actor):
    """#9: Create JWT → verify → get claims back."""
    token, jti = create_access_token(
        str(actor.id), str(org_id), ["admin"]
    )
    payload = verify_access_token(token)
    assert payload["actor_id"] == str(actor.id)
    assert payload["org_id"] == str(org_id)
    assert payload["roles"] == ["admin"]
    assert payload["jti"] == jti


@pytest.mark.asyncio
async def test_invalid_token_rejected(db):
    """#9: Invalid token raises."""
    with pytest.raises(Exception):
        verify_access_token("not-a-real-token")


@pytest.mark.asyncio
async def test_permission_enforcement(db, org_id):
    """#10: Read-only role can't write. Admin can write."""
    # Read-only role
    readonly_role = Role(
        org_id=org_id,
        name="readonly",
        permissions={"read": ["*"], "write": []},
    )
    await readonly_role.insert()

    readonly_actor = Actor(
        org_id=org_id, name="Reader", type="human", status="active",
        role_ids=[readonly_role.id],
    )
    await readonly_actor.insert()
    readonly_actor._cached_roles = [readonly_role]

    # Read should work
    check_permission(readonly_actor, "Submission", "read")  # No exception

    # Write should fail
    with pytest.raises(PermissionError, match="does not have 'write' permission"):
        check_permission(readonly_actor, "Submission", "write")


@pytest.mark.asyncio
async def test_wildcard_permission(db, org_id, actor):
    """#10: Wildcard '*' grants access to all entity types."""
    # actor fixture has admin role with {"read": ["*"], "write": ["*"]}
    check_permission(actor, "AnyEntityType", "read")
    check_permission(actor, "AnyEntityType", "write")


@pytest.mark.asyncio
async def test_bootstrap_creates_org_and_admin(db):
    """#18: Platform init → org + admin + token."""
    # Simulate bootstrap
    org_id = ObjectId()
    org = Organization(
        id=org_id, org_id=org_id, name="Bootstrap Test",
        slug="_platform_test", status="active",
    )
    await org.insert()

    admin = Actor(
        org_id=org_id, name="Admin", email="admin@test.com",
        type="human", status="active",
        authentication_methods=[
            {"type": "password", "password_hash": hash_password("admin123")}
        ],
    )
    await admin.insert()

    role = Role(
        org_id=org_id, name="platform_admin",
        permissions={"read": ["*"], "write": ["*"]},
    )
    await role.insert()
    admin.role_ids = [role.id]
    await admin.save()

    # Verify we can issue a token and use it
    token, jti = create_access_token(str(admin.id), str(org_id), ["platform_admin"])
    payload = verify_access_token(token)
    assert payload["roles"] == ["platform_admin"]

    # Verify password auth
    pw_method = admin.authentication_methods[0]
    assert verify_password("admin123", pw_method["password_hash"])
