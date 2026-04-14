"""MongoDB connection, Beanie initialization, entity registry.

ENTITY_REGISTRY is the central registry of all entity types (kernel + domain).
Populated at startup by init_database:
1. Kernel entities (Python classes) registered first
2. Domain entity definitions loaded from all orgs, merged by name
3. Reserved name guard prevents domain entities from colliding with kernel entities
4. Beanie initialized with all models
5. Watch cache loaded
"""

import logging

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from kernel.config import settings

logger = logging.getLogger(__name__)

# The central registry: entity_name → class
# Populated by init_database at startup
ENTITY_REGISTRY: dict[str, type] = {}

# Module-level client reference
_client: AsyncIOMotorClient | None = None
_db = None


def get_client() -> AsyncIOMotorClient | None:
    """Get the MongoDB client (set during init_database)."""
    return _client


def get_database():
    """Get the MongoDB database (set during init_database)."""
    return _db


async def init_database():
    """Initialize MongoDB and all entity classes."""
    global _client, _db

    _client = AsyncIOMotorClient(
        settings.mongodb_uri,
        maxPoolSize=settings.mongodb_max_pool_size,
    )
    _db = _client[settings.database_name]

    # Import all models
    from kernel.changes.collection import ChangeRecord
    from kernel.entity.definition import EntityDefinition
    from kernel.entity.factory import create_entity_class
    from kernel.message.schema import Message, MessageLog
    from kernel.rule.lookup import Lookup
    from kernel.rule.schema import Rule, RuleGroup
    from kernel.skill.schema import Skill
    from kernel_entities import (
        Actor,
        Attention,
        Integration,
        Organization,
        Role,
        Runtime,
        Session,
    )

    # Kernel entities + kernel infrastructure documents
    kernel_models = [
        Organization,
        Actor,
        Role,
        Integration,
        Attention,
        Runtime,
        Session,
        EntityDefinition,
        Skill,
        Rule,
        RuleGroup,
        Lookup,
        Message,
        MessageLog,
        ChangeRecord,
    ]
    for cls in kernel_models:
        if hasattr(cls, "__name__"):
            ENTITY_REGISTRY[cls.__name__] = cls

    # Load domain entity definitions from ALL orgs.
    # Entity definitions are per-org. At startup, the kernel loads all definitions
    # across all orgs, deduplicating by name (same-name definitions across orgs
    # produce the same Python class — org scoping happens at query time via org_id).
    # If two orgs define "Submission" with different fields, the UNION of fields
    # is used for the Python class.
    defs_coll = _db["entity_definitions"]
    seen_names: dict[str, EntityDefinition] = {}
    async for doc in defs_coll.find({}):
        try:
            defn = EntityDefinition(**doc)
        except Exception as e:
            logger.error("Failed to parse entity definition: %s", e)
            continue
        if defn.name in seen_names:
            # Merge: union of fields from all orgs' definitions of this name
            existing = seen_names[defn.name]
            for fname, fdef in defn.fields.items():
                if fname not in existing.fields:
                    existing.fields[fname] = fdef
            continue
        seen_names[defn.name] = defn

    # Reserved names: kernel entity names cannot be used for domain entities
    RESERVED_NAMES = {cls.__name__ for cls in kernel_models if hasattr(cls, "__name__")}

    for defn in seen_names.values():
        if defn.name in RESERVED_NAMES:
            logger.error(
                "Domain entity '%s' collides with kernel entity name. "
                "Reserved names: %s. Skipping.",
                defn.name,
                RESERVED_NAMES,
            )
            continue
        try:
            dynamic_cls = create_entity_class(defn)
            ENTITY_REGISTRY[defn.name] = dynamic_cls
        except Exception as e:
            logger.error("Failed to create entity class for %s: %s", defn.name, e)

    # Initialize Beanie with all registered models
    all_models = list(ENTITY_REGISTRY.values())
    await init_beanie(database=_db, document_models=all_models)

    # Load watch cache
    from kernel.watch.cache import load_watch_cache

    await load_watch_cache()

    logger.info(
        "Database initialized: %d kernel entities, %d domain entities, %d total",
        len(kernel_models),
        len(seen_names),
        len(ENTITY_REGISTRY),
    )

    return _db
