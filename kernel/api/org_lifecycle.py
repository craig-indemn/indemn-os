"""Org lifecycle operations — export, import, clone, diff, deploy.

Configuration-only operations. Entity instances (business data), messages,
changes, sessions, attentions, and secrets are never exported or cloned.
"""

import logging
from datetime import datetime, timezone

from bson import ObjectId

from kernel.entity.definition import EntityDefinition
from kernel.rule.lookup import Lookup
from kernel.rule.schema import Rule
from kernel.skill.schema import Skill

logger = logging.getLogger(__name__)


async def export_org_config(org_id: ObjectId) -> dict:
    """Export all configuration for an org (no entity instances).

    Returns a dict with categories as keys, each containing name→data mappings.
    """
    config = {
        "org": {},
        "entities": {},
        "rules": {},
        "lookups": {},
        "skills": {},
        "roles": {},
        "actors": {},
        "integrations": {},
        "capabilities": {},
    }

    # Org-level settings
    from kernel.db import get_database

    db = get_database()
    org_doc = await db["organizations"].find_one({"_id": org_id})
    if org_doc:
        config["org"] = _serialize_bson(
            {
                k: v
                for k, v in org_doc.items()
                if k not in ("_id", "org_id", "created_at", "updated_at")
            }
        )

    # Entity definitions
    defs = await EntityDefinition.find({"org_id": org_id}).to_list()
    for d in defs:
        dumped = _dump_doc(
            d,
            exclude={
                "_id",
                "id",
                "org_id",
                "created_at",
                "updated_at",
                "created_by",
            },
        )
        # Extract capability activations into separate category
        caps = dumped.pop("activated_capabilities", [])
        if caps:
            config["capabilities"][d.name] = caps
        config["entities"][d.name] = dumped

    # Rules
    rules = await Rule.find({"org_id": org_id, "status": {"$ne": "archived"}}).to_list()
    for r in rules:
        dumped = _dump_doc(
            r,
            exclude={
                "_id",
                "id",
                "org_id",
                "created_at",
                "created_by",
                "group_id",
            },
        )
        rule_key = r.name or str(r.id)
        config["rules"][rule_key] = dumped

    # Lookups
    lookups = await Lookup.find({"org_id": org_id}).to_list()
    for lk in lookups:
        dumped = _dump_doc(
            lk,
            exclude={
                "_id",
                "id",
                "org_id",
                "created_at",
                "created_by",
            },
        )
        config["lookups"][lk.name] = dumped

    # Skills
    skills = await Skill.find({"org_id": org_id, "status": {"$ne": "deprecated"}}).to_list()
    for s in skills:
        dumped = _dump_doc(
            s,
            exclude={
                "_id",
                "id",
                "org_id",
                "created_at",
                "updated_at",
                "created_by",
            },
        )
        config["skills"][s.name] = dumped

    # Roles (kernel entity — query via Motor)
    from kernel.db import get_database

    db = get_database()
    roles_coll = db["roles"]
    async for doc in roles_coll.find({"org_id": org_id}):
        name = doc.get("name", str(doc["_id"]))
        _ROLE_EXCLUDE = {
            "_id",
            "org_id",
            "created_at",
            "updated_at",
            "created_by",
        }
        dumped = {k: v for k, v in doc.items() if k not in _ROLE_EXCLUDE}
        dumped = _serialize_bson(dumped)
        config["roles"][name] = dumped

    # Associate actors (not human actors — those are per-person)
    actors_coll = db["actors"]
    _ACTOR_EXCLUDE = {
        "_id",
        "org_id",
        "created_at",
        "updated_at",
        "created_by",
        "authentication_methods",
    }
    async for doc in actors_coll.find({"org_id": org_id, "type": "associate"}):
        name = doc.get("name", str(doc["_id"]))
        dumped = {k: v for k, v in doc.items() if k not in _ACTOR_EXCLUDE}
        dumped = _serialize_bson(dumped)
        config["actors"][name] = dumped

    # Integration configs (without secrets)
    integ_coll = db["integrations"]
    _INTEG_EXCLUDE = {
        "_id",
        "org_id",
        "created_at",
        "updated_at",
        "created_by",
        "secret_ref",
        "last_checked_at",
        "last_error",
    }
    async for doc in integ_coll.find({"org_id": org_id}):
        name = doc.get("name", str(doc["_id"]))
        dumped = {k: v for k, v in doc.items() if k not in _INTEG_EXCLUDE}
        dumped = _serialize_bson(dumped)
        config["integrations"][name] = dumped

    return config


