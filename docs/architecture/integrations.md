# Integration System Architecture

Integration is the sixth kernel primitive. It represents every connection between the OS and an external system -- email providers, payment processors, carrier portals, identity providers, voice clients.

This document covers the Integration entity, credential management, adapters, inbound webhooks, the CLI, and the design decisions behind all of it.

---

## Why Integration Is a Kernel Primitive

Integration was elevated to a kernel primitive -- not relegated to an adapter layer or utility module -- because the problems it solves are fundamental to every system on the OS.

**Credential management** requires dual ownership. A company's shared carrier API and an individual employee's personal email are structurally the same problem: an external system that needs authentication, with rules about who can use it. Modeling this requires a first-class entity with ownership, access control, and lifecycle -- not a config file.

**Adapter versioning** requires entity state. When a provider releases a new API version, existing integrations need to keep working while new ones adopt the update. Migration must be safe, reversible, and auditable. This is entity lifecycle management.

**Inbound webhooks** require validation and routing. A Stripe webhook or an OAuth callback needs to be validated against the right credentials, parsed by the right adapter, and routed into the entity system through the normal save path. This is kernel infrastructure.

These three concerns -- credentials, versioning, inbound routing -- touch every domain built on the OS. They belong in the kernel.

---

## The Integration Entity

The Integration entity is one of seven kernel entities defined in `kernel_entities/integration.py`.

```python
class Integration(BaseEntity):
    name: str                       # "GIC Outlook", "JC Personal Gmail"
    owner_type: Literal["org", "actor"]
    owner_id: ObjectId
    system_type: str                # email, calendar, messaging, payment, ams, carrier,
                                    # identity_provider, voice_client
    provider: str                   # outlook, gmail, slack, stripe, applied-epic,
                                    # usli, livekit_human_client
    provider_version: str = "v1"    # outlook_v2, stripe_2024-06-20
    config: dict                    # Non-secret provider config (tenant_id, client_id, etc.)
    secret_ref: Optional[str]       # AWS Secrets Manager path -- NEVER credentials inline
    access: Optional[dict]          # Org-level: {"roles": ["underwriter", "operations"]}
    status: Literal["configured", "connected", "active", "error", "paused"]
    last_checked_at: Optional[datetime]
    last_error: Optional[str]
    content_visibility: Literal["full_shared", "metadata_shared", "owner_only"]
```

### State Machine

```
configured --> connected --> active --> error --> configured
                               |                     ^
                               v                     |
                            paused ------------------>
```

Full transitions:

| From | Allowed To |
|------|-----------|
| `configured` | `connected` |
| `connected` | `active`, `error` |
| `active` | `error`, `paused`, `configured` |
| `error` | `configured` |
| `paused` | `active`, `configured` |

Typical lifecycle: create in `configured`, set credentials and transition to `connected`, test connectivity and transition to `active`. If a health check fails, transition to `error`. Pause when temporarily disabling. Return to `configured` for re-setup.

### Ownership Model

Every Integration has an `owner_type` and `owner_id`:

- **Org-level** (`owner_type: "org"`): Shared across the organization. Access controlled by role list in `access.roles`. Used for shared infrastructure -- carrier APIs, payment processors, company email systems.
- **Actor-level** (`owner_type: "actor"`): Personal to one actor. No `access` field needed -- only that actor (and their bound associates) can use it. Used for personal email sync, personal calendar, personal voice client.

Both types are the same entity with the same lifecycle. The ownership model handles the full spectrum from shared infrastructure to personal connections through a single primitive.

### Database Indexes

```python
[("org_id", 1), ("system_type", 1), ("status", 1)]  # Resolution queries
[("owner_type", 1), ("owner_id", 1)]                  # Ownership lookups
```

---

## Credential Management

**Implementation**: `kernel/integration/credentials.py`

### The Rule: Credentials Never in MongoDB

Integration entities store `secret_ref` -- a path to AWS Secrets Manager. The actual credentials (OAuth tokens, API keys, webhook secrets) live exclusively in Secrets Manager. This is defense in depth: even a database breach does not expose credentials.

### Secret Paths

The path convention for `secret_ref`:

