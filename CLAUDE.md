# Indemn OS — The Operating System for Insurance

Define an entity and it auto-generates its API, CLI, documentation (skill), permissions, and UI. AI agents are a channel into the platform — they use the CLI like any other client.

## Architecture

Modular monolith. Trust boundary splits kernel (direct MongoDB) from everything else (API access only).

**Inside trust boundary** (share `indemn-kernel` image, 3 entry points):
- API Server: `python -m kernel.api.app`
- Queue Processor: `python -m kernel.queue_processor`
- Temporal Worker: `python -m kernel.temporal.worker`

**Outside trust boundary** (authenticate via API, use CLI subprocess):
- Harnesses: `harnesses/{async,chat,voice}-deepagents/`
- Base UI: `ui/`
- CLI package: `indemn_os/`

## Entity Types

**Kernel entities** (7): Python classes in `kernel_entities/`. Always available.
Organization, Actor, Role, Integration, Attention, Runtime, Session.

**Domain entities**: Defined as data in MongoDB `entity_definitions` collection. Per-org. Dynamic classes created at startup via `kernel/entity/factory.py`.

Both auto-generate: API endpoints, CLI commands, skill documentation. Domain entity names MUST NOT collide with kernel entity names.

## The Self-Evidence Property

When you define an entity, you don't then build its API, documentation, CLI, or UI separately. All of that exists the moment the entity is defined. Building on the OS is defining what the system IS. The rest follows.

## How to Define a Domain Entity

```bash
indemn entity create --data '{
  "name": "Submission",
  "collection_name": "submissions",
  "fields": {
    "title": {"type": "str", "required": true},
    "status": {"type": "str", "default": "received", "is_state_field": true},
    "carrier_id": {"type": "objectid", "is_relationship": true, "relationship_target": "Carrier"}
  },
  "state_machine": {
    "received": ["triaging"],
    "triaging": ["quoted", "declined"],
    "quoted": ["bound", "expired"]
  }
}'
```

This creates: API routes (`/api/submissions/`), CLI commands (`indemn submission list/get/create/update/transition`), skill documentation (markdown with fields, lifecycle, commands).

**Field types**: str, int, float, decimal, bool, datetime, date, objectid, list, dict
**Field options**: required, default, unique, indexed, enum_values, is_state_field, is_relationship, relationship_target

## How to Set Up Watches (the wiring mechanism)

Watches live on Roles. When an entity change matches a watch condition, a message is created for actors in that role.

```bash
indemn role create --data '{
  "name": "email_classifier",
  "permissions": {"read": ["Email", "Submission"], "write": ["Email"]},
  "watches": [{
    "entity_type": "Email",
    "event": "created",
    "conditions": {"field": "status", "op": "equals", "value": "received"}
  }]
}'
```

**Watch events**: created, transitioned, method_invoked, fields_changed, deleted
**Condition operators**: equals, not_equals, contains, starts_with, ends_with, gt, gte, lt, lte, in, not_in, matches, exists, older_than, within
**Composition**: `{"all": [...]}`, `{"any": [...]}`, `{"not": {...}}`
**Scope**: `field_path` (ownership routing) or `active_context` (real-time routing via Attention)

## How to Set Up an Associate

```bash
# 1. Create a skill (markdown behavioral instructions)
indemn skill create --name email-classifier --content-from-file skills/email-classifier.md

# 2. Create a Runtime (or use existing)
indemn runtime create --name async-dev --kind async_worker --framework deepagents

# 3. Create the associate actor
indemn actor create --type associate --name "Email Classifier" \
  --mode hybrid --runtime-id <runtime_id> \
  --role email_classifier --skills email-classifier

# 4. Activate
indemn actor transition <actor_id> --to active
```

**Associate modes**: deterministic (rules only), reasoning (LLM), hybrid (rules first, LLM fallback)
**The `--auto` pattern**: try configured rules deterministically; if `needs_reasoning: true`, LLM fallback via associate skill.

## How to Set Up Rules

Rules are per-org condition → action patterns. Two actions only: `set_fields` (deterministic) and `force_reasoning` (veto — send to LLM).

```bash
indemn rule create --data '{
  "entity_type": "Email",
  "capability": "auto_classify",
  "name": "known-carrier",
  "conditions": {"field": "sender_domain", "op": "in", "value": ["usli.com", "markel.com"]},
  "action": "set_fields",
  "sets": {"classification": "carrier_response"},
  "priority": 200
}'
```

