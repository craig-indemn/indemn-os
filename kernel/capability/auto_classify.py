"""auto_classify — the first kernel capability.

Tries deterministic classification via rules. Returns the result or needs_reasoning.
This is the --auto pattern: rules first, LLM fallback if no match.
"""

from kernel.capability.registry import register_capability
from kernel.observability.tracing import create_span
from kernel.rule.engine import evaluate_rules


async def auto_classify(entity, config: dict, org_id) -> dict:
    """Try deterministic classification via rules. Return result or needs_reasoning."""
    with create_span("capability.auto_classify", entity_type=type(entity).__name__):
        result = await evaluate_rules(
            org_id=org_id,
            entity_type=type(entity).__name__,
            capability="auto_classify",
            entity_data=entity.model_dump(by_alias=True),
        )

        if result["matched"] and not result["vetoed"]:
            # Deterministic match
            return {
                "needs_reasoning": False,
                "result": result["winning_rule"]["sets"],
                "rule_evaluation": result,
            }
        else:
            return {
                "needs_reasoning": True,
                "reason": result.get("reason", "no_match"),
                "veto_reason": result.get("veto_reason"),
                "attempted_rules": result.get("attempted_rules", []),
                "rule_evaluation": result,
            }


register_capability("auto_classify", auto_classify)
