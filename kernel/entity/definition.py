"""Entity definition schema — stored in MongoDB, read at startup.

Domain entity types are defined as data (per-org). The entity framework
creates Beanie Document subclasses dynamically from these definitions.
Kernel entities are NOT defined this way — they are Python classes.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from beanie import Document
from bson import ObjectId
from pydantic import BaseModel, Field


class FieldDefinition(BaseModel):
    """Definition of a single field on a domain entity."""

    type: str  # str, int, float, decimal, bool, datetime, date, objectid, list, dict
    required: bool = False
    default: Optional[Any] = None
    unique: bool = False
    indexed: bool = False
    enum_values: Optional[list[str]] = None
    description: Optional[str] = None
    is_state_field: bool = False  # True for the field controlled by the state machine
    is_relationship: bool = False  # True for ObjectId fields that reference other entities
    relationship_target: Optional[str] = None  # Entity name this relationship points to


class ComputedFieldDef(BaseModel):
    """A field whose value is derived from another field via mapping."""

    source_field: str
    mapping: dict[str, str]  # source_value → computed_value


class IndexDef(BaseModel):
    """A compound index definition."""

    fields: list[tuple[str, int]]  # [("org_id", 1), ("status", 1)]
    unique: bool = False


class CapabilityActivation(BaseModel):
    """A kernel capability activated on this entity type."""

    capability: str  # "auto_classify", "fuzzy_search", "stale_check"
    config: dict  # Capability-specific: evaluates, sets_field, threshold, etc.


class FlexibleDataSchema(BaseModel):
    """Configuration for the flexible data section.

    schema_source="self" means the schema is embedded here in the `schema` field.
    schema_source="product_id" means load the Product entity, read its schema_field.
    """

    schema_source: str  # "self" or a relationship field name (e.g., "product_id")
    schema_field: str  # Field on the source entity holding the JSON schema
    data_schema: Optional[dict] = None  # Embedded JSON Schema (used when schema_source="self")


class EntityDefinition(Document):
    """A domain entity type definition. The entity framework reads these
    at startup and creates Beanie Document subclasses dynamically.

    Per-org: different organizations can define different entity types.
    Seed templates provide starting points; orgs clone and customize.
    """

    name: str  # "Submission", "Email"
    collection_name: str  # "submissions", "emails"
    description: Optional[str] = None

    fields: dict[str, FieldDefinition]
    state_machine: Optional[dict[str, list[str]]] = None
    computed_fields: Optional[dict[str, ComputedFieldDef]] = None
    flexible_data: Optional[FlexibleDataSchema] = None
    indexes: list[IndexDef] = Field(default_factory=list)
    activated_capabilities: list[CapabilityActivation] = Field(default_factory=list)

    org_id: ObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: Optional[str] = None
    version: int = 1

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        name = "entity_definitions"
        indexes = [
            [("org_id", 1), ("name", 1)],  # Unique per org
        ]
