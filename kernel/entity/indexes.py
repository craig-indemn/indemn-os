"""Declarative index management for entity collections.

The entity definition is the source of truth for what indexes a collection
should have. `reconcile_indexes(coll, defn)` makes MongoDB match the
definition: drops kernel-managed indexes that aren't requested anymore,
creates missing ones, preserves operator-added custom indexes (those not
matching the kernel naming convention) and the `_id_` primary index.

Closes a kernel gap surfaced 2026-04-27 by the Alliance trace: Meeting
create returned a 500 with a MongoDB DuplicateKeyError on a stale
`org_id_1_external_ref_1` unique index. The current Meeting entity
definition does not declare unique on external_ref — the index was a
relic of a prior version of the definition. The kernel only ever ADDED
indexes (idempotent `create_index`); it never removed ones the operator
had stopped requesting. This module fixes that.

Naming convention: the kernel uses MongoDB's default index-name pattern
(`<field>_<direction>(_<field>_<direction>)*`) and always prefixes with
`org_id_1`. So `org_id_1`, `org_id_1_status_1`, `org_id_1_email_1` are
kernel-managed; an operator-added index named `meetings_search_idx` is
not. Custom names are preserved; auto-pattern names are reconciled.
"""

import logging

from kernel.entity.definition import EntityDefinition

logger = logging.getLogger(__name__)


def _kernel_index_name(keys: list[tuple[str, int]]) -> str:
    """Compute the auto-generated index name MongoDB would use for these keys.

    Joins each (field, direction) pair with `_`, then joins pairs with `_`.
    Matches MongoDB's default naming so `desired` keys align with names
    returned by `list_indexes()` for kernel-created indexes.
    """
    return "_".join(f"{f}_{d}" for f, d in keys)


def _desired_indexes(defn: EntityDefinition) -> dict[str, dict]:
    """Compute the set of indexes the kernel would create for this entity.

    Returns name -> spec where spec has `keys` (list of (field, dir)),
    `unique` (bool), and `sparse` (bool). Names are deterministic from the
    definition so we can diff against `coll.list_indexes()`.
    """
    desired: dict[str, dict] = {}

    # Always-on org_id index (every entity collection scopes by org).
    keys = [("org_id", 1)]
    desired[_kernel_index_name(keys)] = {"keys": keys, "unique": False, "sparse": False}

    # Compound indexes from the definition's IndexDef list. Always prepend
    # org_id so cross-org queries cannot accidentally use the index path.
    for idx in defn.indexes:
        keys = [("org_id", 1)] + list(idx.fields)
        desired[_kernel_index_name(keys)] = {
            "keys": keys,
            "unique": idx.unique,
            "sparse": getattr(idx, "sparse", False),
        }

    # Field-level flags become (org_id, field) compound indexes.
    for fname, fdef in defn.fields.items():
        keys = [("org_id", 1), (fname, 1)]
        sparse = getattr(fdef, "sparse", False)
        if fdef.unique:
            desired[_kernel_index_name(keys)] = {"keys": keys, "unique": True, "sparse": sparse}
        elif fdef.indexed:
            desired[_kernel_index_name(keys)] = {"keys": keys, "unique": False, "sparse": sparse}

    return desired


def _options_match(existing: dict, desired_spec: dict) -> bool:
    """True if an existing MongoDB index has the same options as the desired spec.

    Compares `unique` and `sparse` flags. MongoDB's `list_indexes()` omits
    these keys when they're false, so missing == False. Other options
    (collation, partial filter expressions, TTL) are not currently managed
    by the kernel, so we don't compare them.
    """
    existing_unique = bool(existing.get("unique", False))
    existing_sparse = bool(existing.get("sparse", False))
    return (
        existing_unique == desired_spec["unique"]
        and existing_sparse == desired_spec["sparse"]
    )


def _is_kernel_managed_name(name: str) -> bool:
    """True if `name` matches the kernel's auto-generated naming convention.

    Kernel-managed names always start with `org_id_1` (every kernel-created
    index is scoped to org_id). The `_id_` primary index is explicitly NOT
    managed — MongoDB owns it and it's never droppable.

    Custom indexes added by operators with explicit `name=...` (or
    descriptive names like `meetings_search_text_idx`) won't start with
    `org_id_1` and are preserved untouched. This is the contract: if you
    want the kernel to manage your index, declare it on the entity
    definition; if you want a hand-rolled index, give it a non-kernel name.
    """
    if name == "_id_":
        return False
    return name.startswith("org_id_1")


async def reconcile_indexes(coll, defn: EntityDefinition) -> dict:
    """Make `coll` indexes match `defn`. Idempotent.

    Three buckets after reconciliation:
      - dropped: kernel-managed indexes no longer requested by the definition
      - created: requested indexes that didn't exist
      - preserved: indexes left untouched (custom-named, _id_, or
        already-matching kernel-managed)

    Returns a summary dict with each bucket as a list of index names. The
    summary is mostly for logging/tests; callers can ignore it.

    Errors during drop/create are logged at WARNING (not raised) so a
    single bad index doesn't take down the whole startup reconciliation
    loop. The next startup retries.
    """
    desired = _desired_indexes(defn)

    current_by_name: dict[str, dict] = {}
    async for idx in coll.list_indexes():
        current_by_name[idx["name"]] = idx

    summary: dict[str, list[str]] = {"created": [], "dropped": [], "preserved": []}

    # Drop pass: kernel-managed indexes that either aren't in the desired
    # set OR exist with mismatched options (e.g., a unique index that
    # should now be sparse). Custom-named and `_id_` are always preserved.
    for name in list(current_by_name.keys()):
        if not _is_kernel_managed_name(name):
            summary["preserved"].append(name)
            continue
        if name in desired and _options_match(current_by_name[name], desired[name]):
            summary["preserved"].append(name)
            continue
        # Either no longer desired, or options differ — drop and let the
        # create pass below rebuild it from the current spec.
        try:
            await coll.drop_index(name)
            summary["dropped"].append(name)
            del current_by_name[name]
            logger.info("Dropped kernel index %s on %s (stale or option mismatch)", name, coll.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not drop index %s on %s: %s", name, coll.name, e)

    # Create pass: desired indexes that don't exist yet (or were just dropped).
    for name, spec in desired.items():
        if name in current_by_name:
            # Already there with matching options — leave it alone.
            continue
        try:
            await coll.create_index(
                spec["keys"],
                unique=spec["unique"],
                sparse=spec["sparse"],
            )
            summary["created"].append(name)
            logger.info("Created kernel index %s on %s", name, coll.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not create index %s on %s: %s", name, coll.name, e)

    return summary
