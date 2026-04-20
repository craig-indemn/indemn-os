"""FastAPI auth middleware.

Sets auth context (org_id, actor_id) on each request via contextvars.
Loads roles once per request for permission checks.
Skips auth for health, bootstrap, and webhook endpoints.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from kernel.auth.jwt import verify_access_token
from kernel.context import (
    current_actor_id,
    current_causation_message_id,
    current_correlation_id,
    current_depth,
    current_org_id,
)
from kernel.observability.tracing import create_span
from kernel_entities.actor import Actor
from kernel_entities.role import Role


class AuthMiddleware(BaseHTTPMiddleware):
    """Set auth context on each request."""

    # Paths that don't require authentication
    PUBLIC_PATHS = {"/health", "/api/_platform/init", "/docs", "/openapi.json"}
    # Path prefixes that don't require authentication (pre-auth flows)
    PUBLIC_PREFIXES = (
        "/webhook/",
        "/auth/providers",
        "/auth/login",
        "/auth/signup",
        "/auth/sso/",
        "/auth/mfa/",
        "/auth/reset-password/",
        "/auth/refresh",
        "/auth/setup-password",
    )

    async def dispatch(self, request: Request, call_next):
        # Skip auth for public paths, webhooks, and pre-auth flows
        path = request.url.path
        if path in self.PUBLIC_PATHS or path.startswith(self.PUBLIC_PREFIXES):
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"error": "Missing auth token"})

        token = auth.split(" ", 1)[1]

        # Service tokens (opaque, long-lived) vs JWTs (short-lived, stateless)
        if token.startswith("indemn_"):
            from kernel.auth.token import authenticate_by_token

            actor = await authenticate_by_token(token)
            if not actor:
                return JSONResponse(status_code=401, content={"error": "Invalid service token"})
            payload = {"actor_id": str(actor.id), "org_id": str(actor.org_id)}
        else:
            try:
                payload = verify_access_token(token)
            except Exception:
                return JSONResponse(status_code=401, content={"error": "Invalid token"})

            actor = await Actor.get(payload["actor_id"])
            if not actor or actor.status != "active":
                return JSONResponse(
                    status_code=401, content={"error": "Actor not found or inactive"}
                )

        # Set context variables
        current_org_id.set(actor.org_id)
        current_actor_id.set(str(actor.id))

        # Read context propagation headers (from associate API calls)
        correlation_id = request.headers.get("X-Correlation-ID")
        if correlation_id:
            current_correlation_id.set(correlation_id)
        depth_header = request.headers.get("X-Cascade-Depth")
        if depth_header:
            current_depth.set(int(depth_header))
        causation_msg = request.headers.get("X-Causation-Message-ID")
        if causation_msg:
            current_causation_message_id.set(causation_msg)

        # Load roles once per request for permission checks
        roles = await Role.find({"_id": {"$in": actor.role_ids}}).to_list()
        actor._cached_roles = roles

        request.state.actor = actor

        # Claims refresh: auto-refresh if session has stale claims [G-39]
        await _check_claims_freshness(actor, payload, request)

        with create_span("http.request", method=request.method, path=request.url.path):
            response = await call_next(request)
            if hasattr(request.state, "refreshed_token"):
                response.headers["X-Refreshed-Token"] = request.state.refreshed_token
            return response


async def _check_claims_freshness(actor, jwt_payload: dict, request: Request):
    """If the session has stale claims, auto-refresh the access token. [G-39]"""
    from kernel_entities.session import Session

    jti = jwt_payload.get("jti")
    if not jti:
        return

    session = await Session.find_one({"access_token_jti": jti, "status": "active"})
    if not session or not session.claims_stale:
        return

    # Re-load actor's current roles and issue new token
    from kernel.auth.jwt import create_access_token

    roles = await Role.find({"_id": {"$in": actor.role_ids}}).to_list()
    role_names = [r.name for r in roles]

    new_token, new_jti = create_access_token(str(actor.id), str(actor.org_id), role_names)

    session.claims_stale = False
    session.access_token_jti = new_jti
    await session.save()

    # Set on request state so response middleware adds the header
    request.state.refreshed_token = new_token
    request.state.actor_roles = role_names


async def mark_claims_stale(actor_id) -> None:
    """Mark all active sessions for an actor as claims_stale. [G-39]

    Call this when roles are granted or revoked.
    """
    from kernel_entities.session import Session

    await Session.get_motor_collection().update_many(
        {"actor_id": actor_id, "status": "active"},
        {"$set": {"claims_stale": True}},
    )


async def get_current_actor(request: Request) -> Actor:
    """FastAPI dependency to get the authenticated actor."""
    return request.state.actor


def check_permission(actor: Actor, entity_type: str, action: str):
    """Check if actor's roles grant the required permission.

    Wildcard "*" grants access to all entity types.
    Raises PermissionError if denied.
    """
    roles = getattr(actor, "_cached_roles", None)
    if roles is None:
        raise PermissionError("No roles loaded for actor")

    for role in roles:
        allowed_types = role.permissions.get(action, [])
        if "*" in allowed_types or entity_type in allowed_types:
            return  # Permission granted

    raise PermissionError(
        f"Actor {actor.name} does not have '{action}' permission on {entity_type}. "
        f"Roles: {[r.name for r in roles]}"
    )