```
/indemn/{env}/org/{org_id}/integration/{integration_id}
/indemn/{env}/actor/{actor_id}/integration/{integration_id}
```

### Credential Caching

The API server caches credentials in-process with a 5-minute TTL to avoid hitting Secrets Manager on every request.

```python
CACHE_TTL = 300  # 5 minutes

async def fetch_credentials(secret_ref: str) -> dict:
    """Fetch from Secrets Manager with TTL caching."""
    now = time.time()
    if secret_ref in _cache:
        creds, cached_at = _cache[secret_ref]
        if now - cached_at < CACHE_TTL:
            return creds
    # Miss: fetch from Secrets Manager, update cache
    ...
```

Cache is invalidated on `store_credentials()` and `invalidate_cached_credentials()`, ensuring rotation takes effect immediately on the instance that performed it.

### Credential Operations

| Operation | What Happens |
|-----------|-------------|
| `store_credentials()` | Writes to Secrets Manager (creates if not exists), invalidates cache |
| `fetch_credentials()` | Returns cached if fresh, otherwise fetches from Secrets Manager |
| `invalidate_cached_credentials()` | Removes from in-process cache (forces next fetch to hit Secrets Manager) |

### What Credentials Never Do

- Never appear in entity queries or API responses
- Never appear in CLI output
- Never appear in the changes collection (audit trail)
- Never travel over the wire except from Secrets Manager to the API server process

---

## Credential Resolution

**Implementation**: `kernel/integration/resolver.py`

When an operation needs an external system (e.g., "send an email"), the kernel resolves which Integration to use through a three-step priority chain.

### Resolution Order

1. **Actor personal**: The calling actor has a personal Integration where `owner_type = "actor"`, `owner_id` matches the actor, `system_type` matches, and `status = "active"`. Use it.

