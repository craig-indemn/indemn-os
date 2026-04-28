# Adding a New Entity Type

Step-by-step guide for defining a new entity type on the Indemn OS. By the end of this guide you will have a working entity with auto-generated CLI commands, API endpoints, skill documentation, and UI views.

---

## Prerequisites

- **CLI installed**: `bash install-cli.sh`
- **Authenticated**: `indemn auth login --org <org> --email <email>`
- **Understand entity criteria**: read [domain-modeling.md](../architecture/entity-framework.md) -- an entity must pass the 7 tests (identity, lifecycle, independence, not kernel mechanism, CLI test, watchable, multiplicity)
- **Server running**: the API must be reachable at `$INDEMN_API_URL` (or running locally)

---

## Step 1: Design the Entity

Before touching the CLI, answer these questions on paper:

1. **What is this entity?** (one sentence)
   - "An insurance submission from an agent to a carrier for quoting."

2. **What fields does it have?** (list with types)
   - See [field types](../architecture/entity-framework.md) for the full type map: `str`, `int`, `float`, `decimal`, `bool`, `datetime`, `date`, `objectid`, `list`, `dict`
   - For each field: is it required? Does it have a default? Is it an enum? Is it indexed?

3. **Does it have a lifecycle?** (state machine)
   - List the states. Draw the transitions. Terminal states have no outgoing transitions.
   - One field is the state field (`is_state_field: true`). Transitions are enforced -- the update endpoint rejects direct state field changes.

4. **What relationships does it have?** (to other entities)
   - Use `objectid` fields with `is_relationship: true` and `relationship_target` pointing to the target entity name.

5. **Who cares when it changes?** (informs watches later)
   - Which roles need to know when this entity is created, transitions, or has fields changed?

---

## Step 2: Create the Entity Definition

Use `indemn entity create --data` with a JSON blob containing the full definition:

> **`collection_name` is recommended-explicit.** When omitted, the CLI
> auto-derives via the `inflect` library (`Company` → `companies`,
> `Opportunity` → `opportunities`). Pre-2026-04-28 the auto-derive was
> naive `name.lower() + "s"`, producing typo'd plurals; existing
> collections in dev (`companys`, `opportunitys`) retain their typo'd
> names by design ("accept and fix forward" — no rename migration). Pass
> `--collection-name <name>` explicitly when you need to land in one of
> those existing collections during a cross-org clone, or any time the
> auto-plural would surprise you.

```bash
indemn entity create --data '{
  "name": "Submission",
  "collection_name": "submissions",
  "fields": {
    "title": {"type": "str", "required": true},
    "named_insured": {"type": "str", "required": true},
    "status": {
      "type": "str",
      "default": "received",
      "is_state_field": true,
      "enum_values": ["received", "triaging", "quoted", "declined", "pending_info", "bound", "expired"]
    },
    "lob": {
      "type": "str",
      "enum_values": ["GL", "WC", "BOP", "Auto", "Umbrella"]
    },
    "carrier_id": {
      "type": "objectid",
      "is_relationship": true,
      "relationship_target": "Carrier"
    },
    "effective_date": {"type": "date"},
    "expected_premium": {"type": "decimal"},
    "priority": {
      "type": "str",
      "enum_values": ["Low", "Medium", "High", "Critical"],
      "default": "Medium"
    },
    "notes": {"type": "str"}
  },
  "state_machine": {
    "received": ["triaging"],
    "triaging": ["quoted", "declined", "pending_info"],
    "pending_info": ["triaging"],
    "quoted": ["bound", "expired"],
    "bound": [],
    "declined": [],
    "expired": []
  }
}'
```

**What happens**: The kernel stores this as a document in the `entity_definitions` collection, creates a dynamic Python class at runtime, registers API routes and CLI commands, and generates skill documentation. No restart needed -- the API picks it up immediately, and each CLI invocation discovers all registered entities fresh.

**Constraints**:
- Entity names are PascalCase singular (`Submission`, not `submissions`)
- Collection names are lowercase plural (`submissions`)
- Entity names MUST NOT collide with kernel entity names: Organization, Actor, Role, Integration, Attention, Runtime, Session

---

## Step 3: Verify Auto-Generation

After creating the definition, everything is live. Verify each surface:

### CLI commands

```bash
# List all entities of this type
indemn submission list

# Create an instance
indemn submission create --data '{"title": "Acme GL Renewal", "named_insured": "Acme Corp"}'

# Get by ID
indemn submission get <id>

# Update fields (cannot update the state field -- use transition)
indemn submission update <id> --data '{"lob": "GL", "expected_premium": 45000}'

# State transition (dedicated command enforces the state machine)
indemn submission transition <id> --to triaging --reason "Initial triage"
```

