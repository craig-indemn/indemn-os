"""Rule evaluation engine.

Evaluates all active rules for an org + entity type + capability.
Returns match/no-match/veto result with full context for debugging.
Rule evaluation traces are stored as method_metadata in the changes collection.
"""

from kernel.observability.tracing import create_span
from kernel.rule.lookup import resolve_lookup_references
from kernel.rule.schema import Rule
from kernel.watch.evaluator import evaluate_condition


async def evaluate_rules(
    org_id, entity_type: str, capability: str, entity_data: dict
) -> dict:
    """Evaluate all active rules for this org + entity type + capability.
    Returns the evaluation result with full context for debugging."""

    with create_span("rule.evaluate", entity_type=entity_type, capability=capability):
        # Load active rules, ordered by priority (highest first)
        rules = (
            await Rule.find(
                {
                    "org_id": org_id,
                    "entity_type": entity_type,
                    "capability": capability,
                    "status": "active",
                }
            )
            .sort("-priority")
            .to_list()
        )

        if not rules:
            return {
                "matched": False,
                "vetoed": False,
                "reason": "no_rules_configured",
                "attempted_rules": [],
            }

        matched_rules = []
        veto_rules = []
        attempted = []

        for rule in rules:
            match = evaluate_condition(rule.conditions, entity_data)
            attempted.append(
                {
                    "name": rule.name or str(rule.id),
                    "matched": match,
                    "action": rule.action,
                    "priority": rule.priority,
                }
            )

            if match:
                if rule.action == "force_reasoning":
                    veto_rules.append(rule)
                else:
                    matched_rules.append(rule)

        # Veto overrides positive matches
        if veto_rules:
            veto = veto_rules[0]  # Highest priority veto
            return {
                "matched": True,
                "vetoed": True,
                "reason": "veto",
                "veto_reason": veto.forces_reasoning_reason or "Veto rule matched",
                "attempted_rules": attempted,
                "winning_veto": {
                    "name": veto.name,
                    "reason": veto.forces_reasoning_reason,
                },
            }

        if matched_rules:
            winner = matched_rules[0]  # Highest priority positive match
            # Resolve lookup references in the sets values
            resolved_sets = (
                await resolve_lookup_references(winner.sets, org_id, entity_data)
                if winner.sets
                else {}
            )

            return {
                "matched": True,
                "vetoed": False,
                "winning_rule": {
                    "name": winner.name or str(winner.id),
                    "sets": resolved_sets,
                },
                "attempted_rules": attempted,
            }

        return {
            "matched": False,
            "vetoed": False,
            "reason": "no_match",
            "attempted_rules": attempted,
        }
