# Eval Framework

> Status: PHASE P3 LANDED 2026-05-26. P4 (online dispatch + cli_runner mode + LangSmith sync) is next; this doc grows as P4-P8 land.

The eval framework lets you score every associate Run against a set of pinned scoring dimensions ("Evaluators"). Each Evaluator scores ONE concern (one feedback_key) with explicit pass/fail and an optional `correction` string. The architecture aligns 1:1 with LangSmith primitives (Dataset, Example, Evaluator, Experiment, Feedback) so cross-tool workflows stay coherent.

This document covers the **Check engine** — the single engine that evaluates code Evaluator `check` expressions against Trace structure + entity state + change records. P4 wires the dispatcher; P5 the slice resolver; this doc grows as those land.

For the design rationale + 25 locked decisions (D-A through D-AA) + 4 Craig directives (D++) + 4 grammar extensions (Group E), see `projects/customer-system/artifacts/2026-05-22-eval-framework-refactor-implementation-plan.md`.

## Check engine — what it is

`kernel/eval/check_engine.py` is the single engine for evaluating code Evaluator check expressions. Per D-L architecture lock:

- ONE engine. No Path A / Path B split.
- Runs POST-CLAIM (outside `save_tracked` transactions). Full I/O permitted: entity loads, change-record queries, constellation queries.
- The watch evaluator at `kernel/watch/evaluator.py` stays UNCHANGED — entity-local + microseconds + no I/O constraint preserved.
- Logical composition (`all` / `any` / `not`) + leaf-op evaluators (`equals`, `contains`, ...) imported from the watch evaluator via `_OPERATORS` dict, NOT modified.

## Path grammar (D-C + Group E extensions)

Path strings have a prefix indicating data source + dot/bracket navigation:

```
trace.<field>                            # top-level Trace field
trace.messages[N].<field>                # specific message in array
trace.messages[*].<field>                # iterate all messages (flattened)
trace.messages[?{type:"ai"}].<field>     # filter messages then access
trace.tool_call_summary                  # derived: per-tool-call summary list
trace.transition_reason                  # derived: from final transition CLI args
entity:<EntityType>:<id>.<field>         # load entity by id, read field
entity:<EntityType>:<id>.<nested>.<f>    # dot-traverse after load
changes:correlation_id=<cid>.<field>     # query Changes collection
changes:entity_id=<id>.field=<f>.values  # historical values of <f>
example.reference_outputs.<key>          # offline eval Example reference
example.inputs.<key>                     # offline eval Example input
constellation.created_in_this_run.<EntityType>   # entities created (by correlation_id)
constellation.created_in_this_run.entity_counts  # dict of {EntityType: count}
constellation.created_in_this_run.detail         # flat list of all created records
constellation.company.<EntityType>       # entities linked to Run's target Company
```

**Grammar rules**:

- Prefixes are mandatory — no ambiguous bare field references.
- Array access: `[N]` for index (0-based), `[*]` for iterate, `[?{field:value}]` for filter.
- Each `[*]` flattens one level (JSONPath-standard). `messages[*].tool_calls[*].args.command` produces a flat list of command strings; messages with no `tool_calls` field are silently skipped.
- `entity:Type:id` requires the colon-separated form; only `entity` and `changes` use this colon syntax.

## The `_state` virtual field (decouples checks from per-entity field names)

Entities can use different field names for their state machine field — `Email.status`,
`Meeting.stage`, `Touchpoint.status`, etc. Per the OS philosophy, the eval framework
should NOT force uniform field names across entities; semantically `Meeting.stage`
makes sense for the meeting workflow, and `Email.status` makes sense for email
classification. The kernel already knows which field is the state field via the
EntityDefinition's `is_state_field: true` flag (surfaced on the entity class as
`_state_field_name`).

Check expressions use `entity:Type:id._state` to reference the state field
generically. The check engine resolves `_state` to whatever field the entity
definition marks as the state field. Works across all entity types.

```
entity:Email:<id>._state          # resolves to Email.status
entity:Meeting:<id>._state        # resolves to Meeting.stage
entity:Touchpoint:<id>._state     # resolves to Touchpoint.status
entity:{trace.entity_type}:{trace.entity_id}._state  # polymorphic: works for any source type
```

