# Rules, Lookups, Capabilities, and the --auto Pattern

The rule engine is the deterministic processing layer of the Indemn OS kernel. It evaluates per-organization condition-action patterns against entity data to produce results without invoking an LLM. When rules cannot produce a result, the system returns `needs_reasoning` so the caller can fall back to AI.

The cost of AI is proportional to edge-case complexity, not total volume. The rule engine handles the known patterns deterministically. AI handles the unknowns. Over time, rules replace AI for every repeated pattern, and the system becomes cheaper, faster, and more predictable.

---

## The --auto Pattern

The `--auto` pattern is the single interface for deterministic-first, AI-fallback processing. There is no separate "rules mode" and "AI mode." Every invocation goes through the same path: try rules first, fall back to reasoning if needed.

### Flow

1. Associate claims a message from its queue
2. Associate loads the entity and its skill
3. Associate invokes a kernel capability with `--auto`
4. Kernel loads active rule groups for this entity type + capability
5. Kernel evaluates rules in priority order (highest first)
6. **If a `set_fields` rule matches (and no veto):** applies the field values, returns the deterministic result
7. **If a `force_reasoning` rule matches:** returns `needs_reasoning` with the veto reason, regardless of any `set_fields` matches
8. **If no rules match:** returns `needs_reasoning` with `reason: "no_match"`
9. Associate skill provides AI fallback when `needs_reasoning` is true
10. Entity saved via `save_tracked()` with rule evaluation metadata in the changes collection

### Why One Pattern

The `--auto` flag does not change what the associate does. It changes how the result is produced. The associate's skill always describes the AI behavior. The `--auto` flag adds a deterministic fast path in front of it. This means:

- Adding rules never changes the associate's interface
- Removing all rules degrades gracefully to pure AI
- The shift from AI to deterministic is gradual and reversible
- The same trace format captures both paths

### Associate Modes

Associates have a `mode` field that controls which path they use:

| Mode | Behavior |
|------|----------|
| `deterministic` | Rules only. If no match, the message fails (no LLM call). |
| `reasoning` | LLM only. Rules are not evaluated. |
| `hybrid` | Rules first, LLM fallback. This is the `--auto` pattern. |

Most associates use `hybrid`. The `deterministic` mode is for cases where you want strict rule-only processing with no AI cost (e.g., a routing associate that should never call an LLM). The `reasoning` mode is for capabilities where rules don't apply.

---

## Rules

Rules are per-organization condition-action patterns stored as data in MongoDB. They are not code. They are managed via CLI and API.

### Schema

```
Collection: rules
```

| Field | Type | Description |
|-------|------|-------------|
| `org_id` | ObjectId | Organization scope |
| `entity_type` | str | Entity type this rule applies to (e.g., "Email", "Submission") |
| `capability` | str | Capability name (e.g., "auto_classify", "auto_route") |
| `group_id` | ObjectId (optional) | Reference to a RuleGroup |
| `name` | str (optional) | Human-readable name for display and tracing |
| `conditions` | dict | JSON condition (same evaluator as watches) |
| `action` | "set_fields" or "force_reasoning" | What happens when conditions match |
| `sets` | dict (optional) | Field-value pairs to apply (for `set_fields` action) |
| `forces_reasoning_reason` | str (optional) | Explanation (for `force_reasoning` action) |
| `priority` | int | Evaluation order. Higher = evaluated first. Default: 100 |
| `status` | "draft" / "active" / "archived" | Only active rules evaluate |
| `created_by` | str | Actor ID of the creator |
| `created_at` | datetime | UTC timestamp |

**Implementation:** `kernel/rule/schema.py` (Beanie Document model)

**Index:** `(org_id, entity_type, capability, status, priority DESC)` -- optimized for the evaluation query.

### Exactly Two Actions

Rules have exactly two possible actions. This is a deliberate constraint.

**`set_fields`** -- Applies a deterministic result. The `sets` dict contains field-value pairs that are applied to the entity. Values can be literals or lookup references.

```bash
indemn rule create \
  --entity Email \
  --capability auto_classify \
  --name known-carrier \
  --when '{"field": "sender_domain", "op": "in", "value": ["usli.com", "markel.com"]}' \
  --action set_fields \
  --sets '{"classification": "carrier_response"}' \
  --priority 200
```

