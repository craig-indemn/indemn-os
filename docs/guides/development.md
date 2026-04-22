# Development Guide

Setup, testing, deployment, and conventions for working on the Indemn OS kernel and supporting services.

---

## 1. Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.12+ | `brew install python@3.12` |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | 20+ | `brew install node` |
| Docker | latest | Docker Desktop or `brew install --cask docker` |
| gh CLI | latest | `brew install gh` then `gh auth login` |
| AWS CLI | v2 | `brew install awscli` then `aws configure` |
| Temporal CLI | latest | `brew install temporal` |

Verify everything is installed:

```bash
python3 --version && uv --version && node --version && docker --version && gh --version && aws --version && temporal --version
```

---

## 2. Local Development Setup

### Clone and install

```bash
git clone https://github.com/craig-indemn/indemn-os.git
cd indemn-os
uv sync
```

### Start all services

You need six terminal windows (or use tmux/tabs). Each service runs in its own process:

**Terminal 1: API Server**
```bash
uvicorn kernel.api.app:create_app --factory --reload --port 8000
```

The API server handles all inbound HTTP requests -- CLI commands, webhooks, and the UI. `--reload` watches for file changes during development.

**Terminal 2: Queue Processor**
```bash
python -m kernel.queue_processor
```

The queue processor routes messages to actors, manages claim/release, and handles retry logic. It reads from MongoDB and dispatches to active actors.

**Terminal 3: Temporal (local dev server)**
```bash
temporal server start-dev --port 7233
```

Temporal handles durable workflows -- multi-step processes that survive restarts. The local dev server includes a web UI at `http://localhost:8233`.

**Terminal 4: Temporal Worker**
```bash
python -m kernel.temporal.worker
```

The worker picks up Temporal workflows and executes them. Must be running for any workflow-based operations (cascades, scheduled tasks, long-running processes).

**Terminal 5: UI**
```bash
cd ui && npm install && npm run dev
```

The management UI runs on `http://localhost:5173`. Provides entity browsing, actor management, queue inspection, and trace visualization.

**Terminal 6 (optional): Chat Harness**
```bash
cd harnesses/chat-deepagents && python harness.py
```

The chat harness provides a terminal-based interface for testing conversational interactions with associates. Useful for testing skill behavior interactively.

### Verify everything is running

```bash
# API health
curl http://localhost:8000/health

# Temporal UI
open http://localhost:8233

# App UI
open http://localhost:5173
```

---

## 3. Environment Variables

Create a `.env` file in the project root. Required variables:

```bash
# MongoDB (Atlas dev cluster)
MONGODB_URI=mongodb+srv://...

# AWS (for Secrets Manager, Parameter Store)
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1

# Temporal
TEMPORAL_HOST=localhost:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TASK_QUEUE=indemn-kernel

# Auth
JWT_SIGNING_KEY=...  # Generate with: openssl rand -hex 32

# API
INDEMN_API_URL=http://localhost:8000

# Observability (optional for local dev)
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
OTEL_SERVICE_NAME=indemn-kernel
```

The `.env` file is gitignored. Never commit credentials.

For dev cluster credentials, check AWS Secrets Manager:

```bash
aws secretsmanager get-secret-value --secret-id indemn/dev/shared/mongodb --query SecretString --output text
```

---

## 4. Running Tests

Tests are organized into three tiers by what they depend on:

### Unit tests

No external dependencies. Fast. Run these constantly during development.

```bash
uv run pytest tests/unit/
```

These test individual functions, models, and logic in isolation. Mocked dependencies.

### Integration tests

Require a live Atlas dev cluster and Temporal. Run before pushing.

```bash
uv run pytest tests/integration/
```

These test database queries, adapter connectivity, queue operations, and Temporal workflows against real infrastructure.

### End-to-end tests

Full scenario tests. Create entities, trigger watches, verify associate behavior, check cascades.

```bash
uv run pytest tests/e2e/
```

These simulate real workflows from start to finish. Slower but catch interaction bugs that unit and integration tests miss.

### Running a specific test

```bash
# Single file
uv run pytest tests/unit/test_entity_model.py

# Single test
uv run pytest tests/unit/test_entity_model.py::test_validation_rejects_invalid_status

# With output
uv run pytest tests/unit/ -v --tb=short
```

### Test conventions

- Unit tests mock all external dependencies
- Integration tests use the dev cluster (never production)
- E2E tests clean up after themselves (create and delete test entities)
- Test files mirror the source structure: `kernel/entity/model.py` -> `tests/unit/entity/test_model.py`

---

## 5. Code Conventions

### Pydantic v2

All models use Pydantic v2. This means:

```python
# Correct
data = model.model_dump()
model = MyModel.model_validate(data)

# Wrong (Pydantic v1 patterns)
data = model.dict()        # deprecated
model = MyModel.parse_obj(data)  # deprecated
```

Use `model_dump(exclude_none=True)` when serializing for API responses. Use `model_dump(by_alias=True)` when writing to MongoDB (which uses `_id` not `id`).

### Beanie ODM

All MongoDB documents use Beanie as the ODM:

```python
from beanie import Document

class Submission(Document):
    org_id: str
    status: str
    line_of_business: str

    class Settings:
        name = "submissions"  # MongoDB collection name
```

### Auth context via contextvars

The current actor and org are set in a context variable at the API layer. All downstream code accesses them via:

```python
from kernel.auth.context import get_current_actor, get_current_org

actor = get_current_actor()
org = get_current_org()
```

Never pass actor/org through function signatures when the context variable is available. The context is set by middleware and is available throughout the request lifecycle.

### Scoped queries

All database queries must be scoped to the current org. Never use raw `find()` or `get()`:

```python
# Correct -- scoped to current org
submissions = await Submission.find_scoped({"status": "active"}).to_list()
submission = await Submission.get_scoped(submission_id)

# Wrong -- no org scoping, returns data across all orgs
submissions = await Submission.find({"status": "active"}).to_list()
submission = await Submission.get(submission_id)
```

`find_scoped()` and `get_scoped()` automatically add the org filter from the auth context. This is the primary mechanism for tenant isolation.

### Error handling

Use kernel exception types, not generic Python exceptions:

```python
from kernel.exceptions import EntityNotFound, PermissionDenied, ValidationError

# Raise specific exceptions
raise EntityNotFound(entity_type="Submission", entity_id=submission_id)
raise PermissionDenied(action="write", entity_type="Submission")
raise ValidationError(field="premium", message="must be positive")
```

These are caught by API middleware and converted to appropriate HTTP status codes.

---

## 6. Adding a New Kernel Capability

A kernel capability is a new feature of the core system -- a new entity method, a new queue behavior, a new auth mechanism, etc.

### Where to put it

```
kernel/
  entity/         # Entity definitions, lifecycle, save_tracked(), state machines
  message/        # Message schema, bus, emit, dispatch
  watch/          # Watch evaluation, cache, scope resolution
  rule/           # Rule engine, lookups, validation
  capability/     # Kernel capabilities (auto_classify, stale_check, etc.)
  auth/           # Authentication, authorization, session management
  changes/        # Changes collection, hash chain
  integration/    # External system adapters, credentials, dispatch
  skill/          # Skill generation, integrity verification
  temporal/       # Workflow definitions, activities
  scoping/        # Org isolation (OrgScopedCollection), platform access
  observability/  # OTEL tracing, correlation, logging
  api/            # HTTP endpoints, middleware, route files
  cli/            # CLI commands, client, registration
```

Pick the directory that matches the capability's domain.

### Pattern

1. **Write the model/logic** in the appropriate kernel module
2. **Add API endpoints** if the capability needs HTTP access
3. **Add CLI support** in the CLI layer if it should be accessible from `indemn` commands
4. **Write unit tests** first, then integration tests
5. **Update entity types** if the capability adds new fields or methods to entities

### Registration

New API endpoints register as route files in `kernel/api/`:

```python
# kernel/api/your_feature_routes.py
from fastapi import APIRouter

router = APIRouter(prefix="/your-feature", tags=["your-feature"])

@router.get("/{id}")
async def get_thing(id: str):
    ...
```

Then include in `kernel/api/app.py`:

```python
from kernel.api.routes.your_feature import router as your_feature_router
app.include_router(your_feature_router)
```

---

## 7. Adding a New Adapter

