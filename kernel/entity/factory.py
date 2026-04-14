"""Dynamic entity class creation from EntityDefinition.

Reads entity definitions (data in MongoDB) and creates Beanie Document
subclasses at runtime using Pydantic's create_model. This is the mechanism
that makes domain entities "data, not code."
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from bson import ObjectId
from pydantic import create_model

from kernel.entity.base import DomainBaseEntity
from kernel.entity.definition import EntityDefinition

TYPE_MAP = {
    "str": str,
    "int": int,
    "float": float,
    "decimal": Decimal,
    "bool": bool,
    "datetime": datetime,
    "date": date,
    "objectid": ObjectId,
    "list": list,
    "dict": dict,
}


def create_entity_class(definition: EntityDefinition) -> type[DomainBaseEntity]:
    """Create a Beanie Document subclass from an EntityDefinition."""

    # Build field definitions for create_model
    field_definitions = {}
    for field_name, field_def in definition.fields.items():
        python_type = TYPE_MAP.get(field_def.type, str)
        if field_def.enum_values:
            # Use str type with runtime validation (see enum validator below)
            python_type = str
        if not field_def.required:
            python_type = Optional[python_type]
        default = field_def.default if field_def.default is not None else (
            None if not field_def.required else ...
        )
        field_definitions[field_name] = (python_type, default)

    # Add flexible data field if configured
    if definition.flexible_data:
        field_definitions["data"] = (dict, {})

    # Create dynamic class — uses DomainBaseEntity (Pydantic + Motor, no Beanie lazy model)
    DynamicEntity = create_model(
        definition.name,
        __base__=DomainBaseEntity,
        **field_definitions,
    )

    # Add enum validators for fields with enum_values
    enum_fields = {
        fname: fdef.enum_values
        for fname, fdef in definition.fields.items()
        if fdef.enum_values
    }
    if enum_fields:
        original_init = DynamicEntity.__init__

        def _validating_init(self, **data):
            for fname, allowed in enum_fields.items():
                val = data.get(fname)
                if val is not None and val not in allowed:
                    raise ValueError(
                        f"Field '{fname}' must be one of {allowed}, got '{val}'"
                    )
            original_init(self, **data)

        DynamicEntity.__init__ = _validating_init

    # Store enum metadata for UI and skill generation
    DynamicEntity._enum_fields = enum_fields

    # Identify the state field from is_state_field flag in definition
    state_field_name = None
    for fname, fdef in definition.fields.items():
        if fdef.is_state_field:
            state_field_name = fname
            break
    DynamicEntity._state_field_name = state_field_name

    # Attach configuration
    DynamicEntity._state_machine = definition.state_machine
    DynamicEntity._computed_fields = (
        {k: v.model_dump() for k, v in definition.computed_fields.items()}
        if definition.computed_fields
        else None
    )
    DynamicEntity._activated_capabilities = definition.activated_capabilities or []
    DynamicEntity._flexible_data_config = definition.flexible_data
    DynamicEntity._is_kernel_entity = False

    # Set collection name for Motor operations (no Beanie Settings needed)
    DynamicEntity._collection_name = definition.collection_name

    return DynamicEntity
