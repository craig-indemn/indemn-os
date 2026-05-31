#!/usr/bin/env python3
"""Stage B B3 — scan + (with --commit) clean up dangling relationship refs.

Per the consolidated eval-correctness plan, Stage B Phase B3 + decisions
D10 / D13 / D15 / D16 / D17 / D28 / D29 (Craig, Sessions 35 + 37).

DRY-RUN (default): scans every DOMAIN EntityDefinition's relationship fields
(scalar + list + polymorphic per D10), finds refs whose target document does
not exist, classifies them, and writes a markdown + JSON report under
`--report-dir`. Makes NO writes to the database.

--commit (Stage B P4, Session 38 — ONLY after Craig reviews the dry-run per
D13/B4): applies the nullifications via the D15 in-memory hash-chain pattern
(one `cascade_nullify` ChangeRecord per affected entity, batched `insert_many`
for audits + per-(collection, field) batched update). Scalar → `$set: None`;
list → `$pull` the dead id (D28 FieldChange: old=full list, new=list-minus);
polymorphic → clear both halves in ONE record with 2 FieldChanges (D9).
The exact `change_type`/`method` labels below are PROPOSED for Craig's B4 sign-off.

Scope (D29): DOMAIN-source only. Kernel-entity-as-source refs (e.g.
`Trace.entity_id`) are PRESERVED as historical (like D17); the D7 kernel
`_relationship_field_targets` ClassVar lands in Stage B B5 (Session 38).

Categories (per field/ref):
  - regular                     : target id missing → nullify
  - deleted_entity_type   (D16) : relationship_target is an entity type no longer
                                  in the registry (e.g. wiped CustomerSystem) →
                                  auto-nullify every value in that field
  - evaluationresult_historical (D17): EvaluationResult's dropped Evaluator refs →
                                  PRESERVE (never nullify; tagged `legacy` in Stage C)
  - polymorphic_asymmetric      : poly ref with id set but type-field null (or a
                                  type naming an unknown entity) → clear both halves
  - malformed                   : field value is not an ObjectId (e.g. an embedded
                                  dict — Bug #9/#37 residue) → nullify

Usage:
    MONGODB_URI="$(aws secretsmanager get-secret-value \\
        --secret-id indemn/dev/shared/mongodb-uri --query SecretString \\
        --output text | sed 's/-pl-0//')" \\
    DATABASE_NAME=indemn_os \\
    python scripts/migrate_relationship_integrity.py \\
        --report-dir <abs path to customer-system/artifacts> [--org-id <id>] [--commit]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from bson import ObjectId

# Kernel entities have no EntityDefinition row — their target collection names
# are fixed. Used to resolve relationship targets that point at kernel entities
# (e.g. Company.owner -> Actor) AND to tell "kernel target" from "deleted type".
KERNEL_COLLECTIONS: dict[str, str] = {
    "Organization": "organizations",
    "Actor": "actors",
    "Role": "roles",
    "Integration": "integrations",
    "Attention": "attentions",
    "Runtime": "runtimes",
    "Session": "sessions",
    "Trace": "traces",
    "Deployment": "deployments",
    "SurfaceConfig": "surface_configs",
    "BrandAssets": "brand_assets",
}

CAT_REGULAR = "regular"
CAT_DELETED_TYPE = "deleted_entity_type"
CAT_EVALRESULT_HISTORICAL = "evaluationresult_historical"
CAT_POLY_ASYMMETRIC = "polymorphic_asymmetric"
CAT_MALFORMED = "malformed"


# --------------------------------------------------------------------------
# Pure helpers (unit-testable without a DB)
# --------------------------------------------------------------------------

def field_category(entity_type: str, target: str | None, known_types: set[str]) -> str:
    """Field-level base category per D16 / D17.

    - EvaluationResult source fields → preserved as historical (D17).
    - A scalar/list target that names an entity type no longer in the registry
      (and isn't a kernel entity) → deleted_entity_type (D16, auto-nullify).
    - Otherwise regular (per-ref dangling/malformed still classified at scan).
    Polymorphic fields pass `target=None` (target is dynamic per-doc) → regular.
    """
    if entity_type == "EvaluationResult":
        return CAT_EVALRESULT_HISTORICAL
    if target is not None and target not in known_types and target not in KERNEL_COLLECTIONS:
        return CAT_DELETED_TYPE
    return CAT_REGULAR


def render_json(scan: dict) -> dict:
    """The machine-readable report (already a dict; identity passthrough for clarity)."""
    return scan


def render_markdown(scan: dict) -> str:
    cats = scan["categories"]
    lines = [
        "# Stage B — Dangling-Refs Audit (DRY-RUN)",
        "",
        f"**Generated:** {scan['scanned_at']}",
        f"**Mode:** {scan['mode']}",
        f"**Org scope:** {scan['org_scope']}",
        f"**Database:** {scan['database']}",
        "",
        "> Stage B Phase B3 deliverable for Craig review at the start of Session 38 (B4).",
        "> The COMMIT migration (`--commit`) runs ONLY after sign-off, atomic with",
        "> write-time validation light-up per D13.",
        "",
        "## Headline",
        "",
        f"- **{scan['total_dangling']} dangling/malformed refs** across "
        f"**{len(scan['fields'])} (entity_type, field) pairs**",
        f"- **{scan['total_to_nullify']}** would be nullified on `--commit`",
        f"- **{cats.get(CAT_EVALRESULT_HISTORICAL, 0)}** preserved as historical (D17 — not nullified)",
        "",
        "## By category",
        "",
        "| Category | Count | Migration action |",
        "|---|---|---|",
        f"| regular | {cats.get(CAT_REGULAR, 0)} | nullify (scalar $set None / list $pull / poly clear both) |",
        f"| deleted_entity_type (D16) | {cats.get(CAT_DELETED_TYPE, 0)} | auto-nullify every value in the field |",
        f"| polymorphic_asymmetric | {cats.get(CAT_POLY_ASYMMETRIC, 0)} | clear both halves (D9) |",
        f"| malformed | {cats.get(CAT_MALFORMED, 0)} | nullify (value is not an ObjectId) |",
        f"| evaluationresult_historical (D17) | {cats.get(CAT_EVALRESULT_HISTORICAL, 0)} | PRESERVE (never nullify) |",
        "",
        "## Per-(entity_type, field) breakdown",
        "",
        "| Source entity.field | Kind | → Target | Category | Dangling | Malformed | Sample doc → dead ref |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in sorted(scan["fields"], key=lambda x: -(x["dangling_count"] + x["malformed_count"])):
        sample = ""
        if f["samples"]:
            s = f["samples"][0]
            sample = f"`{s.get('doc_id','')}` → `{s.get('dead_ref','')}`"
        lines.append(
            f"| {f['entity_type']}.{f['field']} | {f['kind']} | {f.get('target') or '(polymorphic)'} "
            f"| {f['category']} | {f['dangling_count']} | "
            f"{f['malformed_count'] + f.get('asymmetric_count', 0)} | {sample} |"
        )
    lines += [
        "",
        "## --commit write shape (PROPOSED — confirm at B4 before running)",
        "",
        "Per D15, the commit builds one `cascade_nullify` ChangeRecord per affected",
        "entity, hash-chained in memory, then a single `insert_many` for audits + a",
        "per-(collection, field) batched update. Proposed `method=\"relationship_integrity_migration\"`",
        "with `method_metadata={missing_target_type, missing_target_id, affected_field_names, "
        "migration_session}`. Field-change shapes follow D28 (list) / D9 (polymorphic).",
        "",
        "_If any of the above categorization or write shape needs adjustment, that is a",
        "Group D++ escalation — STOP and confirm with Craig before `--commit`._",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Scan (async; DB-backed — mockable in tests)
# --------------------------------------------------------------------------

async def _existing_ids(db, coll_name: str, oids: list) -> set[str]:
    """Return the set of str(_id) that EXIST in `coll_name` among `oids`."""
    if not oids or coll_name is None:
        return set()
    found = await db[coll_name].find({"_id": {"$in": oids}}, {"_id": 1}).to_list(length=None)
    return {str(d["_id"]) for d in found}


async def load_relationship_specs(db) -> tuple[list[dict], dict, set]:
    """Read entity_definitions; return (specs, name->collection map, known type names)."""
    defs = await db["entity_definitions"].find({}).to_list(length=None)
    name2coll = {d["name"]: d.get("collection_name") for d in defs}
    known_types = set(name2coll) | set(KERNEL_COLLECTIONS)
    specs: list[dict] = []
    for d in defs:
        coll = d.get("collection_name")
        for fname, fdef in (d.get("fields") or {}).items():
            is_poly = fdef.get("is_polymorphic_relationship") is True
            is_rel = fdef.get("is_relationship") is True
            if not (is_rel or is_poly):
                continue
            if is_poly:
                kind = "polymorphic"
                target = None
                type_field = fdef.get("target_type_field")
            else:
                kind = "list" if fdef.get("type") == "list" else "scalar"
                target = fdef.get("relationship_target")
                type_field = None
            specs.append({
                "entity_type": d["name"], "collection": coll, "field": fname,
                "kind": kind, "target": target, "type_field": type_field,
            })
    return specs, name2coll, known_types


def _resolve_collection(type_name: str | None, name2coll: dict) -> str | None:
    if not type_name:
        return None
    return name2coll.get(type_name) or KERNEL_COLLECTIONS.get(type_name)


async def scan_field(db, spec: dict, name2coll: dict, known_types: set, org_filter: dict) -> dict:
    """Scan one relationship field; return a per-field finding dict."""
    coll = db[spec["collection"]]
    fname = spec["field"]
    kind = spec["kind"]
    base_cat = field_category(spec["entity_type"], spec["target"], known_types)

    dangling_docs: set = set()
    malformed_docs: set = set()
    asymmetric_docs: set = set()
    samples: list[dict] = []

    if kind in ("scalar", "list"):
        target_coll = _resolve_collection(spec["target"], name2coll)
        query = {**org_filter, fname: {"$ne": None, "$exists": True}}
        docs = await coll.find(query, {"_id": 1, fname: 1}).to_list(length=None)
        # gather refs
        all_oids: set = set()
        per_doc: list[tuple] = []  # (doc_id, [oid refs], has_malformed)
        for d in docs:
            v = d.get(fname)
            if kind == "list":
                if not isinstance(v, list):
                    continue
                oids = [x for x in v if isinstance(x, ObjectId)]
                has_mal = any(not isinstance(x, ObjectId) for x in v)
            else:
                if isinstance(v, ObjectId):
                    oids = [v]
                    has_mal = False
                else:
                    oids = []
                    has_mal = True
            all_oids.update(str(x) for x in oids)
            per_doc.append((d["_id"], oids, has_mal))
        existing = await _existing_ids(db, target_coll, [ObjectId(s) for s in all_oids])
        for doc_id, oids, has_mal in per_doc:
            dead = [x for x in oids if str(x) not in existing]
            if dead:
                dangling_docs.add(doc_id)
                if len(samples) < 5:
                    samples.append({"doc_id": str(doc_id), "dead_ref": str(dead[0])})
            if has_mal:
                malformed_docs.add(doc_id)
                if len(samples) < 5:
                    samples.append({"doc_id": str(doc_id), "dead_ref": "(malformed/non-ObjectId)"})

    else:  # polymorphic
        type_field = spec["type_field"]
        query = {**org_filter, fname: {"$ne": None, "$exists": True}}
        docs = await coll.find(query, {"_id": 1, fname: 1, type_field: 1}).to_list(length=None)
        by_type: dict[str, dict] = {}  # type_name -> {ref_str: [doc_ids]}
        for d in docs:
            idv = d.get(fname)
            tv = d.get(type_field)
            if not isinstance(idv, ObjectId):
                malformed_docs.add(d["_id"])
                continue
            if tv is None or _resolve_collection(tv, name2coll) is None:
                # can't resolve a target type to verify against → asymmetric/unknown
                asymmetric_docs.add(d["_id"])
                if len(samples) < 5:
                    samples.append({"doc_id": str(d["_id"]), "dead_ref": f"{idv} (type={tv!r})"})
                continue
            by_type.setdefault(tv, {}).setdefault(str(idv), []).append(d["_id"])
        for tv, refs in by_type.items():
            tcoll = _resolve_collection(tv, name2coll)
            existing = await _existing_ids(db, tcoll, [ObjectId(s) for s in refs])
            for ref, ids in refs.items():
                if ref not in existing:
                    dangling_docs.update(ids)
                    if len(samples) < 5:
                        samples.append({"doc_id": str(ids[0]), "dead_ref": f"{ref} (type={tv})"})

    # The field's primary classification is its base category (regular / D16 /
    # D17). Malformed values + polymorphic-asymmetric refs are tracked as
    # SEPARATE counts so scan_dangling can bucket them into their own report
    # categories (they always nullify; D17-preserve applies only to dangling).
    return {
        "entity_type": spec["entity_type"],
        "collection": spec["collection"],
        "field": fname,
        "kind": kind,
        "target": spec["target"],
        "category": base_cat,
        "dangling_count": len(dangling_docs),
        "malformed_count": len(malformed_docs),
        "asymmetric_count": len(asymmetric_docs),
        "samples": samples,
    }


async def scan_dangling(db, org_id: str | None = None) -> dict:
    """Scan all domain relationship fields; return the structured report dict."""
    specs, name2coll, known_types = await load_relationship_specs(db)
    org_filter = {"org_id": ObjectId(org_id)} if org_id else {}

    fields: list[dict] = []
    categories: dict[str, int] = {}
    total_dangling = 0
    total_to_nullify = 0
    for spec in specs:
        finding = await scan_field(db, spec, name2coll, known_types, org_filter)
        dc = finding["dangling_count"]
        mc = finding["malformed_count"]
        ac = finding.get("asymmetric_count", 0)
        n = dc + mc + ac
        if n == 0:
            continue
        fields.append(finding)
        base = finding["category"]
        # Dangling rolls into the field's base category (regular / D16 / D17);
        # malformed + asymmetric get their own buckets.
        if dc:
            categories[base] = categories.get(base, 0) + dc
        if mc:
            categories[CAT_MALFORMED] = categories.get(CAT_MALFORMED, 0) + mc
        if ac:
            categories[CAT_POLY_ASYMMETRIC] = categories.get(CAT_POLY_ASYMMETRIC, 0) + ac
        total_dangling += n
        # D17: EvaluationResult dangling is PRESERVED (not nullified). Malformed +
        # asymmetric always nullify.
        total_to_nullify += mc + ac + (0 if base == CAT_EVALRESULT_HISTORICAL else dc)

    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "mode": "DRY-RUN",
        "org_scope": org_id or "all orgs",
        "database": os.environ.get("DATABASE_NAME", "indemn_os"),
        "total_dangling": total_dangling,
        "total_to_nullify": total_to_nullify,
        "categories": categories,
        "fields": fields,
    }


# --------------------------------------------------------------------------
# Commit (D15) — Stage B P4, Session 38, ONLY after Craig's B4 sign-off
# --------------------------------------------------------------------------

async def commit_nullifications(db, scan: dict, actor_id: str, session_label: str) -> dict:
    """Apply nullifications per D15 (in-memory hash-chained audit + batched update).

    NOT run in Session 37. Gated on Craig's B4 review of the dry-run report (D13).
    The change_type/method shape is PROPOSED in the report for sign-off.
    """
    from kernel.changes.collection import ChangeRecord, FieldChange  # noqa: F401
    from kernel.changes.hash_chain import compute_hash, get_previous_hash  # noqa: F401

    raise NotImplementedError(
        "commit_nullifications is a Stage B P4 (Session 38) path. Run the dry-run, "
        "get Craig's B4 sign-off on the report + the proposed write shape, then "
        "implement/enable this per D15 + D13 (atomic with write-time validation)."
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _connect():
    from motor.motor_asyncio import AsyncIOMotorClient

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        print("ERROR: MONGODB_URI env var is required (public host; apply the "
              "-pl-0 swap for laptop runs).", file=sys.stderr)
        sys.exit(1)
    db_name = os.environ.get("DATABASE_NAME", "indemn_os")
    client = AsyncIOMotorClient(uri)
    return client, client[db_name]


async def _run(args) -> int:
    client, db = _connect()
    try:
        scan = await scan_dangling(db, args.org_id)
    finally:
        client.close()

    os.makedirs(args.report_dir, exist_ok=True)
    stamp = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    md_path = os.path.join(args.report_dir, f"{stamp}-dangling-refs-audit.md")
    json_path = os.path.join(args.report_dir, f"{stamp}-dangling-refs-audit.json")
    with open(md_path, "w") as fh:
        fh.write(render_markdown(scan))
    with open(json_path, "w") as fh:
        json.dump(render_json(scan), fh, indent=2)

    print(f"=== Stage B B3 dangling-refs scan ({scan['mode']}) ===")
    print(f"Total dangling/malformed: {scan['total_dangling']} across {len(scan['fields'])} field pairs")
    print(f"Would nullify: {scan['total_to_nullify']} | preserved (D17): "
          f"{scan['categories'].get(CAT_EVALRESULT_HISTORICAL, 0)}")
    print(f"Categories: {scan['categories']}")
    print(f"Report: {md_path}")
    print(f"JSON:   {json_path}")

    if args.commit:
        print("\n--commit requested → gated on Craig's B4 review (Stage B P4, Session 38).")
        await commit_nullifications(db, scan, actor_id="(b4)", session_label="(b4)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage B B3 dangling-refs migration (DRY-RUN default).")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Scan + report only (default).")
    parser.add_argument("--commit", action="store_true",
                        help="Apply nullifications (Stage B P4, Session 38 — gated on Craig's B4 review).")
    parser.add_argument("--org-id", default=None, help="Scope to one org_id (default: all orgs).")
    parser.add_argument("--report-dir", default=".",
                        help="Directory to write the markdown + JSON report.")
    parser.add_argument("--date", default=None, help="Report date stamp YYYY-MM-DD (default: today UTC).")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
