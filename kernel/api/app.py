"""FastAPI application factory.

The API server is the gateway — the only service that faces the internet.
Every CLI command, UI interaction, harness operation, and Tier 3 API call
goes through it.
"""

import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal

import orjson
from bson import ObjectId
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from kernel.api.admin_routes import admin_router
from kernel.api.assistant import assistant_router
from kernel.api.auth_routes import auth_router
from kernel.api.bootstrap import bootstrap_router
from kernel.api.bulk import bulk_router
from kernel.api.direct_invoke import invoke_router
from kernel.api.errors import register_error_handlers
from kernel.api.events import events_router
from kernel.api.health import health_router
from kernel.api.human_review import review_router
from kernel.api.integration_routes import integration_mgmt_router
from kernel.api.interaction import interaction_router
from kernel.api.lookup_routes import lookup_router
from kernel.api.meta import meta_router
from kernel.api.queue_routes import queue_router
from kernel.api.rule_routes import rule_router
from kernel.api.skill_routes import skill_router
from kernel.api.webhook import webhook_router
from kernel.api.websocket import websocket_handler
from kernel.auth.middleware import AuthMiddleware
from kernel.observability.tracing import init_tracing

logger = logging.getLogger(__name__)


class _ORJSONResponse(JSONResponse):
    """JSON response that handles bson.ObjectId, datetime, Decimal."""

    media_type = "application/json"

    def render(self, content) -> bytes:
        return orjson.dumps(content, default=self._default)

    @staticmethod
    def _default(obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title="Indemn OS API",
        version="0.2.0",
        default_response_class=_ORJSONResponse,
    )

    register_error_handlers(app)

    @app.on_event("startup")
    async def startup():
        init_tracing()

        # Import adapters and capabilities so they self-register
        import kernel.capability  # noqa: F401
        import kernel.integration.adapters  # noqa: F401
        from kernel.api.registration import register_entity_routes
        from kernel.db import ENTITY_REGISTRY, init_database

        await init_database()

        # Register routes for entity types (skip infrastructure documents)
        _INFRASTRUCTURE = {
            "EntityDefinition", "Skill", "Rule", "RuleGroup", "Lookup",
            "Message", "MessageLog", "ChangeRecord",
        }
        for name, cls in ENTITY_REGISTRY.items():
            if name not in _INFRASTRUCTURE:
                register_entity_routes(app, name, cls)

        # Bootstrap revocation cache [G-42]
        from kernel.auth.jwt import bootstrap_revocation_cache, watch_revocations

        await bootstrap_revocation_cache()
        asyncio.create_task(watch_revocations())
        logger.info("Revocation cache bootstrapped, watcher started")

    @app.on_event("shutdown")
    async def shutdown():
        """Graceful shutdown: close connections."""
        from kernel.db import get_client

        client = get_client()
        if client:
            client.close()

    app.add_middleware(AuthMiddleware)

    # CORS must be outermost (added last) so preflight OPTIONS requests
    # are handled before AuthMiddleware rejects them for missing tokens.
    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Dev only — restrict in production
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Refreshed-Token"],
    )

    # Phase 1-3 routers
    app.include_router(meta_router)
    app.include_router(health_router)
    app.include_router(bootstrap_router)
    app.include_router(invoke_router)
    app.include_router(review_router)
    app.include_router(bulk_router)
    app.include_router(integration_mgmt_router)
    app.include_router(webhook_router)
    app.include_router(queue_router)
    app.include_router(lookup_router)
    app.include_router(admin_router)
    app.include_router(skill_router)
    app.include_router(rule_router)

    # Phase 4+5 routers
    app.include_router(auth_router)
    app.include_router(events_router)
    app.include_router(interaction_router)
    app.include_router(assistant_router)

    # WebSocket endpoint [G-34]
    app.add_api_websocket_route("/ws", websocket_handler)

    return app


# Entry point for `python -m kernel.api.app`
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app, host="0.0.0.0", port=8000, factory=True)
