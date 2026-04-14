"""MongoDB connection, Beanie initialization, entity registry.

ENTITY_REGISTRY is the central registry of all entity types (kernel + domain).
Full init_database implementation is in L11 (Task 13).
"""

from motor.motor_asyncio import AsyncIOMotorClient

from kernel.config import settings

# The central registry: entity_name → class
# Populated by init_database at startup
ENTITY_REGISTRY: dict[str, type] = {}

# Module-level client reference for get_client/get_database
_client: AsyncIOMotorClient | None = None
_db = None


def get_client() -> AsyncIOMotorClient | None:
    """Get the MongoDB client (set during init_database)."""
    return _client


def get_database():
    """Get the MongoDB database (set during init_database)."""
    return _db


async def init_database():
    """Initialize MongoDB and all entity classes.
    Full implementation in L11 (Task 13) — this is the stub."""
    global _client, _db

    _client = AsyncIOMotorClient(
        settings.mongodb_uri,
        maxPoolSize=settings.mongodb_max_pool_size,
    )
    _db = _client[settings.database_name]

    # Full init (kernel entities, domain entities, Beanie) added in L11
    return _db
