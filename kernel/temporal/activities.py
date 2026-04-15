"""Temporal activities — the functions workflows call.

Activities handle the actual work: claiming messages, loading context,
processing via associate skills, completing/failing messages, and bulk operations.
"""

import logging
import re

import httpx
import orjson
from bson import ObjectId
from temporalio import activity

from kernel.auth.jwt import create_access_token
from kernel.config import settings
from kernel.context import (
    current_actor_id,
    current_correlation_id,
    current_depth,
    current_org_id,
)
from kernel.db import ENTITY_REGISTRY
from kernel.message.mongodb_bus import MongoDBMessageBus
from kernel.message.schema import Message
from kernel.observability.tracing import create_span
from kernel.skill.integrity import verify_content_hash
from kernel.skill.schema import Skill
from kernel.watch.evaluator import evaluate_condition

logger = logging.getLogger(__name__)


class PermanentProcessingError(Exception):
    """Non-retryable processing error."""

    pass


class SkillTamperError(Exception):
    """Skill content hash mismatch."""

    pass


class BulkAbortError(Exception):
    """Raised in abort mode when any entity in a bulk batch fails."""

    pass


# --- Core message lifecycle activities ---


@activity.defn
async def claim_message(message_id: str, actor_id: str) -> bool:
    """Atomic claim via findOneAndUpdate. Returns False if already claimed."""
    bus = MongoDBMessageBus()
    msg = await bus.claim_by_id(ObjectId(message_id), ObjectId(actor_id))
    return msg is not None


@activity.defn
async def load_entity_context(message_id: str) -> dict:
    """Load the entity referenced by the message. Fresh from MongoDB."""
    message = await Message.get(ObjectId(message_id))
    if not message:
        return {"message": None, "entity": None}

    entity_cls = ENTITY_REGISTRY.get(message.entity_type)
    entity = None
    if entity_cls:
        entity = await entity_cls.get(message.entity_id)

    return {
        "message": message.model_dump(mode="json"),
        "entity": entity.model_dump(mode="json") if entity else None,
    }


@activity.defn
async def process_with_associate(message_id: str, associate_id: str, context: dict) -> dict:
    """The associate processes the message.

    Loads the associate's skills, determines execution mode,
    and runs the appropriate interpreter.
    """
    from kernel_entities.actor import Actor

    with create_span("associate.process", associate_id=associate_id):
        # Load associate configuration
        associate = await Actor.get(ObjectId(associate_id))
        if not associate or associate.status != "active":
            raise PermanentProcessingError(f"Associate {associate_id} not found or inactive")

        # Set auth context for this activity [G-64]
        current_org_id.set(associate.org_id)
        current_actor_id.set(str(associate.id))
        msg_data = context.get("message") or {}
        current_correlation_id.set(msg_data.get("correlation_id"))
        current_depth.set(msg_data.get("depth", 0))

        # Load and verify skills
        skills_content = await _load_skills(associate.skills or [])

        # Determine execution mode and run
        mode = associate.mode or "hybrid"

        if mode == "deterministic":
            result = await _execute_deterministic(associate, skills_content, context)
            # [G-56] Strict deterministic mode
            if result.get("needs_reasoning") and associate.strict_deterministic:
                raise PermanentProcessingError(
                    f"Associate {associate.name} is strict_deterministic but capability "
                    f"returned needs_reasoning: {result.get('reason')}"
                )
        elif mode == "reasoning":
            result = await _execute_reasoning(associate, skills_content, context)
        else:  # hybrid
            result = await _execute_hybrid(associate, skills_content, context)

        return result


@activity.defn
async def process_human_decision(message_id: str, decision: dict) -> dict:
    """Process a human's decision from the HumanReviewWorkflow."""
    message = await Message.get(ObjectId(message_id))
    if not message:
        return {"status": "message_not_found"}

    entity_cls = ENTITY_REGISTRY.get(message.entity_type)
    if not entity_cls:
        return {"status": "entity_type_not_found"}

    entity = await entity_cls.get(message.entity_id)
    if not entity:
        return {"status": "entity_not_found"}

    action = decision.get("action")  # approve, reject, escalate
    reason = decision.get("reason", "")

    if action == "approve":
        target = decision.get("target_state")
        if target and hasattr(entity, "_state_machine") and entity._state_machine:
            entity.transition_to(target, reason=reason)
            await entity.save_tracked(
                method="human_approve", method_metadata={"decision": decision}
            )
    elif action == "reject":
        target = decision.get("target_state")
        if target:
            entity.transition_to(target, reason=reason)
            await entity.save_tracked(
                method="human_reject", method_metadata={"decision": decision}
            )

    return {"status": action, "entity_id": str(entity.id)}