**`force_reasoning`** -- A veto rule. When this matches, it overrides ALL `set_fields` matches and forces the entity to AI processing. Use this for cases where a rule could match but the situation requires human-level judgment.

```bash
indemn rule create \
  --entity Email \
  --capability auto_classify \
  --name carrier-complaint \
  --when '{"all": [{"field": "sender_domain", "op": "in", "value": ["usli.com"]}, {"field": "subject", "op": "contains", "value": "complaint"}]}' \
  --action force_reasoning \
  --forces-reasoning-reason "Carrier complaints require judgment even from known domains" \
  --priority 300
```

**Why no other actions:**

- No `transition` action -- state transitions must go through the state machine (`transition_to()`), which enforces valid transitions and emits the right events. Allowing rules to bypass this would break state machine guarantees.
- No `call_capability` action -- this was considered and rejected. Capabilities are invoked by associates, not by rules. Rules produce data; capabilities produce behavior.
- No `send_notification` action -- watches already handle this. When a rule's `set_fields` changes an entity, the save goes through `save_tracked()`, which evaluates watches and creates messages. Notification is a side effect of state change, not a rule action.

### CLI Commands

```bash
# List rules (filtered)
indemn rule list --entity Email --capability auto_classify --status active

# Create a set_fields rule
indemn rule create \
  --entity Email \
  --capability auto_classify \
  --name known-carrier \
  --when '{"field": "sender_domain", "op": "in", "value": ["usli.com", "markel.com"]}' \
  --action set_fields \
  --sets '{"classification": "carrier_response"}' \
  --priority 200

# Create a force_reasoning (veto) rule
indemn rule create \
  --entity Email \
  --capability auto_classify \
  --name unusual-attachment \
  --when '{"field": "has_unusual_attachment", "op": "equals", "value": true}' \
  --action force_reasoning \
  --forces-reasoning-reason "Unusual attachments need manual inspection" \
  --priority 500

# Archive a rule (soft delete)
indemn rule archive <rule_id>
```

**Implementation:** `kernel/cli/rule_commands.py` (Typer CLI), `kernel/api/rule_routes.py` (FastAPI routes)

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/rules/` | List rules with optional filters (`entity_type`, `capability`, `status`) |
| GET | `/api/rules/{rule_id}` | Get a single rule by ID |
| POST | `/api/rules/` | Create a new rule (runs validation) |
| PUT | `/api/rules/{rule_id}` | Update a rule's fields |
| DELETE | `/api/rules/{rule_id}` | Archive a rule (sets status to "archived") |

All endpoints require authentication and `Rule` write permission (checked via `check_permission(actor, "Rule", "write")`).

---

## Rule Groups

Rules belong to RuleGroups. Groups provide lifecycle management and governance.

### Schema

```
Collection: rule_groups
```

| Field | Type | Description |
|-------|------|-------------|
| `org_id` | ObjectId | Organization scope |
| `entity_type` | str | Entity type this group covers |
| `name` | str | Group name |
| `description` | str (optional) | What this group is for |
| `status` | "draft" / "active" / "archived" | Group lifecycle state |
| `owner` | str | Who is responsible for this group |
| `created_at` | datetime | UTC timestamp |

**Implementation:** `kernel/rule/schema.py` (same file as Rule)

**Index:** `(org_id, entity_type, status)`

### Lifecycle: draft -> active -> archived

| Status | Rules evaluate? | Use case |
|--------|----------------|----------|
| `draft` | No | New rules being tested. Safe to experiment without production impact. |
| `active` | Yes | Production rules. Only active rules in active groups are evaluated. |
| `archived` | No | Retired rules. Preserved for audit trail but excluded from evaluation. |

**Key behavior:** A rule that is individually `active` but belongs to a `draft` or `archived` group will NOT be evaluated. Both the rule AND its group must be active.

Rules without a `group_id` (ungrouped) are always eligible for evaluation if their own status is `active`. This supports simple cases where group governance is unnecessary.

### Governance

- **Ownership:** Every group has an `owner` field. This is for team accountability -- you know who to talk to about a group of rules.
- **Draft testing:** Create new rules in a draft group. Verify behavior against test data. Activate the group when confident. This prevents accidental production impact from untested rules.
- **Archival:** When rules are no longer needed, archive the group. All rules in it stop evaluating. The data is preserved.

---

## Rule Evaluation

The evaluation engine loads all active rules for an org + entity type + capability, filters by group status, evaluates conditions in priority order, and returns a structured result.

### Algorithm

```
1. Load all rules where org_id + entity_type + capability match and rule status = "active"
2. Sort by priority descending (highest first)
3. Load all referenced rule groups
4. Filter out rules whose group_id references a non-active group
5. For each remaining rule:
   a. Evaluate conditions against entity data
   b. If match + force_reasoning: add to veto list
   c. If match + set_fields: add to match list
