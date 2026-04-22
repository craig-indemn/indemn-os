# Adding Watches to Roles

Watches declare what entity changes matter to actors in a given role. When an entity event matches a watch, the system routes a message to the actor's queue. This is how associates know there is work to do and how humans get notified of changes that need attention.

---

## Understanding Watches

Watches live on **roles**, not on individual actors. Every actor assigned a role inherits that role's watches. When an entity event fires and matches a watch's criteria, every actor in the role receives a queue message -- unless the watch is scoped to a specific actor via `field_path`.

The watch system is the primary mechanism for reactive behavior in the kernel. Without watches, associates sit idle and humans get no notifications.

---

## Creating a Role with Watches

When you create a new role, you can define watches inline:

```bash
indemn role create --data '{
  "name": "underwriter",
  "permissions": {
    "read": ["Submission", "Assessment", "Draft"],
    "write": ["Submission", "Assessment"]
  },
  "watches": [{
    "entity_type": "Assessment",
    "event": "created",
    "conditions": {
      "field": "needs_review",
      "op": "equals",
      "value": true
    }
  }]
}'
```

This creates an underwriter role that receives a message whenever an Assessment is created with `needs_review: true`.

---

## Adding Watches to Existing Roles

To add a watch to a role that already exists:

```bash
indemn role add-watch underwriter --entity Submission --on transitioned \
  --when '{"field": "status", "op": "equals", "value": "ready_for_review"}'
```

This appends the watch to the role's existing watch list. Existing watches are not affected.

To view current watches on a role:

```bash
indemn role get underwriter --show-watches
```

To remove a specific watch:

```bash
indemn role remove-watch underwriter --index 2
```

---

## Watch Events

Each watch targets a specific event type. These are the supported events and when to use them:

### `created`

Fires when a new entity of the specified type is created.

**Use when:** An associate or human needs to process every new entity. Example: classify every new email, review every new submission.

```json
{
  "entity_type": "Email",
  "event": "created"
}
```

### `transitioned`

Fires when an entity moves between lifecycle states (e.g., `draft` to `submitted`, `submitted` to `approved`).

**Use when:** Work begins only at a specific stage. Example: underwriters review submissions only after they reach `ready_for_review`.

```json
{
  "entity_type": "Submission",
  "event": "transitioned",
  "conditions": {
    "field": "status",
    "op": "equals",
    "value": "ready_for_review"
  }
}
```

### `fields_changed`

Fires when specific fields on an entity are modified.

**Use when:** A particular data change requires attention. Example: notify when a quote amount changes, or when a risk score is updated.

```json
{
  "entity_type": "Quote",
  "event": "fields_changed",
  "conditions": {
    "field": "changed_fields",
    "op": "contains",
    "value": "premium_amount"
  }
}
```

### `method_invoked`

Fires when a method defined on the entity type is called.

**Use when:** You need to react to an action rather than a state change. Example: an associate should check compliance every time `submit_for_binding` is invoked.

```json
{
  "entity_type": "Policy",
  "event": "method_invoked",
  "conditions": {
    "field": "method_name",
    "op": "equals",
    "value": "submit_for_binding"
  }
}
```

### `deleted`

Fires when an entity is deleted (soft or hard).

**Use when:** Cleanup or audit is needed after removal. Example: archive related documents when a submission is deleted.

```json
{
  "entity_type": "Submission",
  "event": "deleted"
}
```

---

## Condition Operators

Conditions filter which events match a watch. Every condition has `field`, `op`, and `value`.

### Comparison Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `equals` | Exact match | `{"field": "status", "op": "equals", "value": "active"}` |
| `not_equals` | Not equal | `{"field": "priority", "op": "not_equals", "value": "low"}` |
| `gt` | Greater than | `{"field": "score", "op": "gt", "value": 80}` |
| `gte` | Greater than or equal | `{"field": "premium", "op": "gte", "value": 10000}` |
| `lt` | Less than | `{"field": "days_open", "op": "lt", "value": 5}` |
| `lte` | Less than or equal | `{"field": "risk_score", "op": "lte", "value": 3}` |

