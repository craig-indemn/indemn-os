"""Declarative index management for entity collections.

The entity definition is the source of truth for what indexes a collection
should have. `reconcile_indexes(coll, defn)` makes MongoDB match the
definition: drops kernel-managed indexes that aren't requested anymore,
creates missing ones, preserves operator-added custom indexes (those not
matching the kernel naming convention) and the `_id_` primary index.

Closes a kernel gap surfaced 2026-04-27 by the Alliance trace: Meeting
create returned a 500 with a MongoDB DuplicateKeyError on the
`org_id_1_external_ref_1` unique index. external_ref is nullable (manual
meetings have no external system source), but a unique index treats
explicit null as a value, so only one manual meeting per org was allowed.

The fix is a partial filter index: `sparse=True` on a FieldDefinition
translates to `partialFilterExpression: {<field>: {$type: <bson_type>}}`
so the index only covers documents where the field is set to a value of
the declared type — null and missing both get excluded. (Plain MongoDB
`sparse=True` only excludes MISSING; null still gets indexed because
Pydantic explicitly writes null fields.)

Naming convention: the kernel uses MongoDB's default index-name pattern
(`<field>_<direction>(_<field>_<direction>)*`) and always prefixes with
`org_id_1`. So `org_id_1`, `org_id_1_status_1`, `org_id_1_email_1` are
kernel-managed; an operator-added index named `meetings_search_idx` is
not. Custom names are preserved; auto-pattern names are reconciled.
"""

import logging

from kernel.entity.definition import EntityDefinition, FieldDefinition

logger = logging.getLogger(__name__)


# Map FieldDefinition.type strings to MongoDB BSON $type names.
# Used to build a partialFilterExpression that excludes both null and
# missing for fields with sparse=True. See module docstring for why
# `sparse=True` alone isn't sufficient.
_BSON_TYPE_FOR_FIELD: dict[str, str] = {
    "str": "string",
    "int": "long",
    "float": "double",
    "decimal": "decimal",
    "bool": "bool",
    "datetime": "date",
    "date": "date",
    "objectid": "objectId",
    "list": "array",
    "dict": "object",
}


def _kernel_index_name(keys: list[tuple[str, int]]) -> str:
    """Compute the auto-generated index name MongoDB would use for these keys.

    Joins each (field, direction) pair with `_`, then joins pairs with `_`.
    Matches MongoDB's default naming so `desired` keys align with names
    returned by `list_indexes()` for kernel-created indexes.
    """
    return "_".join(f"{f}_{d}" for f, d in keys)


def _is_effectively_nullable(fdef: FieldDefinition) -> bool:
    """True if the field can legitimately be null in stored documents.

    Effectively nullable means: not required to be set at create time, and
    no positive default that would fill in a value. In Pydantic terms,
    `Optional[X]` with no default — the field becomes `null` in MongoDB
    when the create payload omits it.

    Required fields are never effectively nullable (the create handler
    rejects unset values). Fields with a non-None default also aren't
    effectively nullable in practice — every document gets a real value.

    Defensive on missing attrs so partial test mocks (SimpleNamespace
    without `required`/`default`) don't crash — same pattern as
    `getattr(fdef, "sparse", False)` below. The defaults match
    FieldDefinition's: `required=False`, `default=None`.
    """
    if getattr(fdef, "required", False):
        return False
    return getattr(fdef, "default", None) is None


def _partial_filter_for_field(fname: str, fdef: FieldDefinition) -> dict | None:
    """Translate a FieldDefinition into a MongoDB partialFilterExpression
    when the index needs to exclude null/missing documents.

    Plain MongoDB `sparse=True` only excludes MISSING fields, not null ones —
    but Pydantic writes `field: null` explicitly when an Optional field is
    unset, so plain sparse never helps. A `$type` filter excludes both
    null AND missing because null is not of any concrete type.

    Two paths emit a partial filter:

    1. **Explicit `sparse=True` on the FieldDefinition.** Operator opts in.
       Works for any field including `indexed=True` non-unique ones.

    2. **Auto-emit on `unique=True` + effectively-nullable.** The "unique
       with null counted as a value" semantic is MongoDB's surprising default
       but rarely what anyone wants — and it caused Bug #30 (Meeting create
       500, Company create 500, Email.external_ref collision). The kernel
       takes a stand: `unique: true` on a nullable field means "unique when
       set" and we automatically partial-filter the null/missing rows out.
       Operators wanting the legacy "null is a value" semantic must use a
       different field shape (a sentinel string or required+default value).

    Returns None when no filter applies, or when the field type isn't in
    the BSON map (rare — the type strings are validated at definition write
    time, but defensive in case of schema drift).
    """
    explicit_sparse = bool(getattr(fdef, "sparse", False))
    auto_sparse = bool(fdef.unique and _is_effectively_nullable(fdef))
    if not (explicit_sparse or auto_sparse):
        return None
    bson_type = _BSON_TYPE_FOR_FIELD.get(fdef.type)
    if not bson_type:
        return None
    return {fname: {"$type": bson_type}}