If an entity has no state field, `_state` resolution raises `ValueError` explicitly
(per Group D++ no-fallbacks — surfaces an authoring error rather than silently
returning None).

Nested access after `_state` is supported (`_state.nested` resolves the alias then
descends), though state field values are typically scalars.

## Template substitution (Group E E2 + E3)

Path strings can contain `{placeholder}` substituted from trace/entity context at runtime:

```
{trace.entity_id}                                 → bare scalar substitution
entity:Touchpoint:{trace.entity_id}.field         → id-slot substitution
{entity:Touchpoint:{trace.entity_id}.company}     → nested entity-load (IE-4)
entity:{trace.entity_type}:{trace.entity_id}.x    → entity-type-slot (Group E E3)
{constellation.created_in_this_run.Deal[0]._id}   → subscript-then-field (Group E E2)
```

**Substitution rules**:

- Substitution is single-pass, innermost-first. The regex `\{([^{}]+)\}` matches braces with no nested braces inside.
- A `{...}` is recognized as a path placeholder **only if its content contains `.` or `:`**. This disambiguates from regex quantifier syntax (`{24}`, `{1,3}`) which appears literally inside `value` regex patterns.
- Predicate syntax `[?{field:"value"}]` is also excluded: any `{...}` whose content contains `"` or `'` is treated as a JSON predicate, not a path placeholder.
- If the entire input is a single placeholder, the resolved value is returned as-is (preserves ObjectId, None, int — supports Group E E4 null-as-target).
- If mixed text + placeholders, each placeholder's resolved value is `str()`'d and concatenated. A list/dict at a fragment position raises `ValueError` (per Group D++ no-fallbacks; no silent coercion).

## Operator vocabulary (LOCKED 2026-05-25 per Craig directive #2)

### Leaf comparison ops (reused from `kernel/watch/evaluator.py` via import; NOT modified)

`equals`, `not_equals`, `contains`, `not_contains`, `starts_with`, `ends_with`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `matches`, `exists`, `older_than`, `within`

Apply to scalar values. Do NOT operate on arrays directly.

### Aggregation ops (NEW in `check_engine.py`; resolve array first, then apply)

| Op | Semantics | Returns | Used in |
|---|---|---|---|
| `count` | Length of resolved array. **Dual mode per Group E E1**: with `value` field → bool (count == value); without → int (for continuous scoring per D-H). | int OR bool | TS-3 (`ts_deal_proposal_atomic`) |
| `any_matches_equals` | true iff at least one element equals `value` | bool | — |
| `any_matches_contains` | true iff at least one element contains `value` | bool | — |
| `any_matches_regex` | true iff at least one element matches the `value` regex | bool | IE-1, IE-2 |
| `all_match_equals` | true iff every element equals `value` | bool | — |
| `all_match_contains` | true iff every element contains `value` | bool | — |
| `all_equal` | alias for `all_match_equals` (preferred when checking "every X has Y = Z") | bool | IE-4 |
| `none_match_equals` | true iff no element equals `value` | bool | — |
| `none_match_contains` | true iff no element contains `value` | bool | — |
| `none_match_regex` | true iff no element matches the `value` regex | bool | (anti-pattern checks) |
| `first_call_matching_regex_before_first_create` | Ordering check on `trace.messages`. Takes `regex_call` + `regex_target`. True iff the first command matching `regex_call` precedes the first matching `regex_target`. Trivial-pass when `regex_target` is never seen. | bool | IE-1, MC-1, MC-3 |

**Notes**:

- All_match / none_match / all_equal are **vacuously True on empty arrays** — load-bearing for IE-4 trivial-pass when no entities of a type were created in the run.
- ObjectId values are normalized to `str()` before equality comparisons (both resolved and expected). Cross-type comparisons (str vs ObjectId) compare as if both were strings.
- Per Group D++ no-fallbacks: aggregation ops on a non-list resolved field raise `ValueError`. Unknown ops raise `ValueError`. Unknown entity types in `entity:` paths (when load is needed) raise `ValueError`.

