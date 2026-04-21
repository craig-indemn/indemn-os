"""fetch_new — collection-level capability for ingesting entities from external systems.

Resolves Integration by system_type, calls adapter.fetch(), deduplicates by external_ref,
creates new entities via save_tracked(). Generic — works for any entity type with an
external_ref field and an integration adapter.

Unlike instance-level capabilities (auto_classify, stale_check), this operates on the
entity TYPE, not an entity instance. It creates new entities rather than modifying existing ones.
"""

import logging

from kernel.capability.registry import register_capability
from kernel.context import current_actor_id
from kernel.observability.tracing import create_span

logger = logging.getLogger(__name__)


async def fetch_new(entity_cls, config: dict, org_id, params: dict = {}) -> dict:
    """Fetch new entities from an external system via integration adapter.

    Config: {"system_type": "google_workspace"}
    Params: {"since": "2026-04-01T00:00:00Z", "limit": 100, ...} passed to adapter.fetch()
    """
    with create_span("capability.fetch_new", entity_type=entity_cls.__name__):
        from kernel.integration.dispatch import execute_with_retry, get_adapter

        system_type = config["system_type"]
        adapter = await get_adapter(system_type, org_id=org_id, require_org_only=True)

        # Determine "since" for incremental fetch
        fetch_params = {**params}
        if "since" not in fetch_params:
            # Use most recent entity's date field (actual meeting date, not ingestion time)
            # _DomainQuery.sort() takes a string: "-date" for descending
            try:
                latest = await entity_cls.find_scoped({}).sort("-date").limit(1).to_list()
                if latest and hasattr(latest[0], "date") and latest[0].date:
                    fetch_params["since"] = latest[0].date.isoformat()
            except Exception:
                pass  # No existing entities or no date field — fetch all

        # Fetch from external system
        raw_results = await execute_with_retry(adapter, "fetch", **fetch_params)

        # Deduplicate against existing entities by external_ref
        external_refs = [
            r.get("external_ref") for r in raw_results if r.get("external_ref")
        ]
        existing = set()
        if external_refs:
            existing_entities = await entity_cls.find_scoped(
                {"external_ref": {"$in": external_refs}}
            ).to_list()
            existing = {getattr(e, "external_ref", None) for e in existing_entities}

        # Create new entities
        created = []
        skipped = 0
        errors = []
        actor_id = str(current_actor_id.get())
        for item in raw_results:
            if item.get("external_ref") in existing:
                skipped += 1
                continue
            try:
                entity = entity_cls(org_id=org_id, **item)
                await entity.save_tracked(actor_id=actor_id, method="fetch_new")
                created.append(str(entity.id))
            except Exception as e:
                logger.warning(
                    "Failed to create entity from %s: %s",
                    item.get("external_ref", "?"),
                    e,
                )
                errors.append(
                    {"external_ref": item.get("external_ref"), "error": str(e)}
                )

        return {
            "fetched": len(raw_results),
            "created": len(created),
            "skipped_duplicates": skipped,
            "errors": errors,
            "created_ids": created,
        }


register_capability("fetch_new", fetch_new)
