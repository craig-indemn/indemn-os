"""Flexible data validation.

Entities can have a 'data' dict field for product-specific or config-specific
fields that vary by context. The schema for this data is resolved from either
the entity's own definition or a related entity (e.g., a Product's form_schema).

Validation uses JSON Schema.
"""

from typing import TYPE_CHECKING

import jsonschema

from kernel.entity.definition import EntityDefinition, FlexibleDataSchema

if TYPE_CHECKING:
    from kernel.entity.base import BaseEntity


async def validate_flexible_data(entity: "BaseEntity", data: dict) -> list[str]:
    """Validate the entity's 'data' field against its configured schema.
    Returns list of validation errors (empty = valid)."""
    config = getattr(entity, "_flexible_data_config", None)
    if not config or not data:
        return []

    # Resolve the schema
    schema = await _resolve_schema(entity, config)
    if not schema:
        return []  # No schema configured — all data accepted

    # Validate using JSON Schema
    errors = []
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        errors.append(str(e.message))
    except jsonschema.SchemaError as e:
        errors.append(f"Invalid schema: {e.message}")

    return errors


async def _resolve_schema(entity: "BaseEntity", config: FlexibleDataSchema) -> dict:
    """Resolve the JSON Schema for flexible data validation."""
    if config.schema_source == "self":
        # Schema is on this entity's definition
        defn = await EntityDefinition.find_one({"name": type(entity).__name__})
        if defn and defn.flexible_data:
            return getattr(defn.flexible_data, "schema", None)
        return None
    else:
        # Schema is on a related entity (e.g., product_id → Product.form_schema)
        related_id = getattr(entity, config.schema_source, None)
        if not related_id:
            return None
        target_entity_cls = await _resolve_target_entity(entity, config.schema_source)
        if not target_entity_cls:
            return None
        related = await target_entity_cls.get(related_id)
        if not related:
            return None
        return getattr(related, config.schema_field, None)


async def _resolve_target_entity(entity: "BaseEntity", field_name: str):
    """Resolve the entity class for a relationship field."""
    from kernel.db import ENTITY_REGISTRY

    defn = await EntityDefinition.find_one({"name": type(entity).__name__})
    if defn and field_name in defn.fields:
        target = defn.fields[field_name].relationship_target
        if target:
            return ENTITY_REGISTRY.get(target)
    return None
