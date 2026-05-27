"""Eval-time check expression evaluator — ONE engine per D-L.

Runs POST-CLAIM (outside save_tracked transactions). Used by code Evaluators
in P4's code_executor. Reuses logical composition + leaf-op evaluators from
`kernel/watch/evaluator.py` via import — that file is NOT modified, preserving
its entity-local + microseconds + no-I/O constraint.

Path types supported (per D-C grammar + Group E extensions, 2026-05-26):

    trace.<field>                            # top-level Trace field
    trace.messages[N].<field>                # specific message in array
    trace.messages[*].<field>                # iterate all messages
    trace.messages[?{predicate}].<field>     # filter, then access
    trace.tool_call_summary                  # derived: per-tool-call summary
    trace.transition_reason                  # derived: from final transition CLI
    entity:<EntityType>:<id>.<field>         # load entity by id, read field
    entity:<EntityType>:<id>.<nested>.<f>    # dot-traverse after load
    changes:correlation_id=<cid>.<field>     # query Changes collection
    changes:entity_id=<id>.field=<f>.values  # historical field values
    example.reference_outputs.<key>          # offline eval Example reference
    example.inputs.<key>                     # offline eval Example input
    constellation.created_in_this_run.<EntityType>   # entities by correlation_id
    constellation.created_in_this_run.entity_counts  # dict of {EntityType: count}
    constellation.created_in_this_run.detail         # list of all created entity records
    constellation.company.<EntityType>       # entities linked to Run's Company

Template substitution (per Group E E2/E3/E4):

    {trace.entity_id}                                  # bare scalar
    entity:Touchpoint:{trace.entity_id}.company        # id-slot substitution
    {entity:Touchpoint:{trace.entity_id}.company}      # nested entity-load substitution
    entity:{trace.entity_type}:{trace.entity_id}.status  # entity-type-slot (Group E E3)
    {constellation.created_in_this_run.Deal[0]._id}    # subscript-then-field (Group E E2)
    null as value in equality ops                      # Group E E4

Operator vocabulary (per P3 "Pinned operator vocabulary", LOCKED 2026-05-25):

  Leaf ops (reused from kernel/watch/evaluator.py._OPERATORS):
    equals, not_equals, contains, not_contains, starts_with, ends_with,
    gt, gte, lt, lte, in, not_in, matches, exists, older_than, within

  Aggregation ops (NEW in this module):
    count (dual mode per Group E E1)
    any_matches_equals, any_matches_contains, any_matches_regex
    all_match_equals, all_match_contains, all_equal (alias for all_match_equals)
    none_match_equals, none_match_contains, none_match_regex
    first_call_matching_regex_before_first_create
"""

import re
from typing import Any, Optional

from bson import ObjectId

from kernel.watch.evaluator import _OPERATORS as _WATCH_LEAF_OPS

# --- Op vocabulary ---

LEAF_OPS = frozenset(_WATCH_LEAF_OPS.keys())

AGGREGATION_OPS = frozenset({
    "count",
    "any_matches_equals",
    "any_matches_contains",
    "any_matches_regex",
    "all_match_equals",
    "all_match_contains",
    "all_equal",  # alias for all_match_equals
    "none_match_equals",
    "none_match_contains",
    "none_match_regex",
    "first_call_matching_regex_before_first_create",
})

# Matches the innermost {...} placeholder — content has no nested braces.
# Placeholder content MUST contain `.` or `:` to be considered a path template;
# this disambiguates from regex quantifier syntax like `{24}` or `{1,3}` which
# DOES appear inside `value` fields that are regex patterns (e.g., IE-2's
# `[a-f0-9]{24}`). Regex quantifiers are left literal.
_INNERMOST_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def _is_template_placeholder(inner: str) -> bool:
    """A placeholder must contain `.` or `:` (path-expression sigil).

    Excludes:
    - Regex quantifiers like `{24}` (digits only) and `{1,3}` (digits + comma).
    - Predicate syntax inside `[?{...}]` (contains a `"` or `'` for the value).
    """
    if '"' in inner or "'" in inner:
        return False  # Predicate syntax, not a path placeholder
    return ("." in inner) or (":" in inner)

# Sentinel for "field not present" vs "field present and None".
_MISSING = object()


# --- Top-level entry point ---