def _desired_indexes(defn: EntityDefinition) -> dict[str, dict]:
    """Compute the set of indexes the kernel would create for this entity.

    Returns name -> spec where spec has `keys` (list of (field, dir)),
    `unique` (bool), and `partialFilter` (dict | None — partialFilterExpression
    when the field is sparse). Names are deterministic from the definition
    so we can diff against `coll.list_indexes()`.

    The reason we use partialFilterExpression instead of `sparse=True`:
    plain sparse excludes only MISSING fields, but Pydantic writes nulls
    explicitly. A $type partial filter excludes both null and missing.
    """
    desired: dict[str, dict] = {}

    # Always-on org_id index (every entity collection scopes by org).
    keys = [("org_id", 1)]
    desired[_kernel_index_name(keys)] = {"keys": keys, "unique": False, "partialFilter": None}

    # Compound indexes from the definition's IndexDef list. Always prepend
    # org_id so cross-org queries cannot accidentally use the index path.
    # Compound indexes don't carry per-field type metadata here, so they
    # don't get a partial filter — operators wanting a partial compound
    # index can use the future explicit-partial API (not built yet).
    for idx in defn.indexes:
        keys = [("org_id", 1)] + list(idx.fields)
        desired[_kernel_index_name(keys)] = {
            "keys": keys,
            "unique": idx.unique,
            "partialFilter": None,
        }

    # Field-level flags become (org_id, field) compound indexes. Sparse
    # field flags translate to a partial filter on the field.
    for fname, fdef in defn.fields.items():
        keys = [("org_id", 1), (fname, 1)]
        partial = _partial_filter_for_field(fname, fdef)
        if fdef.unique:
            desired[_kernel_index_name(keys)] = {
                "keys": keys,
                "unique": True,
                "partialFilter": partial,
            }
        elif fdef.indexed:
            desired[_kernel_index_name(keys)] = {
                "keys": keys,
                "unique": False,
                "partialFilter": partial,
            }

    return desired


def _options_match(existing: dict, desired_spec: dict) -> bool:
    """True if an existing MongoDB index has the same options as the desired spec.

    Compares `unique` flag and `partialFilterExpression`. MongoDB's
    `list_indexes()` omits these keys when they're absent. Other options
    (collation, TTL, sparse-without-partial) are not currently managed
    by the kernel.
    """
    existing_unique = bool(existing.get("unique", False))
    existing_partial = existing.get("partialFilterExpression")
    desired_partial = desired_spec.get("partialFilter")
    return (
        existing_unique == desired_spec["unique"]
        and existing_partial == desired_partial
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

    # Diagnostic: log what we see vs what we want — visible in Railway logs
    # so misalignments between the entity definition and MongoDB are
    # debuggable without ssh-ing or attaching.
    logger.info(
        "reconcile_indexes(%s): existing=%s desired=%s",
        coll.name,
        {
            n: {"unique": v.get("unique"), "partialFilter": v.get("partialFilterExpression")}
            for n, v in current_by_name.items()
        },
        {n: {"unique": v["unique"], "partialFilter": v.get("partialFilter")} for n, v in desired.items()},
    )

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
            kwargs: dict = {"unique": spec["unique"]}
            if spec.get("partialFilter"):
                kwargs["partialFilterExpression"] = spec["partialFilter"]
            await coll.create_index(spec["keys"], **kwargs)
            summary["created"].append(name)
            logger.info(
                "Created kernel index %s on %s (unique=%s, partialFilter=%s)",
                name,
                coll.name,
                spec["unique"],
                spec.get("partialFilter"),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not create index %s on %s: %s", name, coll.name, e)

    return summary