async def import_org_config(target_org_name: str, config: dict) -> dict:
    """Import configuration into a new organization.

    Creates the org first, then imports all config categories.
    """
    from kernel_entities import Organization

    # Create the target org
    org_id = ObjectId()
    slug = target_org_name.lower().replace(" ", "-")
    org = Organization(
        id=org_id,
        org_id=org_id,
        name=target_org_name,
        slug=slug,
        status="active",
    )
    await org.insert()

    items_imported = 0

    # Entity definitions
    for name, defn_data in config.get("entities", {}).items():
        defn = EntityDefinition(org_id=org_id, **defn_data)
        await defn.insert()
        items_imported += 1

    # Rules
    for name, rule_data in config.get("rules", {}).items():
        rule_data.pop("version", None)
        rule = Rule(
            org_id=org_id,
            created_by="import",
            **rule_data,
        )
        await rule.insert()
        items_imported += 1

    # Lookups
    for name, lookup_data in config.get("lookups", {}).items():
        lookup = Lookup(
            org_id=org_id,
            created_by="import",
            **lookup_data,
        )
        await lookup.insert()
        items_imported += 1

    # Skills
    for name, skill_data in config.get("skills", {}).items():
        skill_data.pop("version", None)
        skill = Skill(
            org_id=org_id,
            created_by="import",
            **skill_data,
        )
        await skill.insert()
        items_imported += 1

    # Roles — insert via Motor (kernel entity)
    from kernel.db import get_database

    db = get_database()
    # Direct insert with explicit org_id — intentional bypass of
    # OrgScopedCollection because we're creating entities in a new org
    # during import (org_id is the target, not current context)
    role_id_map = {}  # old role name -> new ObjectId
    for name, role_data in config.get("roles", {}).items():
        role_id = ObjectId()
        role_data["_id"] = role_id
        role_data["org_id"] = org_id
        role_data["created_at"] = datetime.now(timezone.utc)
        role_data["updated_at"] = datetime.now(timezone.utc)
        role_data["version"] = 1
        # Ensure watches is a list
        if "watches" not in role_data:
            role_data["watches"] = []
        await db["roles"].insert_one(role_data)
        role_id_map[name] = role_id
        items_imported += 1

    # Direct insert with explicit org_id — intentional bypass of
    # OrgScopedCollection (org_id is the target, not current context)
    for name, actor_data in config.get("actors", {}).items():
        actor_id = ObjectId()
        actor_data["_id"] = actor_id
        actor_data["org_id"] = org_id
        actor_data["created_at"] = datetime.now(timezone.utc)
        actor_data["updated_at"] = datetime.now(timezone.utc)
        actor_data["version"] = 1
        actor_data["status"] = "provisioned"
        actor_data["authentication_methods"] = []
        # Resolve role references by name
        role_ids = []
        for rid in actor_data.pop("role_ids", []):
            # Try to resolve as role name from our map
            if isinstance(rid, str) and rid in role_id_map:
                role_ids.append(role_id_map[rid])
            elif isinstance(rid, str):
                try:
                    role_ids.append(ObjectId(rid))
                except Exception:
                    pass
        actor_data["role_ids"] = role_ids
        await db["actors"].insert_one(actor_data)
        items_imported += 1

    # Direct insert with explicit org_id — intentional bypass of
    # OrgScopedCollection (org_id is the target, not current context)
    for name, integ_data in config.get("integrations", {}).items():
        integ_id = ObjectId()
        integ_data["_id"] = integ_id
        integ_data["org_id"] = org_id
        integ_data["created_at"] = datetime.now(timezone.utc)
        integ_data["updated_at"] = datetime.now(timezone.utc)
        integ_data["version"] = 1
        # Ensure owner_id is ObjectId
        if "owner_id" in integ_data and isinstance(integ_data["owner_id"], str):
            try:
                integ_data["owner_id"] = ObjectId(integ_data["owner_id"])
            except Exception:
                integ_data["owner_id"] = org_id
        await db["integrations"].insert_one(integ_data)
        items_imported += 1

    return {
        "org_id": str(org_id),
        "org_slug": slug,
        "items_imported": items_imported,
    }


async def clone_org(source_org_id: ObjectId, target_org_name: str) -> dict:
    """Clone an org's configuration to a new org. Export + import in one step."""
    config = await export_org_config(source_org_id)
    result = await import_org_config(target_org_name, config)
    result["items_copied"] = result.pop("items_imported")
    result["target_org_slug"] = result.pop("org_slug")
    return result