async def evaluate_check(
    check_expression: dict,
    trace: dict,
    example: Optional[dict] = None,
    experiment: Optional[dict] = None,
) -> Any:
    """Evaluate a code Evaluator's check expression.

    Returns bool for binary checks, int for top-level count (continuous scoring
    per D-H), or float for other continuous shapes. The choice is per-Evaluator
    via the shape of the expression — there is no global mode toggle.
    """
    context = {"trace": trace, "example": example, "experiment": experiment}
    return await _evaluate(check_expression, context)


# --- Recursive composition + dispatch ---


async def _evaluate(expr: dict, context: dict) -> Any:
    """Recursive expression evaluation. Composition first, then leaf/aggregation."""
    if "all" in expr:
        for child in expr["all"]:
            if not await _evaluate(child, context):
                return False
        return True
    if "any" in expr:
        for child in expr["any"]:
            if await _evaluate(child, context):
                return True
        return False
    if "not" in expr:
        return not await _evaluate(expr["not"], context)

    op = expr.get("op")
    if op is None:
        raise ValueError(f"Expression missing 'op' field: {expr}")
    field = expr.get("field")
    if field is None:
        raise ValueError(f"Expression missing 'field' field: {expr}")

    # Substitute placeholders in the field path itself (Group E E3 entity-type slot etc.).
    field = await substitute_template(field, context)

    # Ordering op takes two regex args (NOT value).
    if op == "first_call_matching_regex_before_first_create":
        resolved = await resolve_path(field, context)
        regex_call = await substitute_template(expr["regex_call"], context)
        regex_target = await substitute_template(expr["regex_target"], context)
        return _first_call_before_first_create(resolved, regex_call, regex_target)

    # Resolve the field path to a value.
    resolved = await resolve_path(field, context)

    # Substitute placeholders in value (Group E E2/E4 — subscript-then-field, null-as-target).
    expected = expr.get("value", _MISSING)
    if expected is not _MISSING and expected is not None:
        expected = await substitute_template(expected, context)

    # Aggregation ops operate on the resolved list.
    if op in AGGREGATION_OPS:
        return _apply_aggregation(op, resolved, expected, expr)

    # Leaf comparison op — delegate to the watch evaluator's _OPERATORS dict.
    if op in LEAF_OPS:
        resolved_norm = _normalize_for_comparison(resolved)
        expected_norm = _normalize_for_comparison(expected if expected is not _MISSING else None)
        return _WATCH_LEAF_OPS[op](resolved_norm, expected_norm)

    raise ValueError(f"Unknown operator: {op}")


# --- Template substitution (Group E E2/E3/E4) ---


async def substitute_template(value: Any, context: dict) -> Any:
    """Resolve `{placeholder}` substitutions in a string value.

    Per D-C grammar:
    - Single-pass, innermost-first (regex `\\{([^{}]+)\\}` matches no nested braces).
    - If the entire input is a single placeholder, returns the resolved scalar
      (not its string repr) — supports null-as-target (Group E E4).
    - If mixed text + placeholders, resolves each to its string form and concatenates.
    - Resolved value at each substitution position must be string-serializable scalar;
      a list/dict at a position that expects scalar raises ValueError (no coercion,
      per Group D++ no-fallbacks).
    """
    if not isinstance(value, str):
        return value
    if "{" not in value:
        return value

    # Track an offset so we skip regex-quantifier `{N}` etc. (not real placeholders).
    search_from = 0
    while True:
        match = _INNERMOST_PLACEHOLDER.search(value, search_from)
        if match is None:
            return value
        inner = match.group(1)
        if not _is_template_placeholder(inner):
            # Not a path template — skip past this match and keep looking.
            search_from = match.end()
            continue
        # The inner expression is itself a path; resolve it.
        resolved = await resolve_path(inner, context)
        # If the whole input is JUST this placeholder, return raw resolved scalar
        # (allows null-as-target per Group E E4 + ObjectId/int passthrough).
        if match.group(0) == value:
            return resolved
        # Otherwise it's a fragment — must be string-serializable.
        if isinstance(resolved, (dict, list)):
            raise ValueError(
                f"Template substitution position requires scalar, got "
                f"{type(resolved).__name__} for placeholder {{{inner}}} in {value!r}"
            )
        value = value[: match.start()] + str(_to_str(resolved)) + value[match.end() :]


# --- Path resolution dispatch ---