2. **Owner personal**: The calling actor has an `owner_actor_id` (i.e., it's an associate bound to a human). Check the owner's personal integrations. This enables associates to act on behalf of humans using their credentials -- with consent, auditable, revocable.

3. **Org-level with role check**: The organization has an Integration where `owner_type = "org"`, `system_type` matches, `status = "active"`, and `access.roles` contains at least one of the actor's role names. Use it.

4. **No match**: Raise `AdapterNotFoundError` with a clear message and the CLI command to create one.

### Resolution Entry Point

```python
async def resolve_integration(
    system_type: str,
    actor_id: ObjectId = None,
    org_id: ObjectId = None,
    require_org_only: bool = False,
) -> Integration:
```

The `require_org_only` flag skips actor-level and owner-level lookups -- used when a caller explicitly needs the shared org credential (e.g., for a webhook configuration that shouldn't use a personal integration).

### Why Three Steps

Step 1 (actor personal) covers personal email sync, personal calendar, personal voice client. Step 2 (owner) enables the associate-on-behalf-of-human pattern -- a sync associate bound to a team member uses that team member's email integration. Step 3 (org-level) covers shared infrastructure where access is role-gated.

The chain means: prefer the most specific credential, fall back to the most general. An actor with both a personal email integration and access to the org's shared email integration will always use their personal one.

---

## Adapters

**Implementation**: `kernel/integration/adapter.py`, `kernel/integration/adapters/`

An adapter is the kernel code that knows how to talk to a specific external system. Each provider has one. The adapter translates between OS operations and provider-specific API calls.

### Base Class

All adapters inherit from `Adapter`:

```python
class Adapter(ABC):
    def __init__(self, config: dict, credentials: dict):
        self.config = config
        self.credentials = credentials

    # Outbound
    async def fetch(self, **params) -> list[dict]: ...
    async def send(self, payload: dict) -> dict: ...
    async def charge(self, amount: Decimal, currency: str = "usd", **params) -> dict: ...

    # Inbound
    async def validate_webhook(self, headers: dict, body: bytes) -> bool: ...
    async def parse_webhook(self, body: dict) -> dict: ...

    # Auth
    async def auth_initiate(self, redirect_uri: str) -> str: ...
    async def auth_callback(self, code: str, state: str) -> dict: ...
    async def refresh_token(self) -> dict: ...

    # Connectivity
    async def test(self) -> dict: ...
    def needs_token_refresh(self) -> bool: ...
```

Every method is optional. Adapters implement only what their provider supports. An email adapter implements `fetch` and `send`. A payment adapter implements `charge` and `validate_webhook`. An AMS adapter might implement only `fetch`.

### Current Adapters

| Adapter | Provider | Version | Capabilities | Location |
|---------|----------|---------|-------------|----------|
| `OutlookAdapter` | outlook | v2 | fetch, send, refresh_token, needs_token_refresh | `adapters/outlook.py` |
| `StripeAdapter` | stripe | v1 | charge, test, validate_webhook, parse_webhook | `adapters/stripe_adapter.py` |
| `GoogleWorkspaceAdapter` | google_workspace | v1 | fetch (Meet conferences, transcripts, recordings, smart notes) | `adapters/google_workspace.py` |

### Error Hierarchy

Adapters throw typed errors that the dispatch layer handles:

| Error | Meaning | Retry Behavior |
|-------|---------|---------------|
| `AdapterAuthError` | Authentication failed | Refresh token and retry once |
| `AdapterRateLimitError` | Rate limited | Backoff using `retry_after`, retry once |
| `AdapterTimeoutError` | Operation timed out | Retry once immediately |
| `AdapterNotFoundError` | Resource not found | Do not retry |
| `AdapterValidationError` | Invalid request | Do not retry |

### Data Mapping

Adapters translate between provider-specific formats and OS-normalized formats. For example, the Outlook adapter maps Microsoft Graph message format to a standardized email shape:

```python
def _map_to_os(self, outlook_msg: dict) -> dict:
    return {
        "external_id": outlook_msg["id"],
        "from_address": outlook_msg["from"]["emailAddress"]["address"],
        "subject": outlook_msg["subject"],
        "body": outlook_msg["body"]["content"],
        "received_at": outlook_msg["receivedDateTime"],
        ...
    }
```

This normalization means entity code and skills work with a consistent shape regardless of which email provider is behind the integration.

---

## Adapter Registry and Versioning

**Implementation**: `kernel/integration/registry.py`

### The Registry

The adapter registry maps `provider:version` keys to adapter classes:

```python
ADAPTER_REGISTRY: dict[str, type[Adapter]] = {}

def register_adapter(provider: str, version: str, adapter_cls: type[Adapter]):
    key = f"{provider}:{version}"
    ADAPTER_REGISTRY[key] = adapter_cls

def get_adapter_class(provider: str, version: str) -> type[Adapter]:
    key = f"{provider}:{version}"
    cls = ADAPTER_REGISTRY.get(key)
    if not cls:
        raise AdapterNotFoundError(f"No adapter for {key}")
    return cls
```

Adapters self-register at import time:

```python
# At the bottom of outlook.py
register_adapter("outlook", "v2", OutlookAdapter)

# At the bottom of stripe_adapter.py
register_adapter("stripe", "v1", StripeAdapter)
```

All adapters are auto-imported at startup via `kernel/integration/adapters/__init__.py`.

### Versioning

The `provider_version` field on Integration maps to a specific adapter class in the registry. This enables:

- **Safe migration**: Register `outlook_v3` alongside `outlook_v2`. Existing integrations keep using v2 until explicitly upgraded.
- **Dry-run upgrades**: The upgrade endpoint verifies the target adapter exists before applying.
- **Rollback**: Transition back to the old version if the new one has issues. The old adapter class stays registered.

Upgrade flow:

```
1. New adapter code deployed (registers outlook_v3)
2. indemn integration upgrade <id> --to-version v3 --dry-run  (verify target exists)
3. indemn integration upgrade <id> --to-version v3 --no-dry-run  (apply)
4. indemn integration test <id>  (verify connectivity)
5. If issues: indemn integration upgrade <id> --to-version v2 --no-dry-run  (rollback)
```

---

## Adapter Dispatch

**Implementation**: `kernel/integration/dispatch.py`

The dispatch module is the primary entry point for all adapter usage. It ties together resolution, credential fetching, registry lookup, and OAuth token management.

### `get_adapter()`

```python
async def get_adapter(
    system_type: str,
    actor_id=None,
    org_id=None,
    require_org_only: bool = False,
) -> Adapter:
```

What it does:
1. Resolve the Integration entity via `resolve_integration()`
2. Fetch credentials from Secrets Manager via `fetch_credentials()`
3. Look up the adapter class via `get_adapter_class(provider, version)`
4. Instantiate the adapter with config and credentials
5. If the adapter reports `needs_token_refresh()`, attempt a proactive refresh before returning

This means callers get a ready-to-use adapter with fresh credentials. Token refresh is transparent.

### `execute_with_retry()`

```python
async def execute_with_retry(adapter: Adapter, method_name: str, *args, **kwargs):
```

Wraps any adapter method with automatic error handling:

| Error | Action |
|-------|--------|
| `AdapterAuthError` | Refresh token, update credentials in Secrets Manager, retry once |
| `AdapterRateLimitError` | Sleep for `retry_after` seconds (default 60), retry once |
| `AdapterTimeoutError` | Retry once immediately |

This is the recommended way to call adapter methods from associate code or temporal activities. One retry per error type, no retry loops.

---

## Inbound Webhooks

**Implementation**: `kernel/api/webhook.py`

### Endpoint

```
POST /webhook/{provider}/{integration_id}
```

### Request Flow

1. **Load Integration** by ID. Verify it matches the URL's `provider` parameter and `status = "active"`.

2. **Instantiate adapter** with the integration's config and credentials from Secrets Manager.

3. **Validate webhook** by calling `adapter.validate_webhook(headers, body_bytes)`. The adapter checks the provider-specific signature (e.g., Stripe's `stripe-signature` header). Invalid signatures return 401.

4. **Parse webhook** by calling `adapter.parse_webhook(body_json)`. The adapter converts the provider-specific payload into a standardized entity operation:

```python
{
    "entity_type": "Payment",           # Which entity to target
    "lookup_by": "stripe_payment_intent_id",  # Field to find the entity
    "lookup_value": "pi_abc123",        # Value to match
    "operation": "transition",          # create, transition, or update
    "params": {"to_status": "completed"}  # Operation parameters
}
```

5. **Apply entity operations** through the normal `save_tracked()` path. This means:
   - Optimistic concurrency check
   - State machine enforcement (for transitions)
   - Changes collection record
   - Watch evaluation and message creation
   - Cascade begins

The webhook handler sets `actor_id` to `webhook:{provider}` for audit trail purposes. The `method_metadata` includes the original webhook event type.

### Supported Operations

| Operation | What Happens |
|-----------|-------------|
| `create` | Instantiate new entity with `params`, save via `save_tracked()` |
| `transition` | Look up entity, call `transition_to()`, save via `save_tracked()` |
| `update` | Look up entity, set fields from `params`, save via `save_tracked()` |

### Example: Stripe Payment Webhook

A `payment_intent.succeeded` event arrives:

```
POST /webhook/stripe/64a1b2c3d4e5f6a7b8c9d0e1
```

1. Integration `64a1b2c3...` loaded, verified as Stripe, verified as active
2. `StripeAdapter.validate_webhook()` checks `stripe-signature` header against webhook secret
3. `StripeAdapter.parse_webhook()` maps to `{entity_type: "Payment", operation: "transition", params: {to_status: "completed"}}`
4. Payment entity found by `stripe_payment_intent_id`, transitioned to `completed` via `save_tracked()`
5. Watches fire -- any role watching for Payment transitions to `completed` gets a message
6. Associated actors process the consequence (update billing records, notify customer, etc.)

The inbound webhook joins the same cascade as any other entity change. No special path.

---

## Integration CLI

**Implementation**: `kernel/cli/integration_commands.py`

The CLI provides convenience commands beyond the auto-generated CRUD.

### Create

```bash
# Org-level integration with role-based access
indemn integration create --owner org --name "GIC Outlook" \
  --system-type email --provider outlook \
  --access-roles "underwriter,operations,admin"

# Actor-level (personal) integration
indemn integration create --owner actor --actor jc@gicunderwriters.com \
  --name "JC Personal Gmail" --system-type email --provider gmail
```

For actor-level integrations, `--actor` accepts an email address and resolves it to an actor ID.

### Credential Management

```bash
# Store credentials from a JSON file
indemn integration set-credentials <id> --from-file @outlook-creds.json

# Rotate credentials (provider-specific -- calls adapter.refresh_token())
indemn integration rotate-credentials <id>
```

The `--from-file` flag accepts a `@` prefix (optional, stripped). Credentials are posted to the API, which stores them in Secrets Manager. The file is local-only; credentials never enter MongoDB.

### Connectivity Testing

```bash
# Test a specific integration
indemn integration test <id>

# Health check all integrations in the org
indemn integration health
```

`health` iterates all active integrations, calls each adapter's `test()` method, updates `last_checked_at` and `last_error`, and returns a summary.

### Adapter Upgrades

```bash
# Dry run (default) -- verify target adapter exists
indemn integration upgrade <id> --to-version v3

# Apply
indemn integration upgrade <id> --to-version v3 --no-dry-run
```

### Lifecycle and Listing

```bash
# List with filters (auto-generated CRUD)
indemn integration list
indemn integration get <id>

# State transitions (auto-generated)
indemn integration transition <id> --to active
```

---

## Integration Management API

**Implementation**: `kernel/api/integration_routes.py`

Beyond the auto-generated CRUD endpoints, the integration management router provides:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/integrations/{id}/set-credentials` | POST | Store credentials in Secrets Manager |
| `/api/integrations/{id}/rotate-credentials` | POST | Rotate via `adapter.refresh_token()` |
| `/api/integrations/{id}/test` | POST | Test connectivity via `adapter.fetch(limit=1)` |
| `/api/integrations/{id}/upgrade` | POST | Upgrade adapter version (supports dry run) |
| `/api/integrations/health-check` | POST | Test all org integrations, update health fields |

All endpoints require authentication and check permissions (`Integration` read or write) via the standard auth middleware.

---

## Special System Types

The Integration primitive handles several system types that might seem like they need their own mechanism. They do not.

### Identity Providers

SSO reuses Integration with `system_type: identity_provider`.

An identity provider Integration (e.g., Okta, Azure AD) uses the same entity, the same credential management, the same adapter pattern. The adapter implements `auth_initiate()` (redirect to provider) and `auth_callback()` (validate token, issue OS session).

Login flow:
1. User selects SSO login
2. OS looks up the org's `identity_provider` integration
3. Adapter's `auth_initiate()` returns redirect URL
4. Provider validates and returns token
5. Adapter's `auth_callback()` validates token
6. OS issues a session

SSO and password authentication can coexist in the same org -- they're different auth methods on the same actor, not competing systems.

### Voice Clients

Human participation in voice interactions uses `system_type: voice_client, provider: livekit_human_client, owner_type: actor`.

When a human takes over a live voice interaction, their voice client is resolved through the same credential resolution chain as any other integration. No new concept needed for real-time human participation.

### Future System Types

The `system_type` field is a plain string, not an enum. New system types (AMS, carrier portal, rating engine) require only a new adapter -- the Integration entity, credential management, resolution chain, webhook handler, CLI, and API all work unchanged.

---

## Content Visibility Scoping

When entities are created from personal integration data (e.g., syncing a personal email inbox), the Integration's `content_visibility` field determines who can see what.

| Policy | Metadata | Full Content |
|--------|----------|-------------|
| `full_shared` | Everyone with entity read permission | Everyone with entity read permission |
| `metadata_shared` | Everyone with entity read permission | Owner only |
| `owner_only` | Owner only | Owner only |

**Default for personal integrations**: `metadata_shared`. The team sees that an email arrived, from whom, when, and about what. The full email body is scoped to the owner.

**Default for org-level integrations**: `full_shared`. Shared infrastructure data is visible to anyone with the appropriate permissions.

This is a field on the Integration entity, configurable at creation time and changeable later. The visibility policy is enforced by the entity query layer when returning data synced from personal integrations.

---

## Writing a New Adapter

To add support for a new provider:

### 1. Create the Adapter Class

```python
# kernel/integration/adapters/new_provider.py

from kernel.integration.adapter import Adapter, AdapterAuthError
from kernel.integration.registry import register_adapter

class NewProviderAdapter(Adapter):
    async def fetch(self, **params) -> list[dict]:
        # Provider-specific fetch logic
        ...

    async def validate_webhook(self, headers: dict, body: bytes) -> bool:
        # Provider-specific signature check
        ...

    async def parse_webhook(self, body: dict) -> dict:
        # Return: {entity_type, lookup_by, lookup_value, operation, params}
        ...

    async def test(self) -> dict:
        # Minimal read-only connectivity check
        ...

# Self-register
register_adapter("new_provider", "v1", NewProviderAdapter)
```

### 2. Add to Auto-Import

```python
# kernel/integration/adapters/__init__.py
from kernel.integration.adapters import (
    google_workspace,
    outlook,
    stripe_adapter,
    new_provider,      # Add this
)
```

### 3. Create an Integration

```bash
indemn integration create --owner org --name "New Provider" \
  --system-type <type> --provider new_provider
indemn integration set-credentials <id> --from-file @creds.json
indemn integration test <id>
indemn integration transition <id> --to active
```

That's it. The Integration entity, credential management, resolution chain, dispatch, retry logic, webhook handling, CLI, and API all work without modification. New providers mean new adapter code. Everything else is the same.

---

## Key Files

| Path | Purpose |
|------|---------|
| `kernel_entities/integration.py` | Integration entity definition (fields, state machine, indexes) |
| `kernel/integration/adapter.py` | Base Adapter class and error hierarchy |
| `kernel/integration/registry.py` | Adapter registry (`provider:version` -> class) |
| `kernel/integration/resolver.py` | Credential resolution chain (actor -> owner -> org) |
| `kernel/integration/credentials.py` | AWS Secrets Manager fetch/store with TTL cache |
| `kernel/integration/dispatch.py` | `get_adapter()` and `execute_with_retry()` |
| `kernel/integration/adapters/outlook.py` | Outlook/Microsoft Graph adapter |
| `kernel/integration/adapters/stripe_adapter.py` | Stripe payment + webhook adapter |
| `kernel/integration/adapters/google_workspace.py` | Google Meet/Drive/Admin adapter |
| `kernel/api/webhook.py` | Generic inbound webhook handler |
| `kernel/api/integration_routes.py` | Credential management and health check endpoints |
| `kernel/cli/integration_commands.py` | Integration CLI commands |

---

## Design Decisions

**Why Integration as a kernel primitive, not an adapter layer.** Credential management -- dual ownership, secret references, TTL caching, rotation -- is fundamental infrastructure that every domain depends on. Adapter versioning requires entity-level lifecycle. Inbound webhooks require kernel-level routing. These are not utility concerns. They are structural.

**Why dual ownership (org + actor).** A company's shared carrier API and an employee's personal email are the same pattern with different ownership. One primitive handles both. The resolution chain (personal -> owner -> org) means the most specific credential always wins, with automatic fallback to shared infrastructure.

**Why `secret_ref` instead of inline credentials.** Defense in depth. The database contains only a pointer. Actual secrets live in Secrets Manager with its own access logging, rotation support, and encryption. Even with full database access, credentials are not exposed.

**Why `provider_version` instead of just `provider`.** Adapter migration safety. When Outlook ships a new API version, existing integrations keep working on the old adapter while new ones use the new adapter. Both coexist in the registry. Migration is an explicit, reversible, auditable operation -- not a flag day.

**Why typed errors instead of generic exceptions.** The dispatch layer needs to know what went wrong to decide what to do. Auth errors trigger token refresh. Rate limits trigger backoff. Timeouts trigger immediate retry. Not-found and validation errors are terminal. Generic exceptions would require string parsing to make these decisions.

**Why a generic webhook endpoint instead of per-provider routes.** `/webhook/{provider}/{integration_id}` routes through the same code path for every provider. The adapter handles provider-specific validation and parsing. New providers get webhook support by implementing two methods, not by registering new routes. The entity operations produced by `parse_webhook()` go through `save_tracked()` like everything else -- watches fire, cascades begin, audit trail records.