async def diff_org_configs(org_a_id: ObjectId, org_b_id: ObjectId) -> dict:
    """Diff entity definitions and configuration between two orgs."""
    config_a = await export_org_config(org_a_id)
    config_b = await export_org_config(org_b_id)

    differences = []

    for category in ("entities", "rules", "lookups", "skills", "roles", "actors", "integrations"):
        items_a = config_a.get(category, {})
        items_b = config_b.get(category, {})

        all_names = set(list(items_a.keys()) + list(items_b.keys()))
        for name in sorted(all_names):
            in_a = name in items_a
            in_b = name in items_b
            if in_a and not in_b:
                differences.append({"type": category, "name": name, "change": "only_in_a"})
            elif in_b and not in_a:
                differences.append({"type": category, "name": name, "change": "only_in_b"})
            elif items_a[name] != items_b[name]:
                differences.append({"type": category, "name": name, "change": "modified"})

    return {"differences": differences}


async def deploy_org(
    source_org_id: ObjectId, target_org_id: ObjectId, dry_run: bool = True
) -> dict:
    """Deploy configuration from source to target org.

    Dry run shows what would change. Apply mode updates the target.
    """
    diff_result = await diff_org_configs(source_org_id, target_org_id)
    changes = diff_result["differences"]

    if dry_run:
        return {"dry_run": True, "changes": changes}

    # Apply changes: for each item only_in_a or modified, copy from source to target
    source_config = await export_org_config(source_org_id)
    applied = []

    for change in changes:
        category = change["type"]
        name = change["name"]
        change_type = change["change"]

        if change_type == "only_in_b":
            continue  # Don't remove items that exist only in target

        source_item = source_config.get(category, {}).get(name)
        if not source_item:
            continue

        await _apply_config_item(target_org_id, category, name, source_item)
        applied.append({"type": category, "name": name, "action": "applied"})

    return {"dry_run": False, "applied": applied}


async def _apply_config_item(org_id: ObjectId, category: str, name: str, item_data: dict):
    """Apply a single configuration item to an org."""
    if category == "entities":
        existing = await EntityDefinition.find_one({"name": name, "org_id": org_id})
        if existing:
            for key, value in item_data.items():
                if key not in ("name", "collection_name"):
                    setattr(existing, key, value)
            existing.updated_at = datetime.now(timezone.utc)
            existing.version += 1
            await existing.save()
        else:
            defn = EntityDefinition(org_id=org_id, **item_data)
            await defn.insert()

    elif category == "rules":
        item_data.pop("version", None)
        existing = await Rule.find_one({"name": name, "org_id": org_id})
        if existing:
            for key, value in item_data.items():
                if key not in ("name",):
                    setattr(existing, key, value)
            await existing.save()
        else:
            rule = Rule(org_id=org_id, created_by="deploy", **item_data)
            await rule.insert()

    elif category == "lookups":
        existing = await Lookup.find_one({"name": name, "org_id": org_id})
        if existing:
            await Lookup.get_motor_collection().update_one(
                {"_id": existing.id},
                {"$set": {"data": item_data.get("data", {})}},
            )
        else:
            lookup = Lookup(org_id=org_id, created_by="deploy", **item_data)
            await lookup.insert()

    elif category == "skills":
        item_data.pop("version", None)
        from kernel.skill.integrity import compute_content_hash

        existing = await Skill.find_one({"name": name, "org_id": org_id})
        if existing:
            existing.content = item_data.get("content", existing.content)
            existing.content_hash = compute_content_hash(existing.content)
            existing.version += 1
            existing.updated_at = datetime.now(timezone.utc)
            await existing.save()
        else:
            content = item_data.get("content", "")
            item_data["content_hash"] = compute_content_hash(content)
            skill = Skill(org_id=org_id, created_by="deploy", **item_data)
            await skill.insert()

    elif category in ("roles", "actors", "integrations"):
        from kernel.db import get_database

        db = get_database()
        coll_name = category
        existing = await db[coll_name].find_one({"name": name, "org_id": org_id})
        if existing:
            update_data = {k: v for k, v in item_data.items() if k not in ("name",)}
            update_data["updated_at"] = datetime.now(timezone.utc)
            await db[coll_name].update_one(
                {"_id": existing["_id"]},
                {"$set": update_data},
            )
        else:
            item_data["_id"] = ObjectId()
            item_data["org_id"] = org_id
            item_data["created_at"] = datetime.now(timezone.utc)
            item_data["updated_at"] = datetime.now(timezone.utc)
            item_data["version"] = 1
            await db[coll_name].insert_one(item_data)


def _dump_doc(doc, exclude: set = None) -> dict:
    """Dump a Beanie document to a JSON-safe dict, stripping excluded keys."""
    dumped = _serialize_bson(doc.model_dump(by_alias=True))
    if exclude:
        for key in exclude:
            dumped.pop(key, None)
    return dumped


def _serialize_bson(obj):
    """Recursively convert ObjectId and datetime to strings for JSON serialization."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize_bson(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_bson(v) for v in obj]
    return obj
