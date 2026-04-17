"""Seed data loading from YAML files.

Loads entity definitions and skills from the seed/ directory into MongoDB.
Idempotent — skips items that already exist.
"""

import logging
from pathlib import Path

import yaml

from kernel.entity.definition import EntityDefinition
from kernel.skill.integrity import compute_content_hash
from kernel.skill.schema import Skill

logger = logging.getLogger(__name__)


async def load_seed_data(org_id, seed_dir: Path = Path("seed")):
    """Load seed files into entity definitions and skills for the given org."""

    from bson import ObjectId

    if isinstance(org_id, str):
        org_id = ObjectId(org_id)

    # Entity definitions
    entities_dir = seed_dir / "entities"
    if entities_dir.exists():
        for yaml_file in sorted(entities_dir.glob("*.yaml")):
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            existing = await EntityDefinition.find_one(
                {"name": data["name"], "org_id": org_id}
            )
            if not existing:
                defn = EntityDefinition(**data, org_id=org_id)
                await defn.insert()
                logger.info("Seeded entity definition: %s (org=%s)", data["name"], org_id)

    # Skills
    skills_dir = seed_dir / "skills"
    if skills_dir.exists():
        for md_file in sorted(skills_dir.glob("*.md")):
            content = md_file.read_text()
            name = md_file.stem
            existing = await Skill.find_one({"name": name, "org_id": org_id})
            if not existing:
                skill = Skill(
                    org_id=org_id,
                    name=name,
                    type="associate",
                    content=content,
                    content_hash=compute_content_hash(content),
                    status="active",
                )
                await skill.insert()
                logger.info("Seeded skill: %s (org=%s)", name, org_id)

    # Roles
    roles_dir = seed_dir / "roles"
    if roles_dir.exists():
        from kernel_entities.role import Role

        for yaml_file in sorted(roles_dir.glob("*.yaml")):
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            existing = await Role.find_one({"name": data["name"], "org_id": org_id})
            if not existing:
                role = Role(**data, org_id=org_id)
                await role.insert()
                logger.info("Seeded role: %s (org=%s)", data["name"], org_id)
