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

# Source-system timestamp fields used to compute the incremental fetch watermark.
# These represent "when did this thing happen in the source system" — distinct from
# `created_at` / `updated_at` (OS ingestion / last-mutation time). Tried in order;
# first field that exists on the entity AND has a non-null value on the latest
# record wins. Order keeps existing behavior for Email/Meeting (which use `date`)
# and adds support for newer entity types: SlackMessage (`posted_at`),
# Document (`created_date`).
WATERMARK_FIELD_CANDIDATES = ("date", "posted_at", "created_date")


async def fetch_new(entity_cls, config: dict, org_id, params: dict = {}) -> dict:
    """Fetch new entities from an external system via integration adapter.

    Config: {"system_type": "google_workspace"}
    Params: {"since": "2026-04-01T00:00:00Z", "limit": 100, ...} passed to adapter.fetch()
    """
    with create_span("capability.fetch_new", entity_type=entity_cls.__name__):
        from kernel.integration.dispatch import execute_with_retry, get_adapter

        system_type = config["system_type"]
        adapter = await get_adapter(system_type, org_id=org_id, require_org_only=True)

        # Determine "since" for incremental fetch.
        # Try each candidate timestamp field in WATERMARK_FIELD_CANDIDATES order;
        # first one that exists on the entity AND has a non-null value wins.
        # Falls through to "no since" (adapter fetches all) if no candidate matches.
        fetch_params = {**params}
        if "since" not in fetch_params:
            for field in WATERMARK_FIELD_CANDIDATES:
                try:
                    latest = (
                        await entity_cls.find_scoped({}).sort(f"-{field}").limit(1).to_list()
                    )
                    value = getattr(latest[0], field, None) if latest else None
                    if value:
                        fetch_params["since"] = value.isoformat()
                        break
                except Exception:
                    continue  # Field doesn't exist on this entity type — try next

        # Fetch from external system
        fetch_method = config.get("fetch_method", "fetch")
        raw_results = await execute_with_retry(adapter, fetch_method, **fetch_params)

        # Deduplicate against existing entities by external_ref
        external_refs = [r.get("external_ref") for r in raw_results if r.get("external_ref")]
        existing = set()
        if external_refs:
            existing_entities = await entity_cls.find_scoped(
                {"external_ref": {"$in": external_refs}}
            ).to_list()
            existing = {getattr(e, "external_ref", None) for e in existing_entities}

        # Filter to genuinely new items, then sort ascending by the watermark
        # field so we save oldest-first. This matters when `limit` is set:
        # saving newest-first would advance the watermark past unsaved older
        # items, leaving them stranded forever. Oldest-first means the
        # watermark moves cleanly to "what we just saved" and the next tick
        # picks up the next oldest-window.
        new_items = [item for item in raw_results if item.get("external_ref") not in existing]
        skipped = len(raw_results) - len(new_items)

        sort_field = next(
            (f for f in WATERMARK_FIELD_CANDIDATES if any(f in item for item in new_items)),
            None,
        )
        if sort_field is not None:
            new_items.sort(key=lambda r: r.get(sort_field) or "")

        # Per-call cap on saves (Bug #50 follow-on, fetch_new chunking).
        # Bounds subprocess time when accumulated backlog is large (e.g. Email
        # Fetcher across 11 mailboxes after a stuck period). If `limit` is
        # passed in params, save at most that many; subsequent ticks drain
        # the rest. Default is unbounded — manual backfills (`--data '{"since":...}'`
        # without limit) keep the old "save everything" behavior.
        save_limit = params.get("limit")
        if save_limit is not None and len(new_items) > save_limit:
            new_items = new_items[:save_limit]

        # Create new entities via bulk path
        from kernel.entity.save import bulk_save_tracked

        actor_id = str(current_actor_id.get())

        # Construct all entities (Pydantic validation pass)
        valid_entities = []
        construction_errors = []
        for item in new_items:
            try:
                entity = entity_cls(org_id=org_id, **item)
                valid_entities.append(entity)
            except Exception as e:
                logger.warning(
                    "Failed to construct entity from %s: %s",
                    item.get("external_ref", "?"),
                    e,
                )
                construction_errors.append(
                    {"external_ref": item.get("external_ref"), "error": str(e)}
                )

        # Bulk insert + audit + watch evaluation
        if valid_entities:
            bulk_result = await bulk_save_tracked(
                entities=valid_entities,
                actor_id=actor_id,
                method="fetch_new",
            )
            created = bulk_result["created_ids"]
            errors = construction_errors + bulk_result["errors"]
            # Count dedup-caught items from bulk insert as skipped
            dedup_in_bulk = len(valid_entities) - bulk_result["succeeded"] - bulk_result["errored"]
            skipped += dedup_in_bulk
        else:
            created = []
            errors = construction_errors

        return {
            "fetched": len(raw_results),
            "created": len(created),
            "skipped_duplicates": skipped,
            "errors": errors,
            "created_ids": created,
        }


register_capability("fetch_new", fetch_new)