See the detailed walkthrough in [Adding Integrations, Section 9](adding-integrations.md#9-building-a-new-adapter).

In summary:

1. Create `kernel/integration/adapters/your_adapter.py`
2. Implement `IntegrationAdapter` base class
3. Register in `kernel/integration/registry.py`
4. Write unit tests in `tests/unit/integration/test_your_adapter.py`
5. Write integration tests in `tests/integration/test_your_adapter.py`
6. Document the credentials schema and any provider-specific setup

---

## 8. Deploying to Railway

The production deployment pipeline:

```
push to main -> GitHub Actions CI -> Railway auto-deploy
```

### The flow

1. **Push to `main`**: All code merges go through PRs. Direct pushes to main are blocked.
2. **GitHub Actions**: Runs unit and integration tests. If tests fail, the deploy is blocked.
3. **Railway**: On successful CI, Railway detects the push and deploys automatically.

### Manual deploy (emergency only)

```bash
# Check current deployment status
railway status

# Force redeploy from current main
railway up
```

### Environment variables on Railway

Railway environment variables are managed separately from local `.env`. To update:

```bash
# View current vars
railway variables

# Set a variable
railway variables set MONGODB_URI=mongodb+srv://...
```

Production secrets should be set via Railway's dashboard or CLI, referencing AWS Secrets Manager where possible.

### Rollback

```bash
# List recent deployments
railway deployments

# Roll back to a specific deployment
railway rollback <deployment-id>
```

---

## 9. Entity Type Deployment

Entity types (Submission, Assessment, Email, etc.) are defined as documents in MongoDB's `entity_definitions` collection. Deploying a new or modified entity type:

1. **Define via CLI**: Use `indemn entity create --data '{...}'` to write the definition to MongoDB
2. **Rolling restart**: The API server reads entity definitions from the database on startup and creates dynamic classes via `kernel/entity/factory.py`
3. **Verify**: The auto-generated CLI, API, and skill are immediately available

```bash
# Create entity definition in the database
indemn entity create --data '{
  "name": "Submission",
  "collection_name": "submissions",
  "fields": {...},
  "state_machine": {...}
}'

# Verify auto-generation
indemn submission list
indemn skill get Submission
```

For changes to take effect on running services, a rolling restart is needed:

```bash
# Railway handles this automatically on deploy
# For manual restart:
railway restart
```

Entity type changes that add optional fields are backwards-compatible and do not require migration. Changes that add required fields, rename fields, or change types require a data migration script.

---

## 10. Parallel AI Sessions

When multiple developers (or AI sessions) work simultaneously, isolation matters.

### Git worktrees for code isolation

Each session works in its own worktree to avoid merge conflicts:

```bash
# Create an isolated worktree for a feature
git worktree add .claude/worktrees/my-feature -b my-feature

# Work in the worktree
cd .claude/worktrees/my-feature

# When done, merge and clean up
git checkout main
git merge my-feature
git worktree remove .claude/worktrees/my-feature
```

### Shared infrastructure is single-session

The dev MongoDB cluster, Temporal namespace, and queue processor are shared. This means:

- **Entity types are global**: If session A modifies an entity type, session B sees the change immediately.
- **Queue messages are global**: An associate activated by session A will process messages created by session B.
- **Temporal workflows are global**: Workflows started by one session are visible to all sessions.

### Conventions for parallel work

1. **Define shared contracts before parallel work.** If two sessions will both touch the same entity type, agree on the schema first.
2. **Use feature flags** for in-progress work that touches shared infrastructure.
3. **Prefix test data** with your session or feature name so you can identify and clean up your data.
4. **Do not modify entity types in parallel.** One session owns entity type changes at a time.

---

## 11. Debugging

### Trace an entity

See every event and state change for a specific entity:

```bash
indemn trace entity <entity-id>
```

Shows: creation, transitions, field changes, method invocations, which actors touched it, and when.

### Trace a cascade

See the full chain reaction triggered by a single event:

```bash
indemn trace cascade <message-id>
```

Shows: the originating event, which watches matched, which messages were sent, which actors claimed them, what actions they took, and any downstream events those actions triggered.

### Platform health

Check the status of all kernel components:

```bash
indemn platform health
```

Reports on: API server, queue processor, Temporal connection, MongoDB connection, active integrations, active associates.

### Common issues

**Associate not processing messages:**
1. Check it is active: `indemn actor get <id>`
2. Check the queue has messages: `indemn queue stats --role <role>`
3. Check the runtime is responding: `indemn runtime test <runtime-id>`
4. Check the Temporal worker is running: `temporal workflow list --namespace default`

**Entity type not recognized:**
1. Verify it was deployed: `indemn entity-type list`
2. Check the API server has restarted since deployment
3. Check for syntax errors in the type definition

**Webhook not arriving:**
1. Verify the integration is active: `indemn integration get <id>`
2. Check the webhook URL is registered in the external system
3. Check the adapter's validation: look for 401 responses in logs
4. Test the webhook manually:
   ```bash
   curl -X POST http://localhost:8000/webhooks/<integration-id>/inbound \
     -H "Content-Type: application/json" \
     -d '{"test": true}'
   ```

**Temporal workflow stuck:**
1. Check the Temporal UI: `open http://localhost:8233`
2. Look for failed activities in the workflow history
3. Check the worker logs for errors
4. If a workflow is stuck in a retry loop, check the activity's error and fix the root cause -- do not cancel the workflow unless necessary
