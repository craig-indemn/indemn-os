# Observability Architecture

This document describes the three observability layers of the Indemn OS -- what data is captured, where it lives, how long it is retained, and how to query it. A senior developer who has never seen this system should understand how to debug any issue in the system after reading this document.

---

## Three Data Stores, One Trace ID

Every operation in the system produces data in up to three stores. A single `correlation_id` (which equals the OTEL trace ID) links records across all three.

| Store | What It Contains | Retention | Query Pattern | Primary Use |
|-------|-----------------|-----------|---------------|-------------|
| **Changes collection** | Field-level entity mutations, hash-chained | Years | By entity, by actor, by time range | Regulatory compliance, audit, config history |
| **Message log** | Completed work items (dispatched, processed, dead-lettered) | Months to years | By role, by date, by entity type | Operations, capacity planning, SLA tracking |
| **Trace backend** (OTEL via Grafana Cloud) | Execution paths, spans, timing | Days to weeks | By trace ID, by service, by operation | Debugging, performance analysis |

### Correlation ID

The `correlation_id` is the thread that connects everything:

```
User creates a Submission via CLI
  --> API server generates trace ID (OTEL): trace_abc123
  --> save_tracked() writes change record:     correlation_id: trace_abc123
  --> Watch fires, message created:            correlation_id: trace_abc123
  --> Temporal workflow processes message:      correlation_id: trace_abc123
  --> Associate updates Submission status:     correlation_id: trace_abc123 (causation from parent)
  --> Watch fires again, new message:          correlation_id: trace_abc123
  --> ... entire cascade shares the same ID
```

When a cascade is triggered by an entity change, the `correlation_id` propagates through causation chains. Each message carries `correlation_id` (the root cause) and `causation_id` (the immediate parent), enabling both "show me the full tree" and "show me what caused this specific step" queries.

---

## Changes Collection

The changes collection is the definitive record of what happened to every entity in the system. It is written inside the `save_tracked()` transaction, making it atomically consistent with the entity state.

### Record Structure

```json
{
  "_id": "change_abc123",
  "org_id": "org_xyz",
  "entity_type": "Submission",
  "entity_id": "sub_789",
  "version": 3,
  "event_type": "field_update",
  "changed_fields": {
    "status": {"old": "new", "new": "classified"},
    "classification": {"old": null, "new": "auto_policy"}
  },
  "changed_by": {
    "actor_id": "actor_associate_001",
    "actor_type": "associate",
    "platform_admin_context": null
  },
  "correlation_id": "trace_abc123",
  "causation_id": "msg_parent_456",
  "rule_evaluation": {
    "rules_evaluated": 3,
    "rules_matched": 1,
    "matched_rule_ids": ["rule_classify_auto"],
    "action_applied": "set_fields",
    "execution_time_ms": 12
  },
  "hash": "sha256:a1b2c3d4...",
  "previous_hash": "sha256:e5f6g7h8...",
  "timestamp": "2026-04-22T14:30:00.123Z"
}
```

### Key Fields

| Field | Purpose |
|-------|---------|
| `changed_fields` | Field-level diff with old and new values. Only changed fields are recorded. |
| `changed_by` | Full provenance: actor identity, type, and platform admin context if applicable |
| `correlation_id` | Links to OTEL trace ID and all related changes/messages in this cascade |
| `causation_id` | The specific message or event that caused this change |
| `rule_evaluation` | Embedded trace of rule evaluation for this change (see below) |
| `hash` / `previous_hash` | Tamper-evident hash chain (see below) |

### Tamper-Evident Hash Chain

Each change record contains a SHA-256 hash computed from its content plus the previous record's hash. This creates a blockchain-like chain where modifying or deleting any record breaks the chain from that point forward.

```
Record 1: hash = SHA256(content_1 + "genesis")
Record 2: hash = SHA256(content_2 + hash_1)
Record 3: hash = SHA256(content_3 + hash_2)
...
```

The chain is scoped per entity (each entity has its own independent chain). This means verification can run per-entity without scanning the entire collection.

**Verification:**