6. If any veto rule matched: return vetoed (highest-priority veto wins)
7. If any set_fields rule matched: return the winning rule's sets (highest priority)
8. If no rules matched: return no_match
```

**First match wins.** Within `set_fields` rules, the highest-priority match is applied. There is no merging of results from multiple matching rules.

**Veto overrides everything.** If ANY `force_reasoning` rule matches, the result is `needs_reasoning` regardless of how many `set_fields` rules also matched. The highest-priority veto's reason is included in the result.

**Implementation:** `kernel/rule/engine.py`

### Condition Language

Rules use the same condition evaluator as watches. One evaluator, one syntax, one debugging surface.

**Implementation:** `kernel/watch/evaluator.py`

#### Field Comparisons

```json
{"field": "status", "op": "equals", "value": "active"}
{"field": "sender_domain", "op": "in", "value": ["usli.com", "markel.com"]}
{"field": "amount", "op": "gt", "value": 10000}
{"field": "last_activity_at", "op": "older_than", "value": "7d"}
```

#### Operators

| Operator | Description | Example value |
|----------|-------------|---------------|
| `equals` | Exact equality | `"active"` |
| `not_equals` | Not equal | `"closed"` |
| `contains` | Substring match (converts to string) | `"complaint"` |
| `not_contains` | Substring absence | `"test"` |
| `starts_with` | String prefix | `"MGL"` |
| `ends_with` | String suffix | `"@usli.com"` |
| `gt` | Greater than | `10000` |
| `gte` | Greater than or equal | `5` |
| `lt` | Less than | `100` |
| `lte` | Less than or equal | `50` |
| `in` | Value is in a list | `["usli.com", "markel.com"]` |
| `not_in` | Value is not in a list | `["test.com"]` |
| `matches` | Regex match | `"^MGL-\\d+"` |
| `exists` | Field is not null | `true` (value ignored) |
| `older_than` | Datetime is older than duration | `"7d"`, `"24h"`, `"30m"`, `"60s"` |
| `within` | Datetime is within duration (inverse of older_than) | `"7d"` |

#### Duration Strings (for older_than / within)

Format: `<amount><unit>` where unit is `s` (seconds), `m` (minutes), `h` (hours), `d` (days).

Examples: `"7d"` = 7 days, `"24h"` = 24 hours, `"30m"` = 30 minutes, `"60s"` = 60 seconds.

#### Nested Field Access

Dot notation accesses nested fields:

```json
{"field": "metadata.source", "op": "equals", "value": "outlook"}
{"field": "data.policy_type", "op": "in", "value": ["auto", "home"]}
```

#### Logical Composition

```json
{"all": [
  {"field": "sender_domain", "op": "equals", "value": "usli.com"},
  {"field": "has_attachment", "op": "equals", "value": true}
]}

{"any": [
  {"field": "subject", "op": "contains", "value": "renewal"},
  {"field": "subject", "op": "contains", "value": "endorsement"}
]}

