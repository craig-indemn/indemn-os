"""Platform bootstrap — first-organization initialization.

The very first organization has no email integration yet, so the bootstrap
flow prints a one-time token. After configuring email delivery, subsequent
invitations use magic links.
"""

from bson import ObjectId
from fastapi import APIRouter, HTTPException

from kernel.auth.jwt import create_access_token
from kernel.auth.password import hash_password
from kernel_entities.actor import Actor
from kernel_entities.organization import Organization
from kernel_entities.role import Role

bootstrap_router = APIRouter(prefix="/api/_platform", tags=["platform"])


@bootstrap_router.post("/init")
async def platform_init(data: dict):
    """Bootstrap the first organization. One-time operation."""
    admin_email = data.get("admin_email")
    admin_password = data.get("admin_password")

    if not admin_email or not admin_password:
        raise HTTPException(400, "admin_email and admin_password required")

    existing = await Organization.find_one({"slug": "_platform"})
    if existing:
        raise HTTPException(400, "Platform already initialized")

    # Create platform org (self-referencing: org_id = id).
    # Uses Beanie insert because the self-reference requires pre-setting id,
    # which save_tracked interprets as an update on a non-existent document.
    org_id = ObjectId()
    platform_org = Organization(
        id=org_id,
        org_id=org_id,
        name="Indemn Platform",
        slug="_platform",
        status="active",
    )
    await platform_org.insert()

    # Create admin actor — uses save_tracked for audit trail
    admin = Actor(
        org_id=org_id,
        name="Platform Admin",
        email=admin_email,
        type="human",
        status="active",
        authentication_methods=[
            {
                "type": "password",
                "password_hash": hash_password(admin_password),
            }
        ],
    )
    await admin.save_tracked(actor_id="__bootstrap__")

    # Create admin role — uses save_tracked for audit trail
    admin_role = Role(
        org_id=org_id,
        name="platform_admin",
        permissions={"read": ["*"], "write": ["*"]},
    )
    await admin_role.save_tracked(actor_id="__bootstrap__")
    admin.role_ids = [admin_role.id]
    await admin.save_tracked(actor_id="__bootstrap__")

    # Issue token
    token, jti = create_access_token(str(admin.id), str(org_id), ["platform_admin"])

    return {
        "status": "initialized",
        "org_id": str(org_id),
        "admin_id": str(admin.id),
        "access_token": token,
    }
