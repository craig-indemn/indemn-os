"""Schema migration — first-class capability.

Supports: rename_field, add_field (with default), remove_field (with optional cleanup).
Each batch is a MongoDB transaction. Progress is logged. Dry-run available.
"""

import logging
from datetime import datetime, timezone

from kernel.context import current_org_id
from kernel.entity.definition import EntityDefinition, FieldDefinition

logger = logging.getLogger(__name__)


class MigrationPlan:
    """A planned schema migration with preview and execution."""

    def __init__(self, entity_name: str, org_id, operations: list[dict]):
        self.entity_name = entity_name
        self.org_id = org_id
        self.operations = operations
        self.affected_count = 0
        self.preview_sample = []


async def plan_migration(entity_name: str, operations: list[dict]) -> MigrationPlan:
    """Build a migration plan with affected count and sample documents."""
    from kernel.db import ENTITY_REGISTRY

    org_id = current_org_id.get()
    entity_cls = ENTITY_REGISTRY.get(entity_name)
    if not entity_cls:
        raise ValueError(f"Entity type {entity_name} not found")

    plan = MigrationPlan(entity_name, org_id, operations)
    plan.affected_count = await entity_cls.find_scoped({}).count()
    plan.preview_sample = [
        e.model_dump() for e in await entity_cls.find_scoped({}).limit(5).to_list()
    ]
    return plan


async def execute_migration(
    plan: MigrationPlan, batch_size: int = 100, dry_run: bool = False
) -> dict:
    """Execute a schema migration in batches."""
    from kernel.db import ENTITY_REGISTRY

    entity_cls = ENTITY_REGISTRY.get(plan.entity_name)
    collection = entity_cls.get_motor_collection()

    total = plan.affected_count
    processed = 0
    errors = []

    for op in plan.operations:
        if dry_run:
            logger.info("DRY RUN: Would apply %s on %d documents", op["type"], total)
            continue

        if op["type"] == "rename_field":
            old_name, new_name = op["from"], op["to"]
            while processed < total:
                batch = (
                    await collection.find(
                        {"org_id": plan.org_id, old_name: {"$exists": True}},
                    )
                    .limit(batch_size)
                    .to_list(batch_size)
                )
                if not batch:
                    break
                client = collection.database.client
                async with await client.start_session() as session:
                    async with session.start_transaction():
                        for doc in batch:
                            await collection.update_one(
                                {"_id": doc["_id"]},
                                {"$rename": {old_name: new_name}},
                                session=session,
                            )
                        processed += len(batch)
                logger.info("Migration progress: %d/%d", processed, total)

            # Update entity definition
            defn = await EntityDefinition.find_one(
                {"name": plan.entity_name, "org_id": plan.org_id}
            )
            if defn and old_name in defn.fields:
                defn.fields[new_name] = defn.fields.pop(old_name)
                defn.updated_at = datetime.now(timezone.utc)
                await defn.save()

        elif op["type"] == "add_field":
            # Add with default — no document migration needed for MongoDB
            defn = await EntityDefinition.find_one(
                {"name": plan.entity_name, "org_id": plan.org_id}
            )
            if defn:
                defn.fields[op["name"]] = FieldDefinition(**op["field_def"])
                defn.updated_at = datetime.now(timezone.utc)
                await defn.save()

        elif op["type"] == "remove_field":
            defn = await EntityDefinition.find_one(
                {"name": plan.entity_name, "org_id": plan.org_id}
            )
            if defn and op["name"] in defn.fields:
                del defn.fields[op["name"]]
                defn.updated_at = datetime.now(timezone.utc)
                await defn.save()
            if op.get("cleanup", False):
                await collection.update_many(
                    {"org_id": plan.org_id},
                    {"$unset": {op["name"]: ""}},
                )

    return {
        "status": "completed" if not dry_run else "dry_run",
        "processed": processed,
        "errors": errors,
        "operations": len(plan.operations),
    }
