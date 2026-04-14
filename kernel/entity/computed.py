"""Computed field evaluation.

Computed fields derive their value from another field via a mapping.
For example, ball_holder is computed from stage:
  {"received": "queue", "triaging": "gic", "processing": "gic", ...}

Called inside save_tracked() before the MongoDB write.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kernel.entity.base import BaseEntity


def evaluate_computed_fields(entity: "BaseEntity") -> dict[str, Any]:
    """Evaluate computed fields and return values that were set.
    Called inside save_tracked() before the MongoDB write."""
    computed_defs = entity._computed_fields
    if not computed_defs:
        return {}

    result = {}
    entity_data = entity.model_dump(by_alias=True)

    for field_name, defn in computed_defs.items():
        source_field = defn["source_field"] if isinstance(defn, dict) else defn.source_field
        mapping = defn["mapping"] if isinstance(defn, dict) else defn.mapping
        source_value = entity_data.get(source_field)
        if source_value is not None and str(source_value) in mapping:
            computed_value = mapping[str(source_value)]
            result[field_name] = computed_value
            setattr(entity, field_name, computed_value)

    return result