@activity.defn
async def complete_message(message_id: str, result: dict) -> None:
    """Move message from queue to log."""
    bus = MongoDBMessageBus()
    await bus.complete(ObjectId(message_id), result)


@activity.defn
async def fail_message(message_id: str, error: str) -> None:
    """Return message to queue or move to dead_letter."""
    bus = MongoDBMessageBus()
    await bus.fail(ObjectId(message_id), error)


# --- Bulk operation activities ---


@activity.defn
async def process_bulk_batch(spec_dict: dict, offset: int) -> dict:
    """Process one batch of a bulk operation within a MongoDB transaction."""
    from kernel.capability.registry import get_capability
    from kernel.entity.save import VersionConflictError
    from kernel.entity.state_machine import StateMachineError
    from kernel.temporal.workflows import BulkOperationSpec

    spec = BulkOperationSpec(**spec_dict)

    entity_cls = ENTITY_REGISTRY.get(spec.entity_type)
    if not entity_cls:
        raise PermanentProcessingError(f"Entity type {spec.entity_type} not found")

    # Query entities
    if spec.filter_query:
        entities = (
            await entity_cls.find_scoped(spec.filter_query)
            .skip(offset)
            .limit(spec.batch_size)
            .to_list()
        )
    elif spec.source_data:
        entities = spec.source_data[offset : offset + spec.batch_size]
    else:
        return {"done": True, "batch_processed": 0}

    if not entities:
        return {"done": True, "batch_processed": 0, "total_count": offset}

    errors = []
    batch_processed = 0

    # Process batch within a MongoDB transaction
    from kernel.db import get_client

    mongo_client = get_client()
    async with await mongo_client.start_session() as session:
        async with session.start_transaction():
            for entity in entities:
                try:
                    if spec.operation == "transition":
                        entity.transition_to(spec.target_state)
                        await entity.save_tracked(
                            method="bulk_transition",
                            method_metadata={
                                "bulk_operation_id": activity.info().workflow_id
                            },
                        )
                    elif spec.operation == "method":
                        cap_fn = get_capability(spec.method_name)
                        result = await cap_fn(entity, {}, entity.org_id)
                        if not result.get("needs_reasoning"):
                            for field, value in result.get("result", {}).items():
                                setattr(entity, field, value)
                            await entity.save_tracked(
                                method=spec.method_name,
                                method_metadata={
                                    "rule_evaluation": result.get("rule_evaluation"),
                                    "bulk_operation_id": activity.info().workflow_id,
                                },
                            )
                    elif spec.operation == "update":
                        if spec.sets:
                            # Silent update — bypasses save_tracked() to avoid event emission
                            await entity.get_motor_collection().update_one(
                                {"_id": entity.id},
                                {"$set": spec.sets, "$inc": {"version": 1}},
                                session=session,
                            )
                    elif spec.operation == "create":
                        new_entity = entity_cls(org_id=current_org_id.get(), **entity)
                        await new_entity.save_tracked(method="bulk_create")
                    elif spec.operation == "delete":
                        await entity.get_motor_collection().delete_one(
                            {"_id": entity.id}, session=session
                        )

                    batch_processed += 1

                except VersionConflictError:
                    # Transient — propagate for Temporal retry
                    raise
                except (StateMachineError, ValueError, PermissionError) as e:
                    if spec.failure_mode == "abort":
                        raise BulkAbortError(str(e))
                    errors.append({
                        "entity_id": str(entity.id) if hasattr(entity, "id") else str(entity),
                        "error_type": type(e).__name__,
                        "message": str(e),
                    })

                activity.heartbeat(f"batch progress: {batch_processed}")

    total_count = offset + len(entities)
    done = len(entities) < spec.batch_size

    return {
        "done": done,
        "batch_processed": batch_processed,
        "total_count": total_count,
        "errors": errors,
    }