async def resolve_path(path: str, context: dict) -> Any:
    """Resolve a slice path string to a value. Dispatches by prefix.

    Applies template substitution to the path first (so a path like
    `entity:{trace.entity_type}:{trace.entity_id}.status` is supported when
    called directly, not just via _evaluate's pre-substitution).
    """
    # Substitute any placeholders in the path (no-op fast-return if none).
    if isinstance(path, str) and "{" in path:
        substituted = await substitute_template(path, context)
        # Path substitution may resolve to a non-string scalar in pathological cases;
        # only proceed with str result. If a list/dict came back, downstream parsing fails clearly.
        if not isinstance(substituted, str):
            return substituted
        path = substituted
    # Bare-token paths within {…} (e.g., "trace.entity_id" inside `{trace.entity_id}`)
    # still hit this dispatcher because they have the same prefix structure.
    if path.startswith("trace."):
        return resolve_trace_path(path, context["trace"])
    if path == "trace":
        return context["trace"]
    if path.startswith("entity:"):
        return await resolve_entity_path(path, context)
    if path.startswith("changes:"):
        return await resolve_changes_path(path, context)
    if path.startswith("example."):
        if context.get("example") is None:
            return None
        return _resolve_dotted(context["example"], path[len("example.") :])
    if path.startswith("constellation."):
        return await resolve_constellation_path(path, context)
    raise ValueError(f"Unknown path prefix: {path!r}")


# --- Trace paths ---


def resolve_trace_path(path: str, trace: dict) -> Any:
    """Handle trace.* paths including derived fields."""
    suffix = path[len("trace.") :]
    if suffix == "tool_call_summary":
        return _derive_tool_call_summary(trace)
    if suffix == "tool_call_commands":
        return _derive_tool_call_commands(trace)
    if suffix == "transition_reason":
        return _derive_transition_reason(trace)
    return _resolve_dotted(trace, suffix)


# Shell-operator splitter for chained commands. The IE skill (and most associate
# skills) chain CLI invocations with `&&` to save turns. Each chained invocation
# is a separate LOGICAL command for evaluation purposes — `any_matches_regex` on
# anchored patterns + ordering ops need the SPLIT view, not the raw string.
# Splits on `&&`, `||`, and `;` (the three POSIX command separators that imply
# sequential or alternative invocation). Pipe `|` is NOT split — pipes flow output
# between commands rather than separating logical invocations.
_SHELL_OPERATOR = re.compile(r"\s*(?:&&|\|\||;)\s*")


def _split_chained_command(command: str) -> list[str]:
    """Split a single args.command string on shell operators into logical CLI invocations.

    Examples:
        "indemn skill get X && indemn skill get Y" → ["indemn skill get X", "indemn skill get Y"]
        "indemn op create"                          → ["indemn op create"]
        "cmd1 ; cmd2 || cmd3"                       → ["cmd1", "cmd2", "cmd3"]

    Caller responsibility: this is a simple regex split. If shell-quoted strings
    ever contain literal `&&` / `||` / `;` outside escapes, the split is wrong —
    but no `indemn` CLI invocation should embed those operators in argument
    values. JSON args with `&&` would already be broken under shell interpretation.
    """
    if not isinstance(command, str) or not command.strip():
        return []
    parts = _SHELL_OPERATOR.split(command.strip())
    return [p.strip() for p in parts if p.strip()]


def _derive_tool_call_commands(trace: dict) -> list[str]:
    """Derived: flat list of all individual CLI invocations across all messages.

    Splits chained `&&` / `||` / `;` commands. This is the path Evaluators should
    reference for "did the agent run command X" style checks — robust against the
    common pattern of agents chaining multiple CLI calls in a single `execute`
    tool call to save turns.

    Returns: list of command strings, in message + intra-message order.
    """
    msgs = trace.get("messages") or []
    commands: list[str] = []
    for m in msgs:
        if m.get("type") != "ai":
            continue
        for tc in m.get("tool_calls") or []:
            args = tc.get("args") or {}
            cmd = args.get("command")
            if isinstance(cmd, str):
                commands.extend(_split_chained_command(cmd))
    return commands