{"not": {"field": "status", "op": "equals", "value": "archived"}}
```

Composition can nest arbitrarily:

```json
{"all": [
  {"field": "sender_domain", "op": "in", "value": ["usli.com", "markel.com"]},
  {"not": {"field": "subject", "op": "contains", "value": "complaint"}},
  {"any": [
    {"field": "has_attachment", "op": "equals", "value": true},
    {"field": "body_length", "op": "gt", "value": 500}
  ]}
]}
```

### Evaluation Result Format

The engine returns a structured dict that is stored as `method_metadata` in the changes collection record when the entity is saved.

**Deterministic match (set_fields applied):**

```json
{
  "matched": true,
  "vetoed": false,
  "winning_rule": {
    "name": "known-carrier",
    "sets": {"classification": "carrier_response"}
  },
  "attempted_rules": [
    {"name": "known-carrier", "matched": true, "action": "set_fields", "priority": 200},
    {"name": "generic-fallback", "matched": false, "action": "set_fields", "priority": 50}
  ]
}
```

**Veto (force_reasoning matched):**

```json
{
  "matched": true,
  "vetoed": true,
  "reason": "veto",
  "veto_reason": "Carrier complaints require judgment even from known domains",
  "attempted_rules": [
    {"name": "carrier-complaint", "matched": true, "action": "force_reasoning", "priority": 300},
    {"name": "known-carrier", "matched": true, "action": "set_fields", "priority": 200}
  ],
  "winning_veto": {
    "name": "carrier-complaint",
    "reason": "Carrier complaints require judgment even from known domains"
  }
}
```

**No match (no rules matched):**

```json
{
  "matched": false,
  "vetoed": false,
  "reason": "no_match",
  "attempted_rules": [
    {"name": "known-carrier", "matched": false, "action": "set_fields", "priority": 200}
  ]
}
```

**No rules configured:**

```json
{
  "matched": false,
  "vetoed": false,
  "reason": "no_rules_configured",
  "attempted_rules": []
}
```

### Tracing

Rule evaluation results are stored in the changes collection as `method_metadata` on the entity's change record. This was a design simplification -- the original design called for a separate audit stream, but embedding the trace in the changes collection means the evaluation context is always co-located with the entity mutation it produced.

To inspect rule evaluation for a specific entity:

```bash
indemn trace entity Email <entity_id>
```

The unified timeline shows the change record with its `method_metadata` containing the full rule evaluation trace.

---

## Rule Validation

Rules are validated at creation time to prevent invalid configurations from entering the system.

### Validation Checks

**1. Field existence.** Every field referenced in `sets` must exist on the target entity type. The validator checks against both the entity class model fields and the entity definition's field definitions (for dynamic entities). Fields that are lookup references (`{"lookup": "..."}`) are excluded from this check.

**2. State machine protection.** State machine fields (`status`, `stage`) cannot be set by rules. State transitions must go through `transition_to()`, which enforces valid transition paths, records transition history, and emits the correct events. A rule that tries to set `status` or `stage` is rejected with an error.

**3. Overlap detection.** The validator performs heuristic overlap detection against existing active rules for the same entity type and capability. If two rules reference the same fields in their conditions, a WARNING is issued. This is not a hard error -- use `--force` to create anyway. The heuristic extracts field names from conditions (including nested `all`/`any` blocks) and checks for intersection. It is not exhaustive (it cannot detect semantic overlap), but catches the common case of two rules competing on the same field.

**Implementation:** `kernel/rule/validation.py`

### Validation Error vs Warning

- Hard errors (field doesn't exist, state machine field in sets) block creation
- Warnings (overlap detection) are returned but do not block creation. The CLI prints them; the API returns them as soft errors.

---

## Lookups

Lookups are key-value mapping tables. They exist to prevent rule explosion.

### The Problem They Solve

Without lookups, mapping 47 carrier prefix codes to lines of business requires 47 rules. With a lookup, it requires one rule that references a lookup table. The lookup contains the 47 mappings. When a new code appears, you update the lookup -- not the rules.

### Schema

```
Collection: lookups
```

| Field | Type | Description |
|-------|------|-------------|
| `org_id` | ObjectId | Organization scope |
| `name` | str | Lookup name (unique per org) |
| `data` | dict | Key-value mapping |
| `description` | str (optional) | What this lookup contains |
| `created_by` | str | Actor ID |
| `created_at` | datetime | UTC timestamp |

**Implementation:** `kernel/rule/lookup.py` (Beanie Document model + resolution logic)

**Index:** `(org_id, name)`

### How Rules Reference Lookups

Instead of a literal value in a rule's `sets`, use a lookup reference:

```json
{
  "sets": {
    "line_of_business": {
      "lookup": "usli-prefix-lob",
      "from_field": "quote_prefix"
    }
  }
}
```

This means: read the entity's `quote_prefix` field, look it up in the `usli-prefix-lob` table, and set `line_of_business` to the result.

### Resolution

When a winning `set_fields` rule's `sets` dict contains lookup references, the engine resolves them before returning the result. The resolution logic (`resolve_lookup_references`):

1. For each field in `sets`, check if the value is a dict with a `"lookup"` key
2. If so, read the `from_field` from the entity data to get the lookup key
3. Load the named lookup for the current org
4. Look up the key in the lookup's `data` dict (keys are always strings)
5. If found, use the looked-up value. If not found (lookup miss), set the field to `null`.

Literal values in `sets` pass through unchanged.

### CLI Commands

```bash
# List all lookups
indemn lookup list