@activity.defn
async def preview_bulk_operation(spec_dict: dict) -> dict:
    """Dry-run preview — count and sample affected entities. [G-81]"""
    from kernel.temporal.workflows import BulkOperationSpec

    spec = BulkOperationSpec(**spec_dict)

    entity_cls = ENTITY_REGISTRY.get(spec.entity_type)
    if not entity_cls:
        return {"count": 0, "error": f"Entity type {spec.entity_type} not found"}

    if spec.filter_query:
        count = await entity_cls.find_scoped(spec.filter_query).count()
        sample = await entity_cls.find_scoped(spec.filter_query).limit(5).to_list()
        return {
            "count": count,
            "sample": [e.model_dump(mode="json") for e in sample],
            "operation": spec.operation,
            "dry_run": True,
        }
    return {"count": 0}


# --- Skill loading and execution ---


async def _load_skills(skill_names: list[str]) -> str:
    """Load and concatenate skill content with integrity verification."""
    parts = []
    for name in skill_names:
        skill = await Skill.find_one({"name": name, "status": "active"})
        if not skill:
            continue
        if not verify_content_hash(skill.content, skill.content_hash):
            raise SkillTamperError(f"Skill '{name}' failed integrity check")
        parts.append(skill.content)
    return "\n\n---\n\n".join(parts)


async def _execute_deterministic(associate, skills: str, context: dict) -> dict:
    """Execute skill deterministically — no LLM.

    The deterministic interpreter [G-25]:
    - Reads markdown skill content
    - Identifies lines that are CLI commands (lines starting with `indemn` or backtick-wrapped)
    - Identifies simple conditions (lines starting with "If" or "When")
    - Executes commands sequentially via HTTP API calls
    - Evaluates conditions against entity data
    - Returns the result of the last command

    This is a simple line-by-line interpreter, NOT a full DSL engine.
    Complex orchestration that can't be expressed as sequential commands
    with simple conditions should use reasoning mode instead.
    """
    entity_data = context.get("entity", {}) or {}
    results = []

    steps = _parse_skill_steps(skills)

    for step in steps:
        if step["type"] == "command":
            result = await _execute_command_via_api(step["command"], entity_data, associate)
            results.append(result)
            if isinstance(result, dict):
                entity_data.update(result)

        elif step["type"] == "condition":
            if not evaluate_condition(step["condition"], entity_data):
                if step.get("on_false") == "stop":
                    break
                # skip = continue to next step

        elif step["type"] == "auto_command":
            result = await _execute_command_via_api(step["command"], entity_data, associate)
            if isinstance(result, dict) and result.get("needs_reasoning"):
                return result  # Bubble up to caller
            results.append(result)

    return {"status": "completed", "results": results}


async def _execute_reasoning(associate, skills: str, context: dict) -> dict:
    """Execute skill using LLM reasoning.

    The LLM reads the skill, analyzes the context, and decides which
    CLI commands to execute via the API.
    """
    import anthropic

    llm_config = associate.llm_config or {}
    model = llm_config.get("model", "claude-sonnet-4-6")
    temperature = llm_config.get("temperature", 0.2)

    client = anthropic.AsyncAnthropic()

    system_prompt = (
        f"You are an associate executing the following skill:\n\n{skills}\n\n"
        f"You have access to the Indemn OS CLI via the execute_command tool. "
        f"Every command goes through the API with your permissions. "
        f"Execute the steps in your skill against the provided context."
    )

    entity_context = context.get("entity", {}) or {}
    message_context = context.get("message", {}) or {}

    messages = [
        {
            "role": "user",
            "content": (
                f"Process this work item:\n\n"
                f"Entity: {message_context.get('entity_type')} "
                f"{message_context.get('entity_id')}\n"
                f"Event: {message_context.get('event_type')}\n"
                f"Data: {_safe_serialize(entity_context)}"
            ),
        },
    ]

    max_iterations = 20
    results = []

    for i in range(max_iterations):
        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            temperature=temperature,
            system=system_prompt,
            messages=messages,
            tools=[
                {
                    "name": "execute_command",
                    "description": "Execute an indemn CLI command via the API",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": (
                                    "The full indemn CLI command "
                                    "(e.g., 'indemn email classify EMAIL-001 --auto')"
                                ),
                            },
                        },
                        "required": ["command"],
                    },
                }
            ],
        )

        if response.stop_reason == "tool_use":
            for content_block in response.content:
                if content_block.type == "tool_use":
                    command = content_block.input["command"]
                    try:
                        result = await _execute_command_via_api(
                            command, entity_context, associate
                        )
                        results.append({"command": command, "result": result})
                        messages.append({"role": "assistant", "content": response.content})
                        messages.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": content_block.id,
                                        "content": _safe_serialize(result),
                                    }
                                ],
                            }
                        )
                    except Exception as e:
                        messages.append({"role": "assistant", "content": response.content})
                        messages.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": content_block.id,
                                        "content": f"Error: {str(e)}",
                                        "is_error": True,
                                    }
                                ],
                            }
                        )

        elif response.stop_reason == "end_turn":
            final_text = "".join(b.text for b in response.content if hasattr(b, "text"))
            return {
                "status": "completed",
                "results": results,
                "summary": final_text,
            }

        # Heartbeat for long-running processing
        activity.heartbeat(f"iteration {i + 1}")

    return {"status": "completed", "results": results, "warning": "max_iterations_reached"}


