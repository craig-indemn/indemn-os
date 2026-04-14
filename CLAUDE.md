# Indemn OS — Development Conventions

## Architecture
- Modular monolith. One repo, one Docker image, three entry points.
- Trust boundary: kernel processes (API, QP, TW) have direct MongoDB. Everything else uses the API.
- CLI always in API mode. Never direct MongoDB from CLI.

## Entity Types
- **Kernel entities**: Python classes in `kernel_entities/`. Always available. 7 total: Organization, Actor, Role, Integration, Attention, Runtime, Session.
- **Domain entities**: Defined as data in MongoDB `entity_definitions` collection (per-org). Dynamic classes created at startup.
- Both share BaseEntity. Both get auto-generated CLI, API, and skills.
- Domain entity names MUST NOT collide with kernel entity names.

## How to Add a Kernel Entity
1. Create class in `kernel_entities/` inheriting BaseEntity
2. Add to kernel_models list in `kernel/db.py`
3. Add tests in `tests/`

## How to Add a Domain Entity Definition
Via CLI: `indemn entity create <Name> --fields '...' --state-machine '...'`
Or via seed YAML in `seed/entities/`

## How to Add a Kernel Capability
1. Create module in `kernel/capability/`
2. Register in `kernel/capability/registry.py`
3. Add tests

## The Save Path
ALL entity saves go through `save_tracked()`. This is non-negotiable.
save_tracked() does in one MongoDB transaction:
1. Optimistic concurrency check (version field)
2. Computed field evaluation
3. Flexible data validation
4. Entity write
5. Changes collection record (with hash chain)
6. Watch evaluation → message creation (selective emission)

## Selective Emission Rules
- Entity creation → emits "created"
- State transition → emits "transitioned"
- @exposed method invocation → emits "method_invoked"
- Regular field updates → NO emission
- Priority: creation > transition > method

## OrgScopedCollection
All application queries use find_scoped() / get_scoped() or OrgScopedCollection.
Never use raw Motor collections or unscoped Beanie queries.
org_id comes from contextvars (set by auth middleware).

## Naming
- Python: snake_case
- CLI commands: kebab-case
- MongoDB collections: lowercase plural (submissions, actors, roles)
- Entity class names: PascalCase singular (Submission, Actor, Role)

## Testing
- Unit: `tests/unit/` — no external dependencies, fast
- Integration: `tests/integration/` — uses Atlas dev cluster
- E2E: `tests/e2e/` — full scenarios
- Run: `uv run pytest tests/unit/` or `tests/integration/`

## Local Dev
```bash
docker-compose up        # API + queue processor + Temporal dev
uv run indemn --help     # CLI
```

## Key Patterns
- Pydantic v2: use `model_dump()` not `dict()`
- Beanie: all Document subclasses registered via init_beanie at startup
- Auth context: set via contextvars in middleware, read everywhere
- Watches on roles: the wiring mechanism — "what does this role care about?"
- Rules: two actions only — `set_fields` and `force_reasoning`
- `--auto` pattern: try rules first, return needs_reasoning if no match