# Get a specific lookup
indemn lookup get usli-prefix-lob

# Create from inline JSON
indemn lookup create --name usli-prefix-lob \
  --data '{"MGL": "general_liability", "WC": "workers_comp", "BOP": "business_owners", "CPP": "commercial_package"}'

# Import from CSV (first column = key, second column = value)
indemn lookup import usli-prefix-lob --from-csv ./carrier-codes.csv
```

CSV import format:

```csv
code,line_of_business
MGL,general_liability
WC,workers_comp
BOP,business_owners
CPP,commercial_package
```

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/lookups/` | List all lookups for the current org |
| GET | `/api/lookups/{name}` | Get a lookup by name |
| POST | `/api/lookups/` | Create or update a lookup (upsert by name) |

The POST endpoint is an upsert: if a lookup with the given name already exists for the org, its `data` is replaced. This makes bulk updates idempotent.

**Implementation:** `kernel/cli/lookup_commands.py` (CLI), `kernel/api/lookup_routes.py` (API)

### Who Maintains Lookups

Lookups are designed to be maintainable by non-technical users. The data is a flat key-value map. The CSV import path means someone can maintain a spreadsheet and import it. No JSON editing or rule creation required.

---

## Kernel Capabilities

Capabilities are reusable operations that any entity type can activate. They are registered at import time and dispatched by name.

### Registry

The capability registry is a simple name-to-function map:

```python
CAPABILITY_REGISTRY: dict[str, Callable] = {}

def register_capability(name: str, func: Callable):
    CAPABILITY_REGISTRY[name] = func

def get_capability(name: str) -> Callable:
    func = CAPABILITY_REGISTRY.get(name)
    if not func:
        raise ValueError(f"Unknown capability: {name}")
    return func
```

Capabilities self-register by calling `register_capability()` at module import time. The `kernel/capability/__init__.py` imports all capability modules to trigger registration.

**Implementation:** `kernel/capability/registry.py`

### Implemented Capabilities

#### auto_classify

Classification via rules with AI fallback. This is the canonical `--auto` capability.

- Calls `evaluate_rules()` for the entity type + "auto_classify" capability
- If deterministic match: returns `needs_reasoning: false` with the sets values
- If veto or no match: returns `needs_reasoning: true` with context

**Implementation:** `kernel/capability/auto_classify.py`

```python
async def auto_classify(entity, config: dict, org_id) -> dict:
    result = await evaluate_rules(
        org_id=org_id,
        entity_type=type(entity).__name__,
        capability="auto_classify",
        entity_data=entity.model_dump(by_alias=True),
    )
    if result["matched"] and not result["vetoed"]:
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
```

#### stale_check

Staleness detection by configurable thresholds. Purely deterministic -- never returns `needs_reasoning`.

- Evaluates conditions directly from the capability activation config (not through the rules engine)
- Uses the same condition evaluator as watches and rules
- If conditions match: returns `{sets_field: sets_value}`
- If conditions don't match: returns empty result

**Implementation:** `kernel/capability/stale_check.py`

Config format:

```json
{
  "when": {"field": "last_activity_at", "op": "older_than", "value": "7d"},
  "sets_field": "is_stale",
  "sets_value": true
}
```

#### fetch_new

Integration polling for new data. A collection-level capability (operates on the entity TYPE, not an instance).