def _derive_tool_call_summary(trace: dict) -> list:
    """Derived: list of per-tool-call summaries across all messages.

    Shape pinned for P3:
      [{tool_name, args, result_status, result_preview}, ...]
    where:
      - tool_name: the tool's name (e.g., "execute", "write_todos")
      - args: the tool_call's args dict (preserves "command" for execute calls)
      - result_status: "success" | "error" | "unknown" derived from following tool message
      - result_preview: first ~500 chars of the tool result content (for llm_judge context)
    """
    msgs = trace.get("messages") or []
    # Build a tool_call_id → result mapping by walking messages.
    results_by_id = {}
    for m in msgs:
        if m.get("type") == "tool":
            tid = m.get("tool_call_id") or m.get("id")
            content = m.get("content") or ""
            if isinstance(content, list):
                # Newer LangChain shape: list of content parts
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
            results_by_id[tid] = content

    summary = []
    for m in msgs:
        if m.get("type") != "ai":
            continue
        for tc in m.get("tool_calls") or []:
            tcid = tc.get("id")
            result = results_by_id.get(tcid, "")
            result_status = _classify_tool_result(result)
            summary.append({
                "tool_name": tc.get("name"),
                "args": tc.get("args") or {},
                "result_status": result_status,
                "result_preview": (result[:500] if isinstance(result, str) else str(result)[:500]),
            })
    return summary


def _classify_tool_result(content: str) -> str:
    """Classify a tool result as success/error/unknown.

    Inspects markers from harness_common.cli (e.g., '[Command succeeded with exit code 0]',
    '[Command failed]', '[stderr]'). Best-effort; llm_judge prompts can re-inspect raw content.
    """
    if not isinstance(content, str):
        return "unknown"
    if "[Command succeeded" in content:
        return "success"
    if "[Command failed" in content or "[stderr]" in content or "Error" in content[:200]:
        return "error"
    return "unknown"


def _derive_transition_reason(trace: dict) -> Optional[str]:
    """Derived: extract --reason "..." from the final entity-transition CLI call.

    Looks at the LAST `indemn (touchpoint|email|meeting|slackmessage) transition`
    execute tool call in the trace's messages and extracts the --reason argument.
    Returns None if no such transition call exists.
    """
    msgs = trace.get("messages") or []
    transition_re = re.compile(
        r"indemn (?:touchpoint|email|meeting|slackmessage) transition\s+\S+"
        r".*?--reason\s+(?P<quote>[\"'])(?P<reason>.*?)(?P=quote)",
        re.DOTALL,
    )
    last_reason = None
    for m in msgs:
        if m.get("type") != "ai":
            continue
        for tc in m.get("tool_calls") or []:
            args = tc.get("args") or {}
            cmd = args.get("command") or ""
            if not cmd:
                continue
            match = transition_re.search(cmd)
            if match:
                last_reason = match.group("reason")
    return last_reason


# --- Entity paths ---


async def resolve_entity_path(path: str, context: dict) -> Any:
    """Handle `entity:<Type>:<id>.<field>` via direct MongoDB load (NO CLI subprocess).

    Supports a virtual `_state` field that resolves to whatever field on the entity
    has `is_state_field: true` on its definition. This decouples eval check expressions
    from entity-specific state-field naming — Meeting has `.stage`, Email has `.status`,
    a Submission entity might have `.workflow_state`. Using `entity:Type:id._state`
    works across all of them. The kernel sets `_state_field_name` on the entity class
    via `kernel/entity/factory.py:110`.
    """
    body = path[len("entity:") :]
    # body is "<Type>:<id>.<field>..." — the type+id are colon-separated before the first dot
    head_dot = body.find(".")
    if head_dot == -1:
        head, field_path = body, ""
    else:
        head, field_path = body[:head_dot], body[head_dot + 1 :]
    if ":" not in head:
        raise ValueError(f"Malformed entity path (need Type:id): {path!r}")
    entity_type, entity_id = head.split(":", 1)

    # Resolve the _state virtual field to the entity's actual state field name.
    # Supports `._state` (whole path) or `._state.nested` (descend after state field).
    if field_path == "_state" or field_path.startswith("_state."):
        actual_state_field = _lookup_state_field_name(entity_type)
        if actual_state_field is None:
            raise ValueError(
                f"Entity type {entity_type!r} has no state field (no field with "
                f"is_state_field=True); cannot resolve `_state` on {path!r}"
            )
        # Replace the `_state` prefix with the actual field name.
        field_path = actual_state_field + field_path[len("_state") :]

    entity = await _load_entity(entity_type, entity_id)
    if entity is None:
        return None
    return _resolve_dotted(entity, field_path) if field_path else entity


def _lookup_state_field_name(entity_type: str) -> Optional[str]:
    """Look up the state field name for an entity type via ENTITY_REGISTRY.

    Returns the field name (e.g., "status" for Email, "stage" for Meeting) or None
    if the entity is not registered OR has no state field defined.
    """
    from kernel.db import ENTITY_REGISTRY

    cls = ENTITY_REGISTRY.get(entity_type)
    if cls is None:
        return None
    return getattr(cls, "_state_field_name", None)