### String Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `contains` | Substring match | `{"field": "subject", "op": "contains", "value": "renewal"}` |
| `starts_with` | Prefix match | `{"field": "email", "op": "starts_with", "value": "admin@"}` |
| `ends_with` | Suffix match | `{"field": "sender_domain", "op": "ends_with", "value": "@carrier.com"}` |
| `matches` | Regex match | `{"field": "ref_number", "op": "matches", "value": "^POL-\\d{6}$"}` |

### Collection Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `in` | Value is in list | `{"field": "state", "op": "in", "value": ["CA", "TX", "FL"]}` |
| `not_in` | Value is not in list | `{"field": "classification", "op": "not_in", "value": ["spam", "duplicate"]}` |
| `exists` | Field is present and non-null | `{"field": "assigned_to", "op": "exists", "value": true}` |

### Temporal Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `older_than` | Entity field is older than duration | `{"field": "created_at", "op": "older_than", "value": "24h"}` |
| `within` | Entity field is within duration | `{"field": "updated_at", "op": "within", "value": "1h"}` |

Duration values: `30m`, `2h`, `7d`, `1w`.

---

## Composing Conditions

For complex filtering, combine conditions with `all`, `any`, and `not`.

### `all` -- Every condition must match (AND)

```json
{
  "entity_type": "Submission",
  "event": "created",
  "conditions": {
    "all": [
      {"field": "line_of_business", "op": "equals", "value": "commercial_auto"},
      {"field": "premium_estimate", "op": "gte", "value": 50000},
      {"field": "state", "op": "in", "value": ["CA", "TX", "FL"]}
    ]
  }
}
```

### `any` -- At least one condition must match (OR)

```json
{
  "entity_type": "Email",
  "event": "created",
  "conditions": {
    "any": [
      {"field": "sender_domain", "op": "ends_with", "value": "@carrier.com"},
      {"field": "subject", "op": "contains", "value": "URGENT"},
      {"field": "priority", "op": "equals", "value": "high"}
    ]
  }
}
```

### `not` -- Negate a condition

```json
{
  "entity_type": "Email",
  "event": "created",
  "conditions": {
    "all": [
      {"field": "classification", "op": "not_equals", "value": "spam"},
      {"not": {"field": "sender_domain", "op": "ends_with", "value": "@noreply.com"}}
    ]
  }
}
```

### Nested composition

```json
{
  "entity_type": "Submission",
  "event": "transitioned",
  "conditions": {
    "all": [
      {"field": "status", "op": "equals", "value": "ready_for_review"},
      {
        "any": [
          {"field": "premium_estimate", "op": "gte", "value": 100000},
          {"field": "risk_flags", "op": "exists", "value": true}
        ]
      },
      {"not": {"field": "auto_approved", "op": "equals", "value": true}}
    ]
  }
}
```

This matches: submission transitioned to `ready_for_review`, AND (premium >= 100k OR has risk flags), AND was not auto-approved.

---

## Scoped Watches

### Ownership routing with `field_path`

By default, a watch sends messages to all actors in the role. Use `field_path` to route only to the actor whose ID matches a field on the entity:

```json
{
  "entity_type": "Submission",
  "event": "transitioned",
  "scope": {"type": "field_path", "path": "assigned_underwriter"},
  "conditions": {
    "field": "status",
    "op": "equals",
    "value": "info_received"
  }
}
```

Only the underwriter whose actor ID matches `submission.assigned_underwriter` receives the message. Other underwriters are not notified.

### Real-time scoping with `active_context`

For live interactions (e.g., a customer is in a chat session), use `active_context` to route only to the actor handling that conversation:

```json
{
  "entity_type": "Message",
  "event": "created",
  "scope": {"type": "active_context", "traverses": "conversation_id"},
  "conditions": {
    "field": "requires_response",
    "op": "equals",
    "value": true
  }
}
```