Rules belong to RuleGroups with lifecycle (draft → active → archived). Only active rules in active groups evaluate.

## How to Set Up Integrations

```bash
indemn integration create --name "Outlook" --system-type email --provider outlook --owner-type org
indemn integration set-credentials <id> --secret-ref indemn/prod/integrations/outlook-oauth
indemn integration health  # Test connectivity
```

Adapters: `kernel/integration/adapters/`. Current: Outlook, Stripe.
Credential resolution: actor personal → owner (`owner_actor_id`) → org-level (role-based).
Credentials NEVER in MongoDB — always AWS Secrets Manager via `secret_ref`.

## The Save Path — Non-Negotiable

ALL entity saves go through `save_tracked()`. One MongoDB transaction:
1. Optimistic concurrency check (version field)
2. Computed field evaluation
3. Flexible data validation
4. Entity write
5. Changes collection record (with hash chain for tamper evidence)
6. Watch evaluation → message creation (selective emission)

**Selective emission**: only creation + state transitions + @exposed methods generate messages. NOT every field change.

## Debugging

```bash
indemn trace entity <EntityType> <entity_id>     # Unified timeline: changes + messages
indemn trace cascade <correlation_id>             # Full execution tree
indemn queue stats                                # Pending/processing/dead-letter per role
indemn integration health                         # Test adapter connectivity
indemn audit verify                               # Hash chain integrity
indemn actor list --type associate --status active # Running associates
indemn runtime list                               # Deployed runtimes
```

## The 8-Step Domain Modeling Process

How to build anything on the OS:

1. **Understand the business** — narrative, workflows, people, systems, pain
2. **Identify entities** — nouns, fields, lifecycle (state machine), relationships
3. **Identify roles and actors** — who participates, permissions, watches
4. **Define rules and configuration** — per-org business logic, lookups, capability activation
5. **Write skills** — associate behavioral instructions in markdown
6. **Set up integrations** — external system connections, adapters, credentials
7. **Test in staging** — staging org, realistic data, validate end-to-end
8. **Deploy and tune** — production, monitor, add rules for patterns LLM keeps handling

**The universal pattern**: Entry point → creates entity → watches fire → associates process → entity state changes → more watches → eventually reaches human checkpoint or final state.

## OrgScopedCollection

All queries use `find_scoped()` / `get_scoped()`. Never raw Motor. org_id from contextvars (auth middleware).

## Naming

- Python: snake_case
- CLI commands: kebab-case
- MongoDB collections: lowercase plural
- Entity classes: PascalCase singular
- API routes: /api/{collection_name}/

## Testing

- Unit: `tests/unit/` — no external deps
- Integration: `tests/integration/` — Atlas dev cluster
- E2E: `tests/e2e/` — full scenarios
- Run: `uv run pytest tests/unit/`

## Key Files

| Path | What |
|---|---|
| `kernel/entity/base.py` | BaseEntity (KernelBaseEntity + DomainBaseEntity) |
| `kernel/entity/factory.py` | Dynamic class creation from EntityDefinition |
| `kernel/entity/save.py` | save_tracked() — the ONE save path |
| `kernel/entity/state_machine.py` | State machine enforcement |
| `kernel/api/registration.py` | Auto-generated CRUD + transition + @exposed routes |
| `kernel/watch/evaluator.py` | Condition evaluation (shared by watches + rules) |
| `kernel/rule/engine.py` | Rule evaluation with group status |
| `kernel/message/emit.py` | Watch evaluation → message creation |
| `kernel/temporal/workflows.py` | ProcessMessage, HumanReview, BulkExecute workflows |
| `kernel/temporal/activities.py` | Kernel activities (claim, load_actor, complete, fail, bulk) |
| `kernel/skill/generator.py` | Auto-generate entity skill markdown |
| `kernel/integration/dispatch.py` | Adapter resolution + retry |
| `kernel_entities/` | 7 kernel entity classes |
| `indemn_os/` | CLI package (`indemn` binary) |
| `harnesses/` | Async, chat, voice harness images |

## Key Patterns

- Pydantic v2: use `model_dump()` not `dict()`
- Beanie: all Document subclasses registered via init_beanie at startup
- Auth context: set via contextvars in middleware, read everywhere
- Watches on roles: "what does this role care about?"
- Rules: two actions only — `set_fields` and `force_reasoning`
- `--auto` pattern: try rules first, return needs_reasoning if no match
- Harnesses use CLI subprocess for ALL OS operations — no direct kernel imports