### API endpoints

```bash
# List
curl $INDEMN_API_URL/api/submissions/

# Get by ID
curl $INDEMN_API_URL/api/submissions/<id>

# Create
curl -X POST $INDEMN_API_URL/api/submissions/ \
  -H "Content-Type: application/json" \
  -d '{"title": "Acme GL", "named_insured": "Acme Corp"}'

# Transition (Bug #21 — the canonical body field is `to`, matching the
# CLI's --to flag. Older docs referenced `target_state` but that never
# worked; the API has only ever accepted `to`.)
curl -X POST $INDEMN_API_URL/api/submissions/<id>/transition \
  -H "Content-Type: application/json" \
  -d '{"to": "triaging", "reason": "Initial triage"}'
```

### Skill documentation

```bash
# Read the auto-generated skill for this entity
indemn skill get Submission
```

The generated skill includes a fields table, lifecycle diagram, command reference, and relationship information.

### Entity definition listing

```bash
# Verify it appears in the entity registry
indemn entity list
```

### UI (if running)

Navigate to [os.indemn.ai](https://os.indemn.ai) -- the entity appears in the sidebar automatically. List view, detail view, create/edit forms, and state transition buttons are all generated from the definition.

---

## Step 4: Add Relationships

Relationships are `objectid` fields with `is_relationship: true`. They reference other entities by name.

### How relationships work

When you define a field like:

```json
"carrier_id": {
  "type": "objectid",
  "is_relationship": true,
  "relationship_target": "Carrier"
}
```

The kernel:
- Stores the target entity name in `_relationship_targets` on the dynamic class
- Uses this metadata for scope resolution in watches (the `field_path` mechanism for ownership routing)
- Includes relationship information in the auto-generated skill documentation
- If the entity has `flexible_data` with `schema_source` pointing to this field, the related entity's schema drives validation

### Creating entities with relationships

The Carrier entity must exist first. Create it, then reference its ID:

```bash
# Create the related entity first
indemn carrier create --data '{"name": "Hartford", "rating": "A+"}'
# Note the returned ID

# Create the submission referencing the carrier
indemn submission create --data '{
  "title": "Acme GL",
  "named_insured": "Acme Corp",
  "carrier_id": "<carrier_id_from_above>"
}'
```

### Querying related entities

```bash
# Find all submissions for a specific carrier
indemn submission list --filter '{"carrier_id": "<carrier_id>"}'
```

---

## Step 5: Add Watches

Watches wire the entity into the system's nervous system. Watches live on Roles -- they declare what entity changes matter to actors in that role.

A watch is: **entity type + event type + optional condition + optional scope**.

### Watch events

| Event | Fires When |
|-------|-----------|
| `created` | A new entity is inserted |
| `transitioned` | A state machine transition occurred (use `transitioned:triaging` to match a specific target state) |
| `method_invoked` | An `@exposed` method was called (use `method:classify` for a specific method) |
| `fields_changed` | An exposed method changed fields |
| `deleted` | An entity is removed |

### Add a watch to a role

Use `indemn role add-watch` with entity, event, and condition flags:

```bash
indemn role add-watch underwriter --entity Submission --on created \
  --when '{"field": "lob", "op": "in", "value": ["GL", "WC"]}'
```

This means: when a Submission is created and its `lob` is GL or WC, actors with the `underwriter` role get a message.

### Condition operators

| Operator | Example | Meaning |
|----------|---------|---------|
| `equals` | `{"field": "lob", "op": "equals", "value": "GL"}` | Exact match |
| `in` | `{"field": "lob", "op": "in", "value": ["GL", "WC"]}` | Value in list |
| `gte` | `{"field": "followup_count", "op": "gte", "value": 2}` | Greater than or equal |

Conditions can be combined with `all` (AND) or `any` (OR):

```json
{
  "all": [
    {"field": "status", "op": "equals", "value": "open"},
    {"field": "followup_count", "op": "gte", "value": 2}
  ]
}
```

### Creating a role with watches from scratch

If the role doesn't exist yet, create it with watches inline:

```bash
indemn role create --data '{
  "name": "underwriter",
  "permissions": {"read": ["Submission", "Carrier"], "write": ["Submission"]},
  "watches": [
    {
      "entity_type": "Submission",
      "event": "created",
      "conditions": {"field": "lob", "op": "in", "value": ["GL", "WC"]}
    },
    {
      "entity_type": "Submission",
      "event": "transitioned:triaging"
    }
  ]
}'
```

### View all wiring

```bash
# See every watch on every role -- this is the complete wiring diagram
indemn role list --show-watches
```

---

## Step 6: Activate Capabilities

Capabilities are kernel-provided behaviors that can be activated per-entity. They extend what the entity does without writing custom code.

### Enable a capability

```bash
# Auto-classify: evaluate rules and set a field
indemn entity enable Submission auto_classify \
  --config '{"evaluates": "submission-rules", "sets_field": "lob"}'

# Stale check: flag entities that meet age/condition criteria
indemn entity enable Submission stale_check \
  --config '{
    "when": {
      "all": [
        {"field": "status", "op": "equals", "value": "open"},
        {"field": "last_activity_at", "op": "older_than", "value": "7d"}
      ]
    },
    "sets_field": "is_overdue",
    "sets_value": true
  }'
```

### Computed fields

Computed fields are values derived from other fields via a declarative mapping. Add them at entity creation or later:

```bash
# At creation time -- include computed_fields in the definition
indemn entity create --data '{
  "name": "Submission",
  "fields": { ... },
  "computed_fields": {
    "ball_holder": {
      "source_field": "status",
      "mapping": {
        "received": "queue",
        "triaging": "underwriter",
        "quoted": "agent",
        "declined": "closed",
        "bound": "closed",
        "expired": "closed"
      }
    }
  }
}'

# Later via capability enable
indemn entity enable Submission computed_fields \
  --config '{"ball_holder": {"source_field": "status", "mapping": {"received": "queue", "triaging": "underwriter"}}}'
```

Computed fields evaluate inside `save_tracked()` before the MongoDB write -- they are always current.

---

## Step 7: Test End-to-End

Run through the full lifecycle to verify everything works together.

```bash
# 1. Create an entity
indemn submission create --data '{
  "title": "Test GL Submission",
  "named_insured": "Acme Corp",
  "lob": "GL"
}'
# Note the returned ID

# 2. Check the audit trail (changes collection)
indemn trace entity Submission <id>

# 3. Check that watches fired (messages were created for matching roles)
indemn queue stats

# 4. Transition the entity and verify the cascade
indemn submission transition <id> --to triaging --reason "Initial review"

# 5. Trace the full execution tree by correlation ID
indemn trace cascade <correlation_id>

# 6. Verify the computed field updated (if configured)
indemn submission get <id>
# ball_holder should now be "underwriter"
```

### What to look for

- `indemn trace entity` shows a unified timeline of changes and messages for one entity
- `indemn trace cascade` shows the full execution tree triggered by a single event
- `indemn queue stats` shows pending/processing/dead-letter counts per role -- work should be flowing to the right roles
- State transitions should be enforced: attempting an invalid transition returns an error

---

## Modifying an Existing Entity

Entities evolve. The OS supports adding fields, removing fields, and extending the state machine without data migration.

### Add a field

```bash
indemn entity modify Submission \
  --add-field '{"priority": {"type": "str", "enum_values": ["Low", "Medium", "High"], "default": "Medium"}}'
```

Existing documents are unaffected -- the new field will have its default value when accessed. No migration needed.

**Note**: after modifying a definition, the API needs a restart to pick up changes (the CLI discovers entity definitions on each invocation, so it reflects changes immediately).

### Remove a field

```bash
indemn entity modify Submission --remove-field legacy_notes
```

This removes the field from the definition. Existing documents retain the data in MongoDB but the field no longer appears in the API, CLI, or UI.

### Run a migration (rename, backfill, cleanup)

For more complex changes, use the migrate command:

```bash
# Rename a field (dry run first)
indemn entity migrate Submission --rename "old_field new_field" --dry-run

# Execute the rename
indemn entity migrate Submission --rename "old_field new_field" --no-dry-run

# Add a field with backfill
indemn entity migrate Submission \
  --add-field '{"priority": {"type": "str", "default": "Medium"}}' \
  --no-dry-run --batch-size 500

# Remove a field and clean up existing documents
indemn entity migrate Submission --remove-field legacy_notes --no-dry-run
```

Migrations default to dry-run mode. Always dry-run first to see what would change.

---

## Common Patterns

### Reference entities (lookup tables)

Entities with no lifecycle -- just data that other entities reference. No state machine needed.

```bash
indemn entity create --data '{
  "name": "Carrier",
  "collection_name": "carriers",
  "fields": {
    "name": {"type": "str", "required": true, "unique": true},
    "rating": {"type": "str", "enum_values": ["A++", "A+", "A", "A-", "B++", "B+"]},
    "admitted": {"type": "bool", "default": true},
    "lines_of_business": {"type": "list", "default": []}
  }
}'
```

No `state_machine` field means no lifecycle enforcement. The entity is just a reference record.

### Entities with computed fields

Computed fields derive values from other fields automatically on every save.

```bash
indemn entity create --data '{
  "name": "Deal",
  "collection_name": "deals",
  "fields": {
    "stage": {
      "type": "str",
      "is_state_field": true,
      "default": "prospecting",
      "enum_values": ["prospecting", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"]
    },
    "amount": {"type": "decimal"},
    "probability": {"type": "int"}
  },
  "state_machine": {
    "prospecting": ["qualified"],
    "qualified": ["proposal", "closed_lost"],
    "proposal": ["negotiation", "closed_lost"],
    "negotiation": ["closed_won", "closed_lost"],
    "closed_won": [],
    "closed_lost": []
  },
  "computed_fields": {
    "probability": {
      "source_field": "stage",
      "mapping": {
        "prospecting": 10,
        "qualified": 25,
        "proposal": 50,
        "negotiation": 75,
        "closed_won": 100,
        "closed_lost": 0
      }
    }
  }
}'
```

### Entities with flexible data

When entity instances need different fields depending on context (e.g., a Submission's data varies by insurance product).

```bash
indemn entity create --data '{
  "name": "Application",
  "collection_name": "applications",
  "fields": {
    "product_id": {
      "type": "objectid",
      "is_relationship": true,
      "relationship_target": "Product",
      "required": true
    },
    "status": {"type": "str", "is_state_field": true, "default": "draft"}
  },
  "state_machine": {
    "draft": ["submitted"],
    "submitted": ["approved", "rejected"],
    "approved": [],
    "rejected": []
  },
  "flexible_data": {
    "schema_source": "product_id",
    "schema_field": "form_schema"
  }
}'
```

With `schema_source: "product_id"`, the kernel reads the JSON Schema from the related Product's `form_schema` field and validates the Application's `data` dict against it on every save. Different products get different validation rules -- no code changes needed.

Self-contained schemas are also supported:

```bash
indemn entity create --data '{
  "name": "Submission",
  "fields": { ... },
  "flexible_data": {
    "schema_source": "self",
    "schema_field": "data_schema",
    "data_schema": {
      "type": "object",
      "properties": {
        "effective_date": {"type": "string", "format": "date"},
        "coverage_limit": {"type": "number"}
      },
      "required": ["effective_date"]
    }
  }
}'
```

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `"Reserved entity name"` error | Entity name collides with a kernel entity (Organization, Actor, Role, Integration, Attention, Runtime, Session) | Rename the entity |
| State machine doesn't allow a transition | The target state is not in the allowed list for the current state | Check the state machine definition: `indemn entity get Submission` |
| Watch not firing | Role doesn't have a matching watch, or the watch conditions don't match the event | Check `indemn role list --show-watches` and verify the event type and conditions |
| API returns 403 | Actor's role lacks permissions for this entity type | Check role permissions: `indemn role get <role_id>` -- the entity name must be in the `read` or `write` permission arrays |
| Update rejects state field change | State field changes must go through the transition endpoint | Use `indemn submission transition <id> --to <state>` instead of `indemn submission update` |
| Enum validation error | Field value not in the `enum_values` list | Check the entity definition for the allowed values |
| Flexible data validation failed | The `data` dict doesn't match the JSON Schema | Check the schema: either `data_schema` on the definition (self-contained) or `form_schema` on the related entity |
| Entity not appearing in CLI | CLI discovers entities by calling the API on each invocation | Verify the API is running and the entity definition was created: `indemn entity list` |
| Entity not appearing in UI | UI reads entity definitions from the `_meta/entities` endpoint | Refresh the browser; verify the definition exists via `indemn entity list` |
| Modified definition not reflected in API | API loads definitions at startup | Restart the API process; the CLI reflects changes immediately |

---

## Further Reading

- [Entity Framework Architecture](../architecture/entity-framework.md) -- full internals: factory, save path, state machine, computed fields, flexible data
- [Watches and Wiring](../architecture/watches-and-wiring.md) -- how changes become work: watch events, conditions, selective emission
- [Domain Modeling Skill](../../.claude/skills/domain-modeling/SKILL.md) -- the full 8-step process for building any domain
- [Rules and Automation](../architecture/rules-and-auto.md) -- deterministic business logic via rules and capabilities
