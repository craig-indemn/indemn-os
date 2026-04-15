"""stale_check — kernel capability for time-based staleness detection.

Evaluates conditions from the capability config against entity data.
If conditions match, sets the configured field to the configured value.
Used for overdue detection, stale submission flagging, etc.

Unlike auto_classify which goes through the rules engine, stale_check
evaluates its conditions directly from the capability activation config.
"""

from kernel.capability.registry import register_capability
from kernel.observability.tracing import create_span
from kernel.watch.evaluator import evaluate_condition


async def stale_check(entity, config: dict, org_id) -> dict:
    """Check if an entity meets staleness conditions.

    Config format:
        {
            "when": {"all": [{"field": "due_date", "op": "older_than", "value": "0d"}, ...]},
            "sets_field": "is_overdue",
            "sets_value": true
        }

    Returns:
        - needs_reasoning: always False (deterministic capability)
        - result: {field: value} if conditions match, empty dict otherwise
        - matched: whether conditions were met
    """
    with create_span("capability.stale_check", entity_type=type(entity).__name__):
        conditions = config.get("when", {})
        sets_field = config.get("sets_field")
        sets_value = config.get("sets_value", True)

        if not conditions or not sets_field:
            return {
                "needs_reasoning": False,
                "result": {},
                "matched": False,
                "reason": "missing_config",
            }

        entity_data = entity.model_dump(by_alias=True)
        matched = evaluate_condition(conditions, entity_data)

        if matched:
            return {
                "needs_reasoning": False,
                "result": {sets_field: sets_value},
                "matched": True,
            }
        else:
            return {
                "needs_reasoning": False,
                "result": {},
                "matched": False,
                "reason": "conditions_not_met",
            }


register_capability("stale_check", stale_check)