This sends the message only to the actor currently active in that conversation, not to every actor in the role.

---

## Testing Watches

### 1. Check queue stats

After creating a watch, trigger the event and verify messages appear:

```bash
# See how many messages are queued per actor
indemn queue stats

# Filter to a specific role
indemn queue stats --role underwriter
```

### 2. Trace an entity

Watch the event flow for a specific entity:

```bash
# See all events fired for an entity
indemn trace entity <entity-id>

# Watch events in real time
indemn trace entity <entity-id> --follow
```

### 3. Trigger a test event

Create or transition an entity that should match the watch, then verify:

```bash
# Create a test entity
indemn assessment create --data '{"needs_review": true, "submission_id": "test-123"}'

# Check queue immediately
indemn queue stats --role underwriter
```

### 4. Verify message content

Peek at what the actor receives:

```bash
indemn queue peek --role underwriter --limit 1
```

---

## Common Patterns

### Notify owner when entity transitions

```json
{
  "entity_type": "Submission",
  "event": "transitioned",
  "field_path": "assigned_to",
  "conditions": {
    "field": "status",
    "op": "in",
    "value": ["info_needed", "revision_required", "approved", "declined"]
  }
}
```

### Notify all underwriters on creation

```json
{
  "entity_type": "Submission",
  "event": "created",
  "conditions": {
    "field": "line_of_business",
    "op": "in",
    "value": ["commercial_auto", "general_liability", "workers_comp"]
  }
}
```

No `field_path` means all actors in the role receive it.

### Only notify the handler during a live conversation

```json
{
  "entity_type": "CustomerMessage",
  "event": "created",
  "active_context": "conversation_id"
}
```

### Escalation on stale entities

```json
{
  "entity_type": "Submission",
  "event": "fields_changed",
  "conditions": {
    "all": [
      {"field": "status", "op": "equals", "value": "pending_review"},
      {"field": "updated_at", "op": "older_than", "value": "48h"}
    ]
  }
}
```

### High-value new business alert

```json
{
  "entity_type": "Submission",
  "event": "created",
  "conditions": {
    "all": [
      {"field": "premium_estimate", "op": "gte", "value": 250000},
      {"field": "is_new_business", "op": "equals", "value": true}
    ]
  }
}
```

---

## Troubleshooting

### Watch not firing

1. **Check the watch exists on the role:**
   ```bash
   indemn role get <role-name> --watches
   ```

2. **Verify the event type matches.** If you created a `transitioned` watch but the entity was just updated (fields changed, no state transition), it will not fire. Use `fields_changed` instead.

3. **Check conditions carefully.** A condition with `"op": "equals", "value": "true"` (string) will not match a boolean `true`. Types must match.

4. **Trace the entity to see what events fired:**
   ```bash
   indemn trace entity <entity-id>
   ```
   If the event fired but no watch matched, the conditions are wrong. If the event did not fire, the entity change did not produce the expected event type.

### Wrong actor receiving messages

1. **Check `field_path`.** If omitted, all actors in the role receive the message. Add `field_path` to route to a specific actor.

2. **Verify the field value.** The `field_path` field on the entity must contain the actor's ID, not their name or email.
   ```bash
   indemn entity get <entity-id> --field assigned_underwriter
   ```

3. **Check role assignment.** The receiving actor must be assigned the role the watch is on:
   ```bash
   indemn actor get <actor-id> --roles
   ```

### Too many messages

1. **Tighten conditions.** Add more specific filters to reduce matches.

2. **Use `field_path`** to route only to the relevant actor instead of broadcasting.

3. **Combine events.** If an entity fires multiple `fields_changed` events in rapid succession, consider watching for `transitioned` on the final state instead.

4. **Check for duplicate watches.** Multiple watches on the same role matching the same event will produce multiple messages:
   ```bash
   indemn role get <role-name> --watches
   ```