async def _load_entity(entity_type: str, entity_id: str) -> Optional[dict]:
    """Load an entity by id from MongoDB via ENTITY_REGISTRY.

    Returns the entity as a dict (model_dump) for uniform downstream access.
    Org scoping is enforced by find_scoped which reads current_org_id contextvar.
    """
    from kernel.db import ENTITY_REGISTRY

    cls = ENTITY_REGISTRY.get(entity_type)
    if cls is None:
        # Per Group D++ no-fallbacks: unknown entity type is an error, not silent None.
        raise ValueError(f"Unknown entity_type in entity path: {entity_type}")
    try:
        oid = ObjectId(str(entity_id))
    except Exception as exc:
        raise ValueError(f"Invalid entity_id {entity_id!r} for {entity_type}: {exc}")
    entity = await cls.get_scoped(oid)
    if entity is None:
        return None
    return entity.model_dump(by_alias=True)


# --- Changes paths ---


async def resolve_changes_path(path: str, context: dict) -> Any:
    """Handle `changes:correlation_id=<cid>.<field>` and friends.

    Forms supported:
      changes:correlation_id=<cid>.<field>            # specific field across all changes for cid
      changes:entity_id=<id>.field=<f>.values         # historical values of <f> on <id>
    """
    body = path[len("changes:") :]
    # The first segment is `key=value`, then `.<rest>`.
    if "." not in body:
        raise ValueError(f"Malformed changes path (need .field): {path!r}")
    first_dot = body.find(".")
    key_value = body[:first_dot]
    rest = body[first_dot + 1 :]
    if "=" not in key_value:
        raise ValueError(f"Malformed changes path (need key=value): {path!r}")
    key, value = key_value.split("=", 1)

    from kernel.changes.collection import ChangeRecord

    filter_doc: dict = {}
    if key == "correlation_id":
        filter_doc["correlation_id"] = value
    elif key == "entity_id":
        try:
            filter_doc["entity_id"] = ObjectId(str(value))
        except Exception as exc:
            raise ValueError(f"Invalid entity_id in changes path: {exc}")
    else:
        raise ValueError(f"Unsupported changes filter key: {key!r}")

    # Two recognized rest-shapes:
    # 1) `<field>` — return list of that top-level field from each ChangeRecord
    # 2) `field=<f>.values` — return historical new_values for the named change field
    if rest.startswith("field=") and rest.endswith(".values"):
        field_name = rest[len("field=") : -len(".values")]
        records = await ChangeRecord.find(filter_doc).to_list()
        values = []
        for rec in records:
            for fc in rec.changes or []:
                if fc.field == field_name:
                    values.append(fc.new_value)
        return values
    # Plain field projection.
    records = await ChangeRecord.find(filter_doc).to_list()
    return [_resolve_dotted(rec.model_dump(by_alias=True), rest) for rec in records]


# --- Constellation paths ---


async def resolve_constellation_path(path: str, context: dict) -> Any:
    """Handle constellation.* paths.

    Forms:
      constellation.created_in_this_run.<EntityType>     # list of entity dicts created in this run
      constellation.created_in_this_run.entity_counts    # dict of {EntityType: count}
      constellation.created_in_this_run.detail           # flat list of all created entity records
      constellation.company.<EntityType>                 # entities linked to Run's target Company
    """
    suffix = path[len("constellation.") :]
    trace = context["trace"]

    if suffix.startswith("created_in_this_run."):
        what = suffix[len("created_in_this_run.") :]
        if what == "entity_counts":
            return await _constellation_entity_counts(trace)
        if what == "detail":
            return await _constellation_detail(trace)
        # Entity-type-specific lookup; `what` may include subscript/field like "Deal[0]._id"
        # which is handled at the resolution layer below.
        return await _constellation_by_type(trace, what)

    if suffix.startswith("company."):
        entity_type = suffix[len("company.") :]
        return await _constellation_company_by_type(trace, entity_type)

    raise ValueError(f"Unknown constellation path: {path!r}")


async def _changes_create_records(correlation_id: str) -> list:
    """Helper: return all create-type ChangeRecords for a correlation_id."""
    from kernel.changes.collection import ChangeRecord

    return await ChangeRecord.find({
        "correlation_id": correlation_id,
        "change_type": "create",
    }).to_list()


