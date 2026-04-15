"""Rule creation validation.

Validates: fields exist in entity schema, state machine fields can't be set via rules,
heuristic overlap detection with existing rules.
"""

from kernel.rule.schema import Rule


async def validate_rule(rule: Rule, actor_roles: list[str] = None) -> list[str]:
    """Validate a rule before creation. Returns list of errors.

    actor_roles: list of role names for the creating actor (for future RBAC validation).
    """
    errors = []

    # 1. Fields in 'sets' must exist in entity schema
    from kernel.db import ENTITY_REGISTRY
    from kernel.entity.definition import EntityDefinition

    entity_cls = ENTITY_REGISTRY.get(rule.entity_type)
    if entity_cls and rule.sets:
        entity_fields = set(entity_cls.model_fields.keys())
        # For dynamic entities, also check the definition
        defn = await EntityDefinition.find_one({"name": rule.entity_type})
        if defn:
            entity_fields.update(defn.fields.keys())

        for field_name in rule.sets.keys():
            if isinstance(rule.sets[field_name], dict) and "lookup" in rule.sets[field_name]:
                continue  # Lookup reference — validated separately
            if field_name not in entity_fields:
                errors.append(f"Field '{field_name}' does not exist on {rule.entity_type}")

    # 2. State machine fields cannot be set via rules
    state_fields = {"status", "stage"}
    if rule.sets:
        for field_name in rule.sets.keys():
            if field_name in state_fields:
                errors.append(
                    f"Cannot set '{field_name}' via rule. "
                    f"State transitions must go through transition_to()."
                )

    # 3. Check for overlapping rules (warning, not error)
    existing = await Rule.find(
        {
            "org_id": rule.org_id,
            "entity_type": rule.entity_type,
            "capability": rule.capability,
            "status": "active",
        }
    ).to_list()
    for existing_rule in existing:
        if _conditions_may_overlap(rule.conditions, existing_rule.conditions):
            errors.append(
                f"WARNING: May overlap with rule '{existing_rule.name}'. "
                f"Use --force to create anyway."
            )

    return errors


def _conditions_may_overlap(cond1: dict, cond2: dict) -> bool:
    """Heuristic overlap detection. Not exhaustive."""
    fields1 = _extract_fields(cond1)
    fields2 = _extract_fields(cond2)
    return bool(fields1 & fields2)


def _extract_fields(condition: dict) -> set:
    refs = set()
    if "all" in condition or "any" in condition:
        for sub in condition.get("all", condition.get("any", [])):
            refs.update(_extract_fields(sub))
    elif "field" in condition:
        refs.add(condition["field"])
    return refs
