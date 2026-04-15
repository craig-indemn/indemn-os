"""Authentication API routes — Phase 4.

SSO login, MFA challenge/verify, platform admin sessions,
password reset, claims refresh, Tier 3 self-service signup.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from kernel.auth.middleware import get_current_actor
from kernel.config import settings

auth_router = APIRouter(tags=["auth"])


# --- Request/Response models ---


class LoginRequest(BaseModel):
    email: str
    password: str
    org_slug: str


class MfaVerifyRequest(BaseModel):
    partial_token: str
    totp_code: str


class MfaBackupRequest(BaseModel):
    partial_token: str
    backup_code: str


class PasswordResetInitiateRequest(BaseModel):
    email: str
    org_slug: str


class PasswordResetCompleteRequest(BaseModel):
    token: str
    new_password: str


class PlatformAdminSessionRequest(BaseModel):
    target_org_id: str
    work_type: str = "build"
    duration_hours: int = 4
    reason: str = ""


class SignupRequest(BaseModel):
    email: str
    password: str
    org_name: str


# --- SSO Discovery [G-35] ---


@auth_router.get("/auth/providers")
async def list_auth_providers(org_slug: str):
    """Pre-auth endpoint — no token required.
    Returns available login methods for an org."""
    from kernel_entities.integration import Integration
    from kernel_entities.organization import Organization

    org = await Organization.find_one({"slug": org_slug})
    if not org:
        raise HTTPException(404, "Organization not found")

    providers = [{"type": "password", "name": "Password"}]

    sso_integrations = await Integration.find({
        "org_id": org.id,
        "system_type": "identity_provider",
        "status": "active",
    }).to_list()

    for integration in sso_integrations:
        providers.append({
            "type": "sso",
            "name": integration.name,
            "integration_id": str(integration.id),
            "provider": integration.provider,
        })

    return {"org_id": str(org.id), "providers": providers}


# --- Password Login ---


@auth_router.post("/auth/login")
async def login(data: LoginRequest, request: Request):
    """Password login with rate limiting."""
    from kernel.auth.audit import write_auth_event
    from kernel.auth.password import verify_password
    from kernel.auth.rate_limit import check_rate_limit, record_failed_attempt
    from kernel.auth.session_manager import create_session
    from kernel_entities.actor import Actor
    from kernel_entities.organization import Organization

    org = await Organization.find_one({"slug": data.org_slug})
    if not org:
        raise HTTPException(401, "Invalid credentials")

    ip_address = request.client.host if request.client else "unknown"

    # Rate limit check [G-40]
    if await check_rate_limit(ip_address, data.email, org.id):
        raise HTTPException(429, "Too many login attempts. Try again later.")

    actor = await Actor.find_one({
        "email": data.email,
        "org_id": org.id,
        "status": "active",
    })

    if not actor:
        await record_failed_attempt(ip_address, data.email)
        raise HTTPException(401, "Invalid credentials")

    # Verify password
    password_method = next(
        (m for m in actor.authentication_methods if m.get("type") == "password"),
        None,
    )
    if not password_method or not verify_password(
        data.password, password_method.get("password_hash", "")
    ):
        await record_failed_attempt(ip_address, data.email)
        await write_auth_event(actor, "auth.login_failure", {"ip": ip_address})
        raise HTTPException(401, "Invalid credentials")

    # Check MFA requirement
    mfa_required = _actor_has_mfa(actor)

    session, token = await create_session(
        actor,
        auth_method="password",
        ip_address=ip_address,
        user_agent=request.headers.get("user-agent"),
    )

    if mfa_required and not actor.mfa_exempt:
        from kernel.auth.jwt import create_partial_token

        partial_token = create_partial_token(actor, session)
        await write_auth_event(actor, "auth.mfa_challenged", {"method": "totp"})
        return {
            "requires_mfa": True,
            "mfa_type": "totp",
            "partial_token": partial_token,
        }

    await write_auth_event(
        actor, "auth.login_success", {"method": "password", "ip": ip_address}
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": session.expires_at.isoformat(),
    }


# --- SSO Login Flow [G-35] ---


@auth_router.get("/auth/sso/{integration_id}")
async def sso_initiate(integration_id: str):
    """Redirect to SSO provider."""
    from kernel.integration.dispatch import get_adapter_for_integration
    from kernel_entities.integration import Integration

    integration = await Integration.get(integration_id)
    if not integration or integration.system_type != "identity_provider":
        raise HTTPException(404, "SSO provider not found")

    adapter = await get_adapter_for_integration(integration)
    redirect_url = await adapter.auth_initiate(
        redirect_uri=f"{settings.api_url}/auth/sso/{integration_id}/callback"
    )
    return RedirectResponse(redirect_url)


@auth_router.get("/auth/sso/{integration_id}/callback")
async def sso_callback(integration_id: str, code: str, state: str = None):
    """SSO callback — validate token, find actor, create session."""
    from kernel.auth.audit import write_auth_event
    from kernel.auth.jwt import create_partial_token
    from kernel.auth.session_manager import create_session
    from kernel.integration.dispatch import get_adapter_for_integration
    from kernel_entities.actor import Actor
    from kernel_entities.integration import Integration

    integration = await Integration.get(integration_id)
    if not integration:
        raise HTTPException(404)

    adapter = await get_adapter_for_integration(integration)
    user_info = await adapter.auth_callback(code, state)

    actor = await Actor.find_one({
        "email": user_info["email"],
        "org_id": integration.org_id,
        "status": "active",
    })
    if not actor:
        raise HTTPException(403, "No active actor found for this email")

    mfa_required = _actor_has_mfa(actor)

    session, token = await create_session(
        actor, auth_method=f"sso:{integration.provider}"
    )

    if mfa_required and not actor.mfa_exempt:
        partial_token = create_partial_token(actor, session)
        return {"requires_mfa": True, "mfa_type": "totp", "partial_token": partial_token}

    await write_auth_event(
        actor, "auth.login_success", {"method": f"sso:{integration.provider}"}
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": session.expires_at.isoformat(),
    }


# --- MFA Challenge [G-36] ---


@auth_router.post("/auth/mfa/verify")
async def verify_mfa(data: MfaVerifyRequest):
    """Verify TOTP code and upgrade partial token to full session."""
    import pyotp

    from kernel.auth.audit import write_auth_event
    from kernel.auth.jwt import create_access_token, verify_partial_token
    from kernel.integration.credentials import fetch_credentials
    from kernel_entities.actor import Actor
    from kernel_entities.session import Session

    payload = verify_partial_token(data.partial_token)
    actor = await Actor.get(payload["actor_id"])
    session = await Session.get(payload["session_id"])
    if not actor or not session:
        raise HTTPException(401, "Invalid MFA session")

    totp_method = next(
        (m for m in actor.authentication_methods if m.get("type") == "totp"),
        None,
    )
    if not totp_method:
        raise HTTPException(400, "No TOTP method configured")

    totp_secret = await fetch_credentials(totp_method["secret_ref"])
    totp = pyotp.TOTP(totp_secret["secret"])
    if not totp.verify(data.totp_code, valid_window=1):
        await write_auth_event(actor, "auth.mfa_challenged", {"success": False})
        raise HTTPException(401, "Invalid TOTP code")

    # MFA verified — update session
    session.mfa_verified = True
    session.mfa_verified_at = datetime.now(timezone.utc)
    await session.save()

    await write_auth_event(actor, "auth.mfa_verified", {"method": "totp"})

    # Issue full tokens
    from kernel_entities.role import Role

    roles = await Role.find({"_id": {"$in": actor.role_ids}}).to_list()
    role_names = [r.name for r in roles]
    token, jti = create_access_token(str(actor.id), str(actor.org_id), role_names)
    session.access_token_jti = jti
    await session.save()

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": session.expires_at.isoformat(),
    }


# --- MFA Backup Codes [G-38] ---


@auth_router.post("/auth/mfa/backup")
async def use_backup_code(data: MfaBackupRequest):
    """Use a backup code instead of TOTP. Single-use. Forces re-enrollment."""
    from kernel.auth.audit import write_auth_event
    from kernel.auth.jwt import create_access_token, verify_partial_token
    from kernel.auth.password import verify_password
    from kernel.integration.credentials import fetch_credentials, store_credentials
    from kernel_entities.actor import Actor
    from kernel_entities.session import Session

    payload = verify_partial_token(data.partial_token)
    actor = await Actor.get(payload["actor_id"])
    session = await Session.get(payload["session_id"])
    if not actor or not session:
        raise HTTPException(401, "Invalid MFA session")

    totp_method = next(
        (m for m in actor.authentication_methods if m.get("type") == "totp"),
        None,
    )
    if not totp_method:
        raise HTTPException(400, "No TOTP method configured")

    secrets = await fetch_credentials(totp_method["secret_ref"])
    backup_codes = secrets.get("backup_codes", [])

    # Verify and consume backup code (codes stored as hashes)
    matched = False
    for idx, stored_hash in enumerate(backup_codes):
        if verify_password(data.backup_code, stored_hash):
            matched = True
            backup_codes.pop(idx)
            break

    if not matched:
        raise HTTPException(401, "Invalid backup code")

    # Update stored backup codes
    secrets["backup_codes"] = backup_codes
    await store_credentials(totp_method["secret_ref"], secrets)

    # MFA verified
    session.mfa_verified = True
    session.mfa_verified_at = datetime.now(timezone.utc)
    await session.save()

    await write_auth_event(actor, "auth.mfa_reset", {"method": "backup_code"})

    from kernel_entities.role import Role

    roles = await Role.find({"_id": {"$in": actor.role_ids}}).to_list()
    role_names = [r.name for r in roles]
    token, jti = create_access_token(str(actor.id), str(actor.org_id), role_names)
    session.access_token_jti = jti
    await session.save()

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": session.expires_at.isoformat(),
        "mfa_re_enrollment_required": True,
    }


# --- Platform Admin Cross-Org Sessions [G-37] ---


@auth_router.post("/api/platform/sessions")
async def create_platform_admin_session(
    data: PlatformAdminSessionRequest,
    actor=Depends(get_current_actor),
):
    """Create a platform admin session in a target org.
    The actor must be in the _platform org with platform_admin role."""
    from kernel.auth.audit import write_auth_event_in_org
    from kernel.auth.jwt import create_access_token
    from kernel_entities.organization import Organization
    from kernel_entities.session import Session

    if not await _is_platform_admin(actor):
        raise HTTPException(403, "Not a platform admin")

    if data.duration_hours > 24:
        raise HTTPException(400, "Maximum session duration is 24 hours")

    target_org = await Organization.get(data.target_org_id)
    if not target_org:
        raise HTTPException(404, "Target org not found")

    jti = str(uuid4())
    session = Session(
        org_id=ObjectId(data.target_org_id),
        actor_id=actor.id,
        type="user_interactive",
        auth_method_used="platform_admin",
        status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=data.duration_hours),
        platform_admin_context={
            "source_org_id": str(actor.org_id),
            "target_org_id": data.target_org_id,
            "work_type": data.work_type,
            "reason": data.reason,
            "acting_actor_name": actor.name,
            "acting_actor_email": actor.email,
        },
        access_token_jti=jti,
    )
    await session.insert()

    await write_auth_event_in_org(
        ObjectId(data.target_org_id),
        actor,
        "auth.platform_admin_access",
        {
            "work_type": data.work_type,
            "reason": data.reason,
            "duration_hours": data.duration_hours,
            "source_org": str(actor.org_id),
        },
    )

    token, new_jti = create_access_token(
        str(actor.id), data.target_org_id, ["platform_admin"]
    )
    session.access_token_jti = new_jti
    await session.save()

    return {"access_token": token, "expires_at": session.expires_at.isoformat()}


# --- Password Reset [G-38] ---


@auth_router.post("/auth/reset-password/initiate")
async def initiate_password_reset(data: PasswordResetInitiateRequest):
    """Send password reset magic link via email Integration."""
    from kernel.auth.jwt import generate_magic_link_token
    from kernel_entities.actor import Actor
    from kernel_entities.organization import Organization

    org = await Organization.find_one({"slug": data.org_slug})
    if not org:
        return {"status": "ok"}  # Don't reveal whether org exists

    actor = await Actor.find_one({"email": data.email, "org_id": org.id})
    if not actor:
        return {"status": "ok"}  # Don't reveal whether email exists

    generate_magic_link_token(actor, purpose="password_reset", expires_hours=4)
    # In production, send the token via the org's email Integration.
    # For MVP, the token is generated but email delivery depends on
    # having an email Integration configured.
    return {"status": "ok"}


@auth_router.post("/auth/reset-password/complete")
async def complete_password_reset(data: PasswordResetCompleteRequest):
    """Complete password reset. Revokes all existing sessions."""
    from kernel.auth.audit import write_auth_event
    from kernel.auth.jwt import verify_magic_link_token
    from kernel.auth.password import hash_password
    from kernel.auth.session_manager import revoke_all_sessions
    from kernel_entities.actor import Actor

    payload = verify_magic_link_token(data.token, purpose="password_reset")
    actor = await Actor.get(payload["actor_id"])
    if not actor:
        raise HTTPException(404, "Actor not found")

    # Update password
    password_method = next(
        (m for m in actor.authentication_methods if m.get("type") == "password"),
        None,
    )
    if password_method:
        password_method["password_hash"] = hash_password(data.new_password)
    else:
        actor.authentication_methods.append({
            "type": "password",
            "password_hash": hash_password(data.new_password),
        })
    await actor.save()

    # Revoke all sessions [G-38]
    await revoke_all_sessions(actor.id)

    await write_auth_event(actor, "auth.password_changed", {"method": "reset"})
    return {"status": "password_reset"}


# --- Claims Refresh [G-39] ---


@auth_router.post("/auth/refresh-claims")
async def refresh_claims(actor=Depends(get_current_actor), request: Request = None):
    """Force a claims refresh — re-issue token with current roles."""
    from kernel.auth.jwt import create_access_token
    from kernel_entities.role import Role
    from kernel_entities.session import Session

    session = await Session.find_one({
        "actor_id": actor.id,
        "status": "active",
    })
    if not session:
        raise HTTPException(401, "No active session")

    roles = await Role.find({"_id": {"$in": actor.role_ids}}).to_list()
    role_names = [r.name for r in roles]

    token, jti = create_access_token(str(actor.id), str(actor.org_id), role_names)
    session.claims_stale = False
    session.access_token_jti = jti
    await session.save()

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": session.expires_at.isoformat(),
    }


# --- Tier 3 Self-Service Signup [G-58] ---


@auth_router.post("/auth/signup")
async def tier3_signup(data: SignupRequest):
    """Tier 3 developer self-service signup.
    Creates org + admin actor + password + first API key."""
    from kernel.auth.password import hash_password
    from kernel.auth.token import generate_service_token, hash_token
    from kernel_entities.actor import Actor
    from kernel_entities.organization import Organization
    from kernel_entities.role import Role

    org_id = ObjectId()
    org = Organization(
        id=org_id,
        org_id=org_id,
        name=data.org_name,
        slug=_slugify(data.org_name),
        status="onboarding",
    )
    await org.insert()

    admin = Actor(
        org_id=org_id,
        name=data.email.split("@")[0],
        email=data.email,
        type="tier3_developer",
        status="active",
        authentication_methods=[{
            "type": "password",
            "password_hash": hash_password(data.password),
        }],
    )
    await admin.insert()

    admin_role = Role(
        org_id=org_id,
        name="admin",
        permissions={"read": ["*"], "write": ["*"]},
    )
    await admin_role.insert()
    admin.role_ids = [admin_role.id]

    # Generate API key
    api_key = generate_service_token()
    admin.authentication_methods.append({
        "type": "token",
        "usage": "tier3_api_key",
        "token_hash": hash_token(api_key),
    })
    await admin.save()

    return {
        "org_id": str(org_id),
        "actor_id": str(admin.id),
        "api_key": api_key,
        "status": "created",
        "note": "Verify your email to activate the org",
    }


# --- Auth Events View [G-41] ---


@auth_router.get("/api/auth-events")
async def list_auth_events(
    limit: int = 50,
    offset: int = 0,
    event_type: str = None,
    actor=Depends(get_current_actor),
):
    """List auth audit events for the current org."""
    from kernel.changes.collection import ChangeRecord

    query = {
        "org_id": actor.org_id,
        "change_type": {"$regex": "^auth\\."},
    }
    if event_type:
        query["change_type"] = event_type

    records = (
        await ChangeRecord.find(query)
        .sort("-timestamp")
        .skip(offset)
        .limit(limit)
        .to_list()
    )

    return [
        {
            "id": str(r.id),
            "event_type": r.change_type,
            "actor_id": r.actor_id,
            "entity_id": str(r.entity_id),
            "timestamp": r.timestamp.isoformat(),
            "metadata": r.method_metadata,
        }
        for r in records
    ]


# --- Helpers ---


def _actor_has_mfa(actor) -> bool:
    """Check if an actor has MFA configured."""
    return any(m.get("type") == "totp" for m in actor.authentication_methods)


async def _is_platform_admin(actor) -> bool:
    """Check if actor has platform_admin role in the _platform org."""
    from kernel_entities.organization import Organization

    platform_org = await Organization.find_one({"slug": "_platform"})
    if not platform_org or actor.org_id != platform_org.id:
        return False

    roles = getattr(actor, "_cached_roles", [])
    return any(r.name == "platform_admin" for r in roles)


def _slugify(name: str) -> str:
    """Convert a name to a URL-safe slug."""
    import re

    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")
