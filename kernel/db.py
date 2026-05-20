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

from kernel.changes.collection import ChangeRecord
from kernel.config import settings
from kernel.entity.definition import EntityDefinition
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
    Trace,
)

logger = logging.getLogger(__name__)

# Kernel entities + kernel infrastructure documents that init_beanie must
# register at startup. Exposed at module-level so tests (and the registration
# guard test in tests/unit/test_kernel_entity_registration.py) can verify
# what's wired without instantiating the FastAPI app.
KERNEL_DOCUMENT_MODELS = [
    Organization,
    Actor,
    Role,
    Integration,
    Attention,
    Runtime,
    Session,
    Trace,
    EntityDefinition,
    Skill,
    Rule,
    RuleGroup,
    Lookup,
    Message,
    MessageLog,
    ChangeRecord,
]

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

    # create_entity_class is needed inside the function (avoids circular imports
    # with kernel.entity.factory which may transitively reference get_database()).
    from kernel.entity.factory import create_entity_class

    # Initialize Beanie with KERNEL models first — EntityDefinition is a Beanie
    # Document, so init_beanie must run before we can construct EntityDefinition objects.
    await init_beanie(database=_db, document_models=KERNEL_DOCUMENT_MODELS)

    for cls in KERNEL_DOCUMENT_MODELS:
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
    RESERVED_NAMES = {cls.__name__ for cls in KERNEL_DOCUMENT_MODELS if hasattr(cls, "__name__")}

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
            # Set the database reference so domain entities can access Motor
            dynamic_cls._db_ref = _db
            ENTITY_REGISTRY[defn.name] = dynamic_cls
        except Exception as e:
            logger.error("Failed to create entity class for %s: %s", defn.name, e)

    # Reconcile indexes for domain entity collections — drops kernel-managed
    # indexes the definition no longer requests, creates missing ones,
    # preserves custom (operator-added) indexes. The previous additive
    # create_index loop was the source of Bug #2 / Bug #25 / Bug #26: stale
    # unique indexes from prior definition versions kept blocking writes.
    from kernel.entity.indexes import reconcile_indexes

    for defn in seen_names.values():
        if defn.name in RESERVED_NAMES:
            continue
        coll = _db[defn.collection_name]
        await reconcile_indexes(coll, defn)

    # Load watch cache
    from kernel.watch.cache import load_watch_cache

    await load_watch_cache()

    logger.info(
        "Database initialized: %d kernel entities, %d domain entities, %d total",
        len(KERNEL_DOCUMENT_MODELS),
        len(seen_names),
        len(ENTITY_REGISTRY),
    )

    return _db


async def register_domain_entity(defn, app=None):
    """Register a domain entity at runtime — no restart required.

    Creates the dynamic class, sets up indexes, adds to ENTITY_REGISTRY,
    and optionally registers API routes on the FastAPI app.

    Called by:
    - POST /api/entitydefinitions (inline registration)
    - PUT /api/entitydefinitions/{name}/enable-capability (re-registration)
    """
    from kernel.entity.factory import create_entity_class

    # Check reserved names
    reserved = {
        name for name, cls in ENTITY_REGISTRY.items() if getattr(cls, "_is_kernel_entity", False)
    }
    if defn.name in reserved:
        raise ValueError(f"'{defn.name}' collides with kernel entity name")

    # Create dynamic class
    dynamic_cls = create_entity_class(defn)
    dynamic_cls._db_ref = _db
    ENTITY_REGISTRY[defn.name] = dynamic_cls

    # Reconcile indexes — drops stale kernel-managed indexes from prior
    # versions of this definition, creates missing ones requested by the
    # current definition. See kernel/entity/indexes.py.
    from kernel.entity.indexes import reconcile_indexes

    coll = _db[defn.collection_name]
    await reconcile_indexes(coll, defn)

    # Register API routes if app provided
    if app is not None:
        from kernel.api.registration import register_entity_routes

        _INFRASTRUCTURE = {
            "EntityDefinition",
            "Skill",
            "Rule",
            "RuleGroup",
            "Lookup",
            "Message",
            "MessageLog",
            "ChangeRecord",
        }
        if defn.name not in _INFRASTRUCTURE:
            register_entity_routes(app, defn.name, dynamic_cls)

    logger.info("Registered domain entity '%s' at runtime", defn.name)
    return dynamic_cls