- Resolves an Integration by `system_type` for the org
- Calls the adapter's `fetch()` method with optional `since` parameter
- Deduplicates against existing entities by `external_ref`
- Creates new entities via `save_tracked()`
- Returns counts: fetched, created, skipped duplicates, errors

**Implementation:** `kernel/capability/fetch_new.py`

Incremental fetch: if no explicit `since` parameter, the capability checks the most recent entity's `date` field and uses that as the watermark.

#### aggregations

Pipeline metrics: state distribution and queue depth. Read-only queries, no entity mutation.

- `state_distribution(entity_cls, org_id)` -- counts entities per state machine value using MongoDB aggregation
- `queue_depth(org_id)` -- counts pending messages per role

**Implementation:** `kernel/capability/aggregations.py`

#### Others in the Library

The following capabilities exist in the design but are not yet implemented as kernel modules:

- `fuzzy_search` -- approximate matching across entity fields
- `pattern_extract` -- regex/pattern extraction from text fields
- `auto_link` -- automatic relationship detection between entities
- `auto_route` -- deterministic routing to roles/actors based on entity properties

These follow the same registration pattern and will be added to `kernel/capability/` as needed.

### Activating Capabilities on Entity Types

Capabilities are activated per-entity-type, per-org. The activation config is stored in the EntityDefinition's `activated_capabilities` list.

```bash
# Enable auto_classify on Submission
indemn entity enable Submission auto_classify \
  --config '{"evaluates": "classification-rule", "sets_field": "classification"}'

# Enable stale_check on Submission
indemn entity enable Submission stale_check \
  --config '{"when": {"field": "last_activity_at", "op": "older_than", "value": "7d"}, "sets_field": "is_stale", "sets_value": true}'

# Enable fetch_new on Meeting
indemn entity enable Meeting fetch_new \
  --config '{"system_type": "google_workspace"}'
```

The `indemn entity enable` command calls `PUT /api/entitydefinitions/{name}/enable-capability` which adds a `CapabilityActivation` entry to the entity definition.

**CapabilityActivation schema:**

```python
class CapabilityActivation(BaseModel):
    capability: str      # "auto_classify", "stale_check", "fetch_new"
    config: dict         # Capability-specific configuration
```

**Implementation:** `kernel/entity/definition.py` (CapabilityActivation model), `kernel/cli/entity_commands.py` (CLI)

---

## The Complete --auto Flow

```
Associate                    Kernel                         Rules Engine               Lookup Store
   |                           |                               |                          |
   |  claim message            |                               |                          |
   |-------------------------->|                               |                          |
   |  load entity + skill      |                               |                          |
   |-------------------------->|                               |                          |
   |                           |                               |                          |
   |  invoke capability        |                               |                          |
   |  (e.g. auto_classify)     |                               |                          |
   |-------------------------->|                               |                          |
   |                           |  load active rules            |                          |
   |                           |  (org + entity + capability)  |                          |
   |                           |------------------------------>|                          |
   |                           |                               |                          |
   |                           |  load active groups           |                          |
   |                           |------------------------------>|                          |
   |                           |                               |                          |
   |                           |  filter rules by group status |                          |
   |                           |------------------------------>|                          |
   |                           |                               |                          |
   |                           |  evaluate conditions          |                          |
   |                           |  (priority order, desc)       |                          |
   |                           |------------------------------>|                          |
   |                           |                               |                          |
   |                           |      [if set_fields match]    |                          |
   |                           |  resolve lookup references    |                          |
   |                           |------------------------------------------------------------->|
   |                           |                               |                          |
   |  result: {needs_reasoning:|                               |                          |
   |    false, sets: {...}}    |                               |                          |
   |<--------------------------|                               |                          |
   |                           |                               |                          |
   |  apply sets to entity     |                               |                          |
   |  save_tracked()           |                               |                          |
   |-------------------------->|                               |                          |
   |                           |  [in transaction]             |                          |
   |                           |  - version check              |                          |
   |                           |  - entity write               |                          |
   |                           |  - change record              |                          |
   |                           |    (rule_evaluation in        |                          |
   |                           |     method_metadata)          |                          |
   |                           |  - watch eval + messages      |                          |
   |                           |                               |                          |
   
   --- OR if needs_reasoning: true ---

   |  result: {needs_reasoning:|                               |                          |
   |    true, reason: "..."}   |                               |                          |
   |<--------------------------|                               |                          |
   |                           |                               |                          |
   |  [skill provides AI       |                               |                          |
   |   fallback via LLM call]  |                               |                          |
   |  apply AI result          |                               |                          |
   |  save_tracked()           |                               |                          |
   |-------------------------->|                               |                          |
```