async def _execute_hybrid(associate, skills: str, context: dict) -> dict:
    """Try deterministic first. If any step returns needs_reasoning,
    fall back to LLM for the remainder."""
    result = await _execute_deterministic(associate, skills, context)
    if result.get("needs_reasoning"):
        return await _execute_reasoning(
            associate,
            skills,
            {**context, "deterministic_result": result},
        )
    return result


# --- Skill parsing ---


def _parse_skill_steps(skill_content: str) -> list[dict]:
    """Parse markdown skill into executable steps.

    Recognizes:
    - Lines containing `indemn ...` (backtick-wrapped) as commands
    - Lines starting with 'If' or 'When' as conditions
    """
    steps = []
    for line in skill_content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue

        # Extract command from backticks
        cmd_match = re.search(r"`(indemn [^`]+)`", line)
        if cmd_match:
            cmd = cmd_match.group(1)
            if "--auto" in cmd:
                steps.append({"type": "auto_command", "command": cmd})
            else:
                steps.append({"type": "command", "command": cmd})
            continue

        # Simple condition
        if line.lower().startswith(("if ", "when ")):
            steps.append({
                "type": "condition",
                "condition": _parse_simple_condition(line),
                "on_false": "skip",
            })

    return steps


def _parse_simple_condition(line: str) -> dict:
    """Parse a simple condition from a skill line.

    E.g., 'If needs_reasoning is true' → {"field": "needs_reasoning", "op": "equals", "value": true}
    """
    match = re.search(r"(\w+)\s+(is|equals?|=)\s+(\w+)", line, re.IGNORECASE)
    if match:
        field, _, value = match.groups()
        if value.lower() == "true":
            value = True
        elif value.lower() == "false":
            value = False
        return {"field": field, "op": "equals", "value": value}
    return {"field": "_always", "op": "equals", "value": True}  # Fallback: always true


# --- Helpers ---


async def _get_roles(actor) -> list:
    """Load role entities for an actor."""
    from kernel_entities.role import Role

    return await Role.find({"_id": {"$in": actor.role_ids}}).to_list()


# --- API call translation ---


async def _execute_command_via_api(command: str, entity_data: dict, associate) -> dict:
    """Execute an indemn CLI command by translating it to an API call. [G-21]"""
    parts = command.strip().split()
    if parts[0] != "indemn":
        raise PermanentProcessingError(f"Invalid command: {command}")

    entity_type = parts[1]  # "email"
    operation = parts[2] if len(parts) > 2 else "list"
    args = parts[3:] if len(parts) > 3 else []

    # Get a service token for the associate
    roles = await _get_roles(associate)
    role_names = [r.name for r in roles]
    token, _ = create_access_token(str(associate.id), str(associate.org_id), role_names)

    async with httpx.AsyncClient(base_url=settings.api_url) as client:
        entity_id = args[0] if args else None
        auto = "--auto" in args
        url = f"/api/{entity_type}s/{entity_id}/{operation}"
        params = {"auto": "true"} if auto else {}
        data = _extract_data_from_args(args)

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Correlation-ID": current_correlation_id.get() or "",
            "X-Cascade-Depth": str(current_depth.get()),
        }

        response = await client.post(
            url,
            json=data,
            params=params,
            headers=headers,
            timeout=60.0,
        )

        if response.status_code >= 400:
            raise PermanentProcessingError(
                f"API call failed: {response.status_code} {response.text}"
            )

        return response.json()


def _extract_data_from_args(args: list[str]) -> dict:
    """Extract --key value pairs from CLI args into a dict."""
    data = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and args[i] != "--auto":
            key = args[i][2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                data[key] = args[i + 1]
                i += 2
            else:
                data[key] = True
                i += 1
        else:
            i += 1
    return data


def _safe_serialize(obj) -> str:
    """Serialize to JSON string, handling ObjectIds and datetimes."""
    return orjson.dumps(obj, default=str).decode()