async def _constellation_entity_counts(trace: dict) -> dict:
    cid = trace.get("correlation_id")
    if not cid:
        return {}
    records = await _changes_create_records(cid)
    counts: dict = {}
    for rec in records:
        counts[rec.entity_type] = counts.get(rec.entity_type, 0) + 1
    return counts


async def _constellation_detail(trace: dict) -> list:
    """Load FULL entity records for everything created in this run."""
    from kernel.db import ENTITY_REGISTRY

    cid = trace.get("correlation_id")
    if not cid:
        return []
    records = await _changes_create_records(cid)
    out = []
    for rec in records:
        cls = ENTITY_REGISTRY.get(rec.entity_type)
        if cls is None:
            continue
        entity = await cls.get_scoped(rec.entity_id)
        if entity is not None:
            d = entity.model_dump(by_alias=True)
            d["_entity_type"] = rec.entity_type
            out.append(d)
    return out


async def _constellation_by_type(trace: dict, what: str) -> Any:
    """Resolve `constellation.created_in_this_run.<Type>[opt subscript].<opt field>`.

    `what` may be:
      "Decision"             → list of Decision dicts
      "Decision[*]"          → same as above (explicit iterate)
      "Decision[*].field"    → list of field values
      "Decision[0]"          → single dict (the first Decision)
      "Decision[0]._id"      → scalar (the first Decision's _id)
    """
    from kernel.db import ENTITY_REGISTRY

    cid = trace.get("correlation_id")
    if not cid:
        return []
    # Parse out the entity type from `what` — terminates at `[` or `.`.
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(.*)$", what)
    if not m:
        raise ValueError(f"Malformed constellation.created_in_this_run path: {what!r}")
    entity_type, tail = m.group(1), m.group(2)
    records = await _changes_create_records(cid)
    entity_ids = [rec.entity_id for rec in records if rec.entity_type == entity_type]
    if not entity_ids:
        # No entities of this type created in this run; downstream aggregation handles empty list.
        return [] if tail == "" or tail.startswith("[") else None
    cls = ENTITY_REGISTRY.get(entity_type)
    if cls is None:
        # Records exist but the entity type is unknown — load IS needed and cannot proceed.
        raise ValueError(f"Unknown entity_type in constellation path: {entity_type}")
    # Load full records.
    entities = []
    cursor = cls.find_scoped({"_id": {"$in": entity_ids}})
    docs = await cursor.to_list()
    for d in docs:
        entities.append(d.model_dump(by_alias=True))
    # Now apply the tail navigation: [N] | [*] | [*].field | [N].field.
    return _navigate_array_tail(entities, tail)


async def _constellation_company_by_type(trace: dict, entity_type: str) -> list:
    """Resolve `constellation.company.<EntityType>` — entities linked to the run's Company."""
    from kernel.db import ENTITY_REGISTRY

    # Find the Company id via the trace's entity (typically a Touchpoint with .company).
    company_id = await _resolve_run_company_id(trace)
    if company_id is None:
        return []
    cls = ENTITY_REGISTRY.get(entity_type)
    if cls is None:
        raise ValueError(f"Unknown entity_type in constellation.company path: {entity_type}")
    cursor = cls.find_scoped({"company": company_id})
    docs = await cursor.to_list()
    return [d.model_dump(by_alias=True) for d in docs]


async def _resolve_run_company_id(trace: dict) -> Optional[ObjectId]:
    """Resolve the Company id for the run.

    Convention: the Run's entity is typically a Touchpoint (for IE/CE/PH) with
    a `.company` field. For other associates (EC on Email, MC on Meeting, SC
    on SlackMessage) the entity itself has a `.company` field. We use the
    generic mechanism: load the run's entity, return its `company` field
    (or `None` if missing).
    """
    from kernel.db import ENTITY_REGISTRY

    entity_type = trace.get("entity_type")
    entity_id = trace.get("entity_id")
    if not entity_type or not entity_id:
        return None
    cls = ENTITY_REGISTRY.get(entity_type)
    if cls is None:
        return None
    entity = await cls.get_scoped(ObjectId(str(entity_id)))
    if entity is None:
        return None
    d = entity.model_dump(by_alias=True)
    company = d.get("company")
    if company is None:
        return None
    if isinstance(company, ObjectId):
        return company
    try:
        return ObjectId(str(company))
    except Exception:
        return None


# --- Generic helpers ---


