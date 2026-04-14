"""Health endpoint — checks connectivity to all dependencies."""

from fastapi import APIRouter

from kernel.config import settings

health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health_check():
    """Check connectivity to all dependencies.
    Returns healthy/degraded/unhealthy with per-dependency status."""
    checks = {}

    # MongoDB
    try:
        from kernel.db import get_database

        db = get_database()
        await db.command("ping")
        checks["mongodb"] = "ok"
    except Exception as e:
        checks["mongodb"] = f"error: {str(e)}"

    # Temporal (if configured)
    if settings.temporal_address and settings.temporal_address != "localhost:7233":
        try:
            from kernel.temporal.client import get_temporal_client

            await get_temporal_client()
            checks["temporal"] = "ok"
        except Exception as e:
            checks["temporal"] = f"error: {str(e)}"

    # Overall status
    all_ok = all(v == "ok" for v in checks.values())
    critical_ok = checks.get("mongodb") == "ok"

    return {
        "status": "healthy" if all_ok else ("degraded" if critical_ok else "unhealthy"),
        "checks": checks,
    }