```bash
# Verify hash chain integrity for a specific entity
indemn audit verify --entity-type Submission --entity-id sub_789
# Output:
# Submission sub_789: 47 records, chain intact, last verified: 2026-04-22T14:30:00Z

# Verify all entities of a type
indemn audit verify --entity-type Submission
# Output:
# Submission: 1,247 entities checked, 58,392 records, all chains intact

# Verify everything (slow -- runs across all entity types)
indemn audit verify --all
```

If a chain is broken:

```
Submission sub_789: CHAIN BROKEN at record change_abc123
  Expected previous_hash: sha256:e5f6g7h8...
  Found previous_hash: sha256:0000dead...
  Records after break point: 12 (potentially affected)
```

Implementation: `kernel/changes/collection.py::write_change_record()` for writing. `kernel/changes/hash_chain.py::compute_hash()` and `get_previous_hash()` for chain computation. `kernel/cli/audit_commands.py::verify()` for the CLI command.

### Rule Evaluation Traces

Rule evaluation traces are embedded directly in change records, not in a separate audit stream. This is a deliberate simplification: when you look at a change record and ask "why did this field change?", the rule evaluation trace is right there in the same document.

The `rule_evaluation` field captures:
- How many rules were evaluated
- Which rules matched
- What action was applied (`set_fields` or `force_reasoning`)
- Execution time

For `force_reasoning` vetoes (where rules could not determine the outcome and an LLM was consulted), the change record also includes:

```json
"rule_evaluation": {
  "rules_evaluated": 5,
  "rules_matched": 0,
  "action_applied": "force_reasoning",
  "reasoning_model": "claude-sonnet-4-20250514",
  "reasoning_prompt_hash": "sha256:...",
  "execution_time_ms": 2340
}
```

Implementation: `kernel/rule/engine.py::evaluate_rules()` returns the evaluation trace, which is passed to `write_change_record()`.

### Auth Events

Authentication events (login attempts, session lifecycle, MFA events, rate limiting) are written to the changes collection as Session entity changes. They follow the same hash chain and use the same query patterns. See `authentication.md` for the full event catalog.

---

## Message Log

The message log is the cold storage counterpart to the hot message queue. When a message is fully processed (completed, failed, or dead-lettered), it moves from `message_queue` to `message_log`.

### Record Structure

```json
{
  "_id": "msg_abc123",
  "org_id": "org_xyz",
  "entity_type": "Submission",
  "entity_id": "sub_789",
  "event_type": "state_transition",
  "target_role_id": "role_classifier",
  "claimed_by": "actor_associate_001",
  "status": "completed",
  "priority": 5,
  "correlation_id": "trace_abc123",
  "causation_id": "change_parent_456",
  "cascade_depth": 2,
  "created_at": "2026-04-22T14:30:00.123Z",
  "claimed_at": "2026-04-22T14:30:00.456Z",
  "completed_at": "2026-04-22T14:30:02.789Z",
  "processing_time_ms": 2333,
  "result": {"fields_set": ["status", "classification"]},
  "dead_letter_reason": null
}
```

### Query Patterns

```bash
# Messages processed by a role in the last 24 hours
indemn queue stats --role role_classifier --since 24h

# Dead-lettered messages (failed after all retries)
indemn queue stats --status dead_letter --since 7d

# Processing time distribution for a role
indemn queue stats --role role_classifier --metric processing_time --since 7d

# Message throughput by entity type
indemn queue stats --group-by entity_type --since 24h
```

Implementation: `kernel/message/schema.py::MessageLog` for the document model. `kernel/cli/queue_commands.py` for CLI commands.

---

## Trace Backend (OTEL via Grafana Cloud)

The third observability layer is OpenTelemetry spans exported to Grafana Cloud. This provides execution-path visibility with timing, ideal for debugging performance issues and understanding control flow.

### What Gets Instrumented

Instrumentation is built into the kernel -- not sprinkled across application code. Every major kernel operation generates spans automatically.