def _resolve_dotted(data: Any, path: str) -> Any:
    """Walk a dotted path with optional `[N]` / `[*]` segments through nested dicts/lists.

    Supports: a.b, a.b[0], a.b[*], a.b[*].c, a[0].b.

    Returns None if any intermediate field is missing. Returns a list when `[*]`
    is present anywhere in the path.
    """
    if path == "":
        return data
    # Tokenize: split on '.' but preserve '[N]'/'[*]' attached to preceding token.
    # We do a manual scan to handle the mixed grammar cleanly.
    return _walk(data, path)


def _walk(current: Any, remaining: str) -> Any:
    """Recursive walker. Handles dot + bracket + iterate."""
    if remaining == "":
        return current
    if current is None:
        return None
    # Find next separator: '.', '[', or end of string.
    next_dot = remaining.find(".")
    next_br = remaining.find("[")
    # Choose the earliest separator.
    cands = [i for i in (next_dot, next_br) if i != -1]
    if not cands:
        # Plain field name remaining
        return _get_field(current, remaining)
    next_sep = min(cands)

    field_name = remaining[:next_sep]
    rest = remaining[next_sep:]
    if field_name:
        current = _get_field(current, field_name)
        if current is None:
            return None

    if rest.startswith("."):
        return _walk(current, rest[1:])
    if rest.startswith("["):
        return _walk_bracket(current, rest)
    return current


def _walk_bracket(current: Any, remaining: str) -> Any:
    """Handle '[N]' / '[*]' / '[?{...}]' followed by optional dot continuation."""
    close = remaining.find("]")
    if close == -1:
        raise ValueError(f"Unclosed bracket in path: {remaining!r}")
    inside = remaining[1:close]
    after = remaining[close + 1 :]
    if inside == "*":
        if not isinstance(current, list):
            return None if current is None else []
        if after == "":
            return current
        # JSONPath-style flattening: each `[*]` collapses one level of nesting.
        # If the per-item continuation produces a list, extend (flatten); else append.
        # None values from missing fields on individual items are skipped (matches
        # "iterate present values; ignore missing").
        results: list = []
        for item in current:
            if after.startswith("."):
                sub = _walk(item, after[1:])
            elif after.startswith("["):
                sub = _walk_bracket(item, after)
            else:
                sub = item
            if isinstance(sub, list):
                results.extend(sub)
            elif sub is not None:
                results.append(sub)
        return results
    if inside.startswith("?{"):
        # Predicate filter — defer to a simple field=value parser for now.
        # Form: ?{field:value} — only equality predicates supported in v1.
        # Per Group D++ no-fallbacks: if a different predicate shape is needed,
        # STOP and escalate; do not silently accept a malformed predicate.
        predicate = inside[2:-1]  # strip "?{" and trailing "}"
        if ":" not in predicate:
            raise ValueError(f"Predicate must be field:value (got: {predicate!r})")
        pf, pv = predicate.split(":", 1)
        pv = pv.strip('"').strip("'")
        if not isinstance(current, list):
            return []
        filtered = [item for item in current if _get_field(item, pf) == pv]
        if after == "":
            return filtered
        if after.startswith("."):
            return [_walk(item, after[1:]) for item in filtered]
        return filtered
    # Numeric index
    try:
        idx = int(inside)
    except ValueError:
        raise ValueError(f"Bracket subscript must be N, *, or ?{{...}}: {inside!r}")
    if not isinstance(current, list) or idx < 0 or idx >= len(current):
        return None
    sub = current[idx]
    if after == "":
        return sub
    if after.startswith("."):
        return _walk(sub, after[1:])
    if after.startswith("["):
        return _walk_bracket(sub, after)
    return sub


def _get_field(data: Any, field: str) -> Any:
    """Get a single field from a dict-like. Returns None on miss."""
    if isinstance(data, dict):
        return data.get(field)
    if hasattr(data, field):
        return getattr(data, field)
    return None


def _navigate_array_tail(entities: list, tail: str) -> Any:
    """Apply a tail like '', '[*]', '[*].field', '[0]', '[0].field' to a list of entities."""
    if tail == "":
        return entities
    return _walk_bracket(entities, tail)


def _normalize_for_comparison(value: Any) -> Any:
    """Normalize values for comparison. ObjectId → str; list/dict pass through."""
    if isinstance(value, ObjectId):
        return str(value)
    return value


