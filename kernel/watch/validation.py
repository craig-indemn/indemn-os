"""Watch creation validation.

Watches can only reference fields on the changed entity (entity-local).
No cross-entity lookups during evaluation. This constraint keeps
watch evaluation fast and predictable.
"""

from kernel_entities.role import WatchDefinition


async def validate_watch(watch: WatchDefinition, org_id: str) -> list[str]:
    """Validate a watch before allowing it to be added to a role."""
    errors = []

    # Check that the entity type exists
    from kernel.db import ENTITY_REGISTRY

    if watch.entity_type not in ENTITY_REGISTRY:
        errors.append(f"Entity type '{watch.entity_type}' does not exist")
        return errors

    # Check that conditions only reference entity-local fields
    if watch.conditions:
        entity_fields = _get_entity_fields(watch.entity_type)
        referenced_fields = _extract_field_references(watch.conditions)
        for field in referenced_fields:
            # Only the first part (before any dot) must be an entity field
            root_field = field.split(".")[0]
            if root_field not in entity_fields and root_field not in ("status", "stage"):
                errors.append(
                    f"Condition references field '{field}' which is not on "
                    f"{watch.entity_type}. Watch conditions must be entity-local."
                )

    return errors


def _get_entity_fields(entity_type: str) -> set[str]:
    """Get all field names for an entity type (kernel or domain)."""
    from kernel.db import ENTITY_REGISTRY

    cls = ENTITY_REGISTRY.get(entity_type)
    if not cls:
        return set()
    return set(cls.model_fields.keys())


def _extract_field_references(condition: dict) -> set[str]:
    """Extract all field names referenced in a condition tree."""
    fields = set()
    if "all" in condition or "any" in condition:
        for sub in condition.get("all", condition.get("any", [])):
            fields.update(_extract_field_references(sub))
    elif "not" in condition:
        fields.update(_extract_field_references(condition["not"]))
    elif "field" in condition:
        fields.add(condition["field"])
    return fields