| Operation | Span Name | Key Attributes |
|-----------|-----------|----------------|
| Entity save | `kernel.entity.save` | `entity_type`, `entity_id`, `version`, `fields_changed` |
| Watch evaluation | `kernel.watch.evaluate` | `watch_count`, `matches`, `messages_created` |
| Rule evaluation | `kernel.rule.evaluate` | `rules_count`, `matched`, `action` |
| Message dispatch | `kernel.message.dispatch` | `message_id`, `target_role`, `dispatch_method` |
| Associate invocation | `kernel.associate.invoke` | `actor_id`, `runtime_id`, `skill_ids` |
| Temporal workflow | `kernel.temporal.workflow` | `workflow_type`, `workflow_id` |
| CLI command | `kernel.cli.command` | `command`, `subcommand`, `args_hash` |
| API request | `kernel.api.request` | `method`, `path`, `status_code`, `latency_ms` |
| Auth operation | `kernel.auth.operation` | `operation_type`, `success`, `actor_id` |
| Integration call | `kernel.integration.call` | `provider`, `operation`, `latency_ms`, `retry_count` |

### Span Hierarchy

A typical entity save produces a span tree like:

```
kernel.api.request (POST /submission)
  kernel.auth.verify_token
  kernel.entity.save
    kernel.entity.version_check
    kernel.entity.computed_fields
    kernel.entity.flexible_validation
    kernel.rule.evaluate
    kernel.changes.write_record
    kernel.watch.evaluate
      kernel.message.create (x2)
  kernel.message.dispatch
    kernel.temporal.start_workflow
```

### OTEL Export

```
Application --> OTLP Exporter --> Grafana Cloud
                (fire-and-forget)
```

The OTEL exporter is configured as fire-and-forget. If Grafana Cloud is unreachable:
- Spans are dropped silently
- No retry queue, no backpressure
- Zero impact on application latency or availability
- The application continues to function identically

This is by design. Observability is a debugging aid, not a business-critical dependency. The changes collection and message log provide durable observability for compliance and operations. Grafana Cloud provides ephemeral observability for debugging.

Implementation: `kernel/observability/tracing.py::init_tracing()` configures the OTEL SDK. `kernel/observability/tracing.py::create_span()` is the instrumentation helper used throughout the kernel. `kernel/observability/correlation.py` propagates `correlation_id` across async boundaries.

---

## CLI Debugging Commands

### Unified Entity Timeline

```bash
# Full timeline for an entity: changes + messages combined chronologically
indemn trace entity Submission sub_789

# Output:
# 2026-04-22T14:29:58Z [CREATED] by actor_human_craig
#   Fields: status=new, source=email, raw_data={...}
#   Correlation: trace_abc123
#
# 2026-04-22T14:30:00Z [MESSAGE] to role_classifier (priority 5)
#   Status: completed in 2.3s by actor_associate_001
#   Correlation: trace_abc123
#
# 2026-04-22T14:30:02Z [UPDATED] by actor_associate_001
#   Fields: status new->classified, classification null->auto_policy
#   Rule: rule_classify_auto matched (set_fields)
#   Correlation: trace_abc123
#
# 2026-04-22T14:30:02Z [MESSAGE] to role_processor (priority 5)
#   Status: completed in 4.1s by actor_associate_002
#   Correlation: trace_abc123

# Filter by time range
indemn trace entity Submission sub_789 --since 2026-04-22T14:00:00Z --until 2026-04-22T15:00:00Z

# Filter by event type
indemn trace entity Submission sub_789 --events changes
indemn trace entity Submission sub_789 --events messages
```

### Cascade Tree

```bash
# Full execution tree from a correlation ID
indemn trace cascade trace_abc123

# Output (tree view):
# trace_abc123
# +-- [14:29:58] Submission sub_789 CREATED by actor_human_craig
# |   +-- [14:30:00] Message msg_001 -> role_classifier
# |   |   +-- [14:30:02] Submission sub_789 UPDATED (status: classified)
# |   |       +-- [14:30:02] Message msg_002 -> role_processor
# |   |       |   +-- [14:30:06] Submission sub_789 UPDATED (status: processed)
# |   |       |       +-- [14:30:06] Message msg_003 -> role_reviewer
# |   |       |           +-- [14:30:06] HumanReview started (timeout: 4h)

# JSON output for programmatic consumption
indemn trace cascade trace_abc123 --format json
```

### Queue Statistics

```bash
# Per-role queue health
indemn queue stats

# Output:
# Role                  Pending  Processing  Dead Letter  Avg Time
# role_classifier            2           1            0     2.1s
# role_processor             0           3            1     4.8s
# role_reviewer              5           0            0      N/A
# role_notifier              0           0            0     0.3s

# Detailed stats for a role
indemn queue stats --role role_processor --since 24h

# Output:
# role_processor (last 24h):
#   Processed: 1,247
#   Failed: 3 (0.24%)
#   Dead lettered: 1
#   Avg processing time: 4.8s
#   P95 processing time: 12.3s
#   P99 processing time: 28.1s
```

