"""FastAPI auth middleware.

Sets auth context (org_id, actor_id) on each request via contextvars.
Loads roles once per request for permission checks.
Skips auth for health, bootstrap, and webhook endpoints.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from kernel.auth.jwt import verify_access_token
from kernel.context import current_actor_id, current_correlation_id, current_depth, current_org_id
from kernel_entities.actor import Actor
from kernel_entities.role import Role


class AuthMiddleware(BaseHTTPMiddleware):
    """Set auth context on each request."""

    # Paths that don't require authentication
    PUBLIC_PATHS = {"/health", "/api/_platform/init", "/docs", "/openapi.json"}

    async def dispatch(self, request: Request, call_next):
        # Skip auth for public paths and webhooks
        path = request.url.path
        if path in self.PUBLIC_PATHS or path.startswith("/webhook/"):
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                status_code=401, content={"error": "Missing auth token"}
            )

        token = auth.split(" ", 1)[1]
        try:
            payload = verify_access_token(token)
        except Exception:
            return JSONResponse(
                status_code=401, content={"error": "Invalid token"}
            )

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

        # Load roles once per request for permission checks
        roles = await Role.find({"_id": {"$in": actor.role_ids}}).to_list()
        actor._cached_roles = roles

        request.state.actor = actor
        return await call_next(request)


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