## Group E grammar extensions (locked 2026-05-26)

Four amendments applied in-place after P2 Session B execution surfaced them:

- **E1 — `count` dual-mode**: bool when leaf op inside `any`/`all`/`not` composition (equality via `value`), int at top-level for continuous scoring. Used in TS-3 + future PH/CE "count == 0" patterns.
- **E2 — subscript-then-field substitution**: `{constellation.created_in_this_run.Deal[0]._id}` resolves to first Deal's `_id` as substitution scalar. Used by TS-3 + future cross-entity reference checks.
- **E3 — entity-type-slot substitution**: `entity:{trace.entity_type}:{trace.entity_id}.field` for polymorphic source-agnostic associates (TS/CE/PH where `trace.entity_type ∈ {Email, Meeting, SlackMessage}`).
- **E4 — null as target in equality ops**: `equals` / `not_equals` / `none_match_equals` / `all_match_equals` accept `null` as the `value` for MongoDB-style "field is/is-not null" semantics.

## Derived `trace.*` paths

The check engine computes two derived paths from `trace.messages`:

### `trace.tool_call_summary`

List of per-tool-call summary dicts:

```python
[
    {
        "tool_name": "execute",                      # tool's name
        "args": {"command": "indemn skill get X"},   # full args dict
        "result_status": "success" | "error" | "unknown",  # classified from tool result content
        "result_preview": "<first 500 chars of result>",   # for llm_judge prompts
    },
    ...
]
```

`result_status` is classified from markers in the tool result content:
- `[Command succeeded` → `success`
- `[Command failed` / `[stderr]` / starts with `Error` → `error`
- Otherwise → `unknown`

P5's llm_judge prompts use this for tool-result inspection (e.g., MC-3 ambiguity-halts, CE-3 null-only-writes).

### `trace.transition_reason`

Extracts the `--reason "..."` argument from the LAST `indemn (touchpoint|email|meeting|slackmessage) transition` execute tool call. Returns `None` if no such transition call exists. Used by IE-3 + TS-7 + PH-6 etc.

## Integration with the rest of the eval framework

Per D-L + D-M (visible Role-watch routing):

1. **Run is claimed** by the runtime; agent runs; produces a Trace via the standard harness path.
2. **`Trace:created` fires `eval_dispatcher` role's watch** (P4). Its actor is `mode=cli_runner` (per D-O) — deterministic, no LLM. It runs `indemn evaluator dispatch <trace_id>`.
3. **`indemn evaluator dispatch`** queries matching Evaluators (live, no caching per D-K) + dispatches:
   - **code Evaluators** → call `check_engine.evaluate_check(evaluator.check, trace, example)` synchronously in-process. Result drives `EvaluationResult.score` + `.passed`. (~milliseconds, no LLM.)
   - **llm_judge Evaluators** → create a synthetic `_EvalDispatch:created` message (per D-Q). Routes to the `evaluator` role's reasoning actor, which composes the per-Evaluator SystemMessage (per D-N + D-R) using `slice_resolver` (P5) and scores via deepagents.
4. **EvaluationResult is written** via `save_tracked`. Triggers `langsmith_sync` to push as LangSmith Feedback (per D-B + D-I).

The check engine is the foundation for the code-Evaluator path. Slice resolver (P5) handles llm_judge context materialization separately — different I/O constraints (CLI calls) and a different runtime location (harness, outside kernel trust boundary).

## What this doc does NOT cover (yet)

- **P4** — online dispatcher + cli_runner mode + LangSmith sync (next session)
- **P5** — harness slice resolver + per-Evaluator SystemMessage composition
- **P6** — offline experiment mode (`indemn experiment run`)
- **P7** — Run 10 + Run 11 verification + Evaluator reactivation
- **P8** — CLI command refactor + `indemn evaluator reapply`

Cross-references:
- Watch evaluator (entity-local, unchanged): `docs/architecture/watches-and-wiring.md` § Condition language
- Trace entity: `docs/architecture/observability.md` § Trace as kernel entity
- Per-field content-size-hint (used by harness slice_resolver, not check_engine): `docs/architecture/entity-framework.md` § Serialization Profiles
