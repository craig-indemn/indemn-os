"""FastAPI application factory.

The API server is the gateway — the only service that faces the internet.
Every CLI command, UI interaction, harness operation, and Tier 3 API call
goes through it.
"""

from fastapi import FastAPI

from kernel.api.admin_routes import admin_router
from kernel.api.bootstrap import bootstrap_router
from kernel.api.bulk import bulk_router
from kernel.api.direct_invoke import invoke_router
from kernel.api.errors import register_error_handlers
from kernel.api.health import health_router
from kernel.api.human_review import review_router
from kernel.api.integration_routes import integration_mgmt_router
from kernel.api.lookup_routes import lookup_router
from kernel.api.meta import meta_router
from kernel.api.queue_routes import queue_router
from kernel.api.webhook import webhook_router
from kernel.auth.middleware import AuthMiddleware
from kernel.observability.tracing import init_tracing


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="Indemn OS API", version="0.1.0")

    register_error_handlers(app)

    @app.on_event("startup")
    async def startup():
        init_tracing()

        from kernel.api.registration import register_entity_routes
        from kernel.db import ENTITY_REGISTRY, init_database

        # Import adapters so they register via register_adapter()
        import kernel.integration.adapters  # noqa: F401

        await init_database()

        # Register routes for entity types (skip infrastructure documents)
        _INFRASTRUCTURE = {
            "EntityDefinition", "Skill", "Rule", "RuleGroup", "Lookup",
            "Message", "MessageLog", "ChangeRecord",
        }
        for name, cls in ENTITY_REGISTRY.items():
            if name not in _INFRASTRUCTURE:
                register_entity_routes(app, name, cls)

    @app.on_event("shutdown")
    async def shutdown():
        """Graceful shutdown: close connections."""
        from kernel.db import get_client

        client = get_client()
        if client:
            client.close()

    app.add_middleware(AuthMiddleware)
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

    return app


# Entry point for `python -m kernel.api.app`
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app, host="0.0.0.0", port=8000, factory=True)