### What Gets Recorded

When `save_tracked()` is called after rule evaluation, the `method_metadata` parameter carries the full rule evaluation result. This is written into the changes collection record:

```json
{
  "entity_type": "Email",
  "entity_id": "66a1...",
  "change_type": "update",
  "actor_id": "665f...",
  "method": "auto_classify",
  "method_metadata": {
    "rule_evaluation": {
      "matched": true,
      "vetoed": false,
      "winning_rule": {"name": "known-carrier", "sets": {"classification": "carrier_response"}},
      "attempted_rules": [
        {"name": "known-carrier", "matched": true, "action": "set_fields", "priority": 200}
      ]
    }
  },
  "changes": [
    {"field": "classification", "old_value": null, "new_value": "carrier_response"}
  ],
  "correlation_id": "abc-123",
  "previous_hash": "...",
  "current_hash": "..."
}
```

### Bulk Operations

The `--auto` pattern also works in bulk. The `process_bulk_batch` Temporal activity can invoke capabilities on batches of entities:

```python
# In bulk batch processing:
cap_fn = get_capability(spec.method_name)
result = await cap_fn(entity, {}, entity.org_id)
if not result.get("needs_reasoning"):
    for field, value in result.get("result", {}).items():
        setattr(entity, field, value)
    await entity.save_tracked(
        method=spec.method_name,
        method_metadata={"rule_evaluation": result.get("rule_evaluation")},
    )
```

In bulk mode, entities that return `needs_reasoning` are skipped (not processed). They can be collected and processed individually by an AI associate afterward.

---

## The needs_reasoning Metric

The `needs_reasoning` rate is the single most important metric for the rule engine. It tells you exactly where to invest in new rules.

### What It Measures

For each capability + entity type combination, track:

- Total invocations
- Count where `needs_reasoning: false` (deterministic path)
- Count where `needs_reasoning: true` (AI fallback)
- Rate = AI fallback count / total invocations

### Interpretation

| Rate | Meaning | Action |
|------|---------|--------|
| 100% | No rules configured or none matching | Add rules for common patterns |
| 50-99% | Some rules exist but many edge cases | Analyze AI fallback cases, add rules for repeated patterns |
| 10-49% | Good rule coverage, some genuine edge cases | Review AI cases -- are any repeated patterns? |
| 1-9% | Excellent deterministic coverage | The remaining cases may genuinely need AI |
| 0% | Fully deterministic | Consider if `deterministic` mode is appropriate |

### The Flywheel

1. Deploy a capability with zero rules (100% AI)
2. AI processes everything (expensive, slow)
3. Observe patterns in AI decisions from the changes collection traces
4. Create rules for the most common patterns
5. `needs_reasoning` rate drops
6. Repeat: observe remaining AI cases, add more rules
7. Over time: cost goes down, speed goes up, predictability increases

Every pattern the AI keeps handling is a candidate for a new rule. The metric tells you exactly which capability + entity type pairs have the highest return on new rules.

### Where the Data Lives

Every rule evaluation is recorded in the changes collection via `method_metadata`. To analyze `needs_reasoning` rates, query the changes collection:

```javascript
// MongoDB aggregation: needs_reasoning rate per capability per entity type
db.changes.aggregate([
  {$match: {"method": "auto_classify", "method_metadata.rule_evaluation": {$exists: true}}},
  {$group: {
    _id: "$entity_type",
    total: {$sum: 1},
    deterministic: {$sum: {$cond: [{$eq: ["$method_metadata.rule_evaluation.matched", true]}, 1, 0]}},
    needs_reasoning: {$sum: {$cond: [{$eq: ["$method_metadata.rule_evaluation.matched", false]}, 1, 0]}}
  }}
])
```

---