### Integration Health

```bash
# Check connectivity for all integrations
indemn integration health

# Output:
# Integration              Provider    Status     Last Check       Latency
# outlook-acme             outlook     healthy    2m ago           120ms
# stripe-billing           stripe      healthy    5m ago           89ms
# twilio-voice             twilio      degraded   1m ago           2,340ms
# salesforce-crm           salesforce  error      12m ago          timeout
#
# 3 healthy, 1 degraded, 1 error

# Detailed health for a specific integration
indemn integration health outlook-acme --detail

# Output:
# outlook-acme (Outlook)
#   Status: healthy
#   Last successful call: 2m ago (120ms)
#   Token expires: 2026-04-22T18:30:00Z (4h remaining)
#   Calls (24h): 847 success, 2 retry, 0 failure
#   Avg latency: 134ms (P95: 289ms)
```

### Audit Verification

```bash
# Verify hash chain integrity
indemn audit verify

# Output:
# Verifying hash chains...
# Organization:  12 entities,    48 records, intact
# Actor:         34 entities,   412 records, intact
# Role:          18 entities,   156 records, intact
# Session:      847 entities, 3,291 records, intact
# Submission:  1,247 entities, 58,392 records, intact
# ...
# Total: 62,299 records verified, all chains intact

# Verify specific entity type
indemn audit verify --entity-type Submission

# Verify specific entity
indemn audit verify --entity-type Submission --entity-id sub_789
```

### Platform Health

```bash
# Overall platform health check
indemn platform health

# Output:
# API Server:        healthy (3 instances, avg latency 12ms)
# MongoDB Atlas:     healthy (replica set, 3 nodes, connections: 47/150)
# Temporal Cloud:    healthy (namespace: indemn-dev, pending workflows: 3)
# Queue Processor:   healthy (last sweep: 2s ago, pending messages: 7)
# Grafana Cloud:     healthy (last export: 5s ago)
# S3:                healthy (bucket: indemn-files)
#
# Overall: HEALTHY
```

---

## Querying Across Stores

The three stores serve different query patterns. Here is how to choose:

| Question | Store | Command |
|----------|-------|---------|
| "What changed on this entity?" | Changes | `indemn trace entity <Type> <id>` |
| "Who changed this field and when?" | Changes | `indemn trace entity <Type> <id> --events changes` |
| "What was the full cascade from this event?" | Changes + Messages | `indemn trace cascade <correlation_id>` |
| "How many messages is this role processing?" | Message Log | `indemn queue stats --role <role>` |
| "Why did this request take 30 seconds?" | OTEL Traces | Grafana Cloud UI, search by trace ID |
| "Is the hash chain intact?" | Changes | `indemn audit verify` |
| "What rules fired on this change?" | Changes | `indemn trace entity` (rule_evaluation embedded in change record) |
| "Are integrations healthy?" | Integration entities + OTEL | `indemn integration health` |

---

## Implementation Files

| File | Responsibility |
|------|----------------|
| `kernel/changes/collection.py` | `write_change_record()` -- writes change records inside `save_tracked()` transaction |
| `kernel/changes/hash_chain.py` | `compute_hash()`, `get_previous_hash()` -- SHA-256 hash chain computation |
| `kernel/message/schema.py` | `MessageLog(Document)` -- completed message storage |
| `kernel/observability/tracing.py` | `init_tracing()`, `create_span()` -- OTEL instrumentation |
| `kernel/observability/correlation.py` | Correlation ID propagation across async boundaries |
| `kernel/observability/logging.py` | `setup_logging()` -- structured JSON logging |
| `kernel/cli/audit_commands.py` | `indemn audit verify` -- hash chain verification CLI |
| `kernel/cli/queue_commands.py` | `indemn queue stats` -- message queue statistics CLI |
| `kernel/cli/events_commands.py` | `indemn events stream` -- real-time event streaming CLI |
| `kernel/api/trace_routes.py` | Trace query API endpoints |
| `kernel/api/health.py` | Health check endpoint |
