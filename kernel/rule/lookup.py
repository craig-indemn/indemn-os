"""Lookup tables — mapping tables separate from rules.

Prevents rule explosion. Instead of writing dozens of rules to map individual
codes to categories, a single lookup table handles all the mappings.
Bulk-importable from CSV, maintained by non-technical users.
"""

from datetime import datetime, timezone
from typing import Optional

from beanie import Document
from bson import ObjectId
from pydantic import Field


class Lookup(Document):
    """Mapping table — key→value data."""

    org_id: ObjectId
    name: str
    data: dict  # key → value
    description: Optional[str] = None
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        name = "lookups"
        indexes = [[("org_id", 1), ("name", 1)]]


async def resolve_lookup_references(
    sets: dict, org_id: str, entity_data: dict
) -> dict:
    """Resolve lookup references in rule action values.

    Example: {"lob": {"lookup": "usli-prefix-lob", "from_field": "quote_prefix"}}
    → resolves the lookup by reading the entity's quote_prefix field value
    and looking it up in the usli-prefix-lob table.
    """
    resolved = {}
    for field_name, value in sets.items():
        if isinstance(value, dict) and "lookup" in value:
            lookup_name = value["lookup"]
            source_field = value.get("from_field")
            source_value = entity_data.get(source_field) if source_field else None

            lookup = await Lookup.find_one({"name": lookup_name, "org_id": org_id})
            if lookup and source_value and str(source_value) in lookup.data:
                resolved[field_name] = lookup.data[str(source_value)]
            else:
                resolved[field_name] = None  # Lookup miss
        else:
            resolved[field_name] = value
    return resolved