## Design Decisions

### Why only two rule actions

Simplicity. `set_fields` handles the deterministic case. `force_reasoning` handles the veto case. These are the only two things a condition-action pattern needs to do in this system.

Transitions were excluded because the state machine has its own enforcement layer (`transition_to()`) with valid-path checking, transition history recording, and event emission. Letting rules bypass this would create a second path for state changes with weaker guarantees.

Call-capability actions were considered in early design sessions and retracted. Capabilities are invoked by associates, not by rules. Rules produce data (field values). Capabilities produce behavior (classification, staleness checks, data fetching). Mixing them in one layer creates unpredictable execution chains.

### Why lookups are separate from rules

Rule explosion. Without lookups, every distinct mapping (carrier code to line of business, sender domain to category, zip code to region) requires its own rule. With lookups, one rule references a table that contains all the mappings. Adding a new mapping is a data update, not a rule creation.

Lookups also have a different maintenance profile. Rules require understanding conditions and actions. Lookups are flat key-value tables that anyone can maintain via CSV import.

### Why rule groups

Governance from day one. Without groups, every rule change is a production change. With groups, you can:

- Create rules in a draft group, test them, then activate the group
- Archive an entire category of rules at once
- Know who owns which rules
- See which rules are experimental vs production

This costs almost nothing to implement (a status field on a grouping document, a filter in the evaluation query) but prevents the class of incidents where an untested rule silently changes production behavior.

### Why --auto, not separate modes

One pattern means one interface, one trace format, one debugging surface. If rules and AI were separate code paths invoked differently, you would need to choose at call time which path to use. With `--auto`, the caller always does the same thing. The system decides whether rules or AI handle it, and the decision is transparent in the trace.

This also makes the transition from AI to deterministic invisible to the caller. You can add rules to a capability that was previously 100% AI, and the associate's code does not change. The `needs_reasoning` rate just drops.

### Why conditions are JSON, not a DSL

The condition language is the same one used by watches. One evaluator (`kernel/watch/evaluator.py`), one syntax, one set of operators. Learning conditions for watches means you already know conditions for rules. Debugging a rule condition uses the same tools as debugging a watch condition.

JSON was chosen over a string DSL because it composes naturally (nested `all`/`any`/`not`), serializes trivially to MongoDB, and is validated structurally. There is no parser to maintain.

---

## File Reference

| Path | What |
|------|------|
| `kernel/rule/engine.py` | Rule evaluation: load rules, filter by group, evaluate conditions, return result |
| `kernel/rule/schema.py` | Rule and RuleGroup Beanie Document models |
| `kernel/rule/validation.py` | Rule creation validation: field existence, state machine protection, overlap detection |
| `kernel/rule/lookup.py` | Lookup Document model and `resolve_lookup_references()` |
| `kernel/watch/evaluator.py` | Condition evaluator: shared by watches and rules |
| `kernel/capability/__init__.py` | Imports all capability modules to trigger self-registration |
| `kernel/capability/registry.py` | Capability registry: `register_capability()`, `get_capability()` |
| `kernel/capability/auto_classify.py` | auto_classify: rules + AI fallback |
| `kernel/capability/stale_check.py` | stale_check: time-based staleness detection |
| `kernel/capability/fetch_new.py` | fetch_new: integration polling + deduplication |
| `kernel/capability/aggregations.py` | aggregations: state distribution, queue depth |
| `kernel/entity/definition.py` | EntityDefinition with CapabilityActivation schema |
| `kernel/entity/save.py` | save_tracked(): the one save path (rule metadata stored here) |
| `kernel/changes/collection.py` | ChangeRecord with method_metadata for rule evaluation traces |
| `kernel/cli/rule_commands.py` | CLI: `indemn rule list/create/archive` |
| `kernel/cli/lookup_commands.py` | CLI: `indemn lookup list/get/create/import` |
| `kernel/cli/entity_commands.py` | CLI: `indemn entity enable` (capability activation) |
| `kernel/api/rule_routes.py` | API: `/api/rules/` CRUD |
| `kernel/api/lookup_routes.py` | API: `/api/lookups/` CRUD |
| `kernel/temporal/activities.py` | Bulk batch processing with capability invocation |