def _to_str(value: Any) -> str:
    """Stringify a scalar for template substitution."""
    if isinstance(value, ObjectId):
        return str(value)
    return str(value)


# --- Aggregation ops ---


def _apply_aggregation(op: str, resolved: Any, expected: Any, expr: dict) -> Any:
    """Apply an aggregation op to a resolved value (typically a list)."""
    # Normalize: non-list → empty list (aggregation on missing/None field is empty).
    if resolved is None:
        items = []
    elif isinstance(resolved, list):
        items = resolved
    else:
        # A scalar where a list was expected. Per Group D++ no-fallbacks: this is
        # almost certainly a misconfigured Evaluator; surface explicitly.
        raise ValueError(
            f"Aggregation op {op!r} requires a list-resolved field; got {type(resolved).__name__}"
        )

    if op == "count":
        n = len(items)
        # Group E E1: dual mode — if `value` is present in the expression, return
        # bool (count == value). If not, return int for continuous scoring per D-H.
        if "value" in expr:
            return n == expected
        return n

    # Normalize expected value for comparison ops (ObjectId → str) so it
    # matches the per-item normalization applied to resolved elements.
    expected_norm = _normalize_for_comparison(expected)

    if op == "any_matches_equals":
        return any(_normalize_for_comparison(x) == expected_norm for x in items)
    if op == "any_matches_contains":
        return any(_contains(x, expected_norm) for x in items)
    if op == "any_matches_regex":
        pat = re.compile(expected) if isinstance(expected, str) else None
        if pat is None:
            return False
        return any(isinstance(x, str) and pat.search(x) for x in items)

    if op in ("all_match_equals", "all_equal"):
        # Vacuously True for empty arrays — load-bearing for IE-4 trivial-pass.
        return all(_normalize_for_comparison(x) == expected_norm for x in items)
    if op == "all_match_contains":
        return all(_contains(x, expected_norm) for x in items)

    if op == "none_match_equals":
        return all(_normalize_for_comparison(x) != expected_norm for x in items)
    if op == "none_match_contains":
        return not any(_contains(x, expected_norm) for x in items)
    if op == "none_match_regex":
        pat = re.compile(expected) if isinstance(expected, str) else None
        if pat is None:
            return True  # No pattern → no match → none-match is True
        return not any(isinstance(x, str) and pat.search(x) for x in items)

    raise ValueError(f"Unknown aggregation op: {op}")


def _contains(value: Any, needle: Any) -> bool:
    """Compatibility with watch evaluator's 'contains' semantics on string OR list."""
    if value is None:
        return False
    if isinstance(value, str):
        return needle in value
    if isinstance(value, list):
        return needle in value
    return False


# --- Ordering op ---


def _first_call_before_first_create(
    messages: Any,
    regex_call: str,
    regex_target: str,
) -> bool:
    """Ordering check on trace.messages.

    Walks all `tool_calls[*].args.command` entries in message order, splitting
    each command on shell operators (`&&` / `||` / `;`) so chained CLI
    invocations are treated as SEPARATE logical commands. This is essential
    because the IE skill (and other associate skills) chain skill-gets +
    other reads with `&&` to save turns — without splitting, an ordering check
    like "skill-get must precede create" would falsely fail when both calls
    are in the same chained `args.command` string.

    Returns True iff:
      (a) no logical command matches regex_target  (trivial pass — the target action never happened); OR
      (b) some logical command matching regex_call precedes the first logical command matching regex_target.

    Returns False iff the first matching regex_target appears before any matching regex_call.
    """
    if not isinstance(messages, list):
        return False
    pat_call = re.compile(regex_call)
    pat_target = re.compile(regex_target)
    # Build the FLAT, SPLIT, ordered list of logical commands across all messages.
    commands: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("type") != "ai":
            continue
        for tc in m.get("tool_calls") or []:
            args = tc.get("args") or {}
            cmd = args.get("command")
            if isinstance(cmd, str):
                commands.extend(_split_chained_command(cmd))
    first_call_idx = None
    first_target_idx = None
    for i, cmd in enumerate(commands):
        if first_target_idx is None and pat_target.search(cmd):
            first_target_idx = i
        if first_call_idx is None and pat_call.search(cmd):
            first_call_idx = i
        if first_call_idx is not None and first_target_idx is not None:
            break
    if first_target_idx is None:
        return True  # Target never happened — trivial pass.
    if first_call_idx is None:
        return False  # Target happened but call never did — fail.
    return first_call_idx < first_target_idx
