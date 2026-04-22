# Security Architecture

This document describes the security model of the Indemn OS -- how organization isolation is enforced, how credentials are managed, how the audit trail resists tampering, and what boundaries constrain every actor in the system. A senior developer who has never seen this system should understand every security boundary after reading this document.

---

## Organization Isolation

Every query in the system is scoped to an organization. This is not a convention -- it is enforced structurally by the `OrgScopedCollection` wrapper. Application code cannot construct an unscoped query.

### OrgScopedCollection

All entity access goes through `OrgScopedCollection`, which wraps the Motor (async MongoDB) collection and injects `org_id` into every operation:

```python
# What application code writes:
submissions = await collection.find_scoped({"status": "new"})

# What actually executes against MongoDB:
submissions = await raw_collection.find({"org_id": current_org_id, "status": "new"})
```

The `org_id` comes from Python contextvars, set by the auth middleware after JWT verification. There is no parameter for org_id in the application-facing API -- it is always injected.

**Key enforcement points:**

| Operation | OrgScopedCollection Method | Injection |
|-----------|---------------------------|-----------|
| Find | `find_scoped(**query)` | Adds `org_id` to filter |
| Find one | `get_scoped(entity_id)` | Adds `org_id` to filter |
| Insert | `insert_scoped(doc)` | Sets `org_id` on document |
| Update | `update_scoped(entity_id, update)` | Adds `org_id` to filter |
| Delete | `delete_scoped(entity_id)` | Adds `org_id` to filter |
| Aggregate | `aggregate_scoped(pipeline)` | Prepends `$match: {org_id}` stage |

**Raw collection access is hidden.** The raw Motor collection is available only inside `kernel/__init__.py::init_database()`. Application code receives `OrgScopedCollection` instances. Importing the raw collection from application code is a code review rejection.

Implementation: `kernel/scoping/org_scoped.py`.

### PlatformCollection

For cross-org administrative operations (e.g., listing all organizations, platform health checks), `PlatformCollection` provides unscoped access with full audit:

```python
# Platform-level query (no org scoping)
orgs = await platform_collection.find({"status": "active"})
```

Every operation through `PlatformCollection` is logged with the platform admin context (see `authentication.md`). This collection is only used by:

- `kernel/api/admin_routes.py` -- platform admin endpoints
- `kernel/api/bootstrap.py` -- first-org initialization
- `kernel/queue_processor.py` -- cross-org sweep operations (message backlog, zombie sessions)

Application code that handles customer requests never imports `PlatformCollection`.

Implementation: `kernel/scoping/platform.py`.

---

## Credential Management

Credentials never touch MongoDB. Every integration credential is stored in AWS Secrets Manager, and the database holds only a `secret_ref` -- an opaque reference string.

### Storage Model

```
Integration entity (MongoDB):
  {
    "name": "outlook-acme",
    "provider": "outlook",
    "secret_ref": "indemn/dev/integrations/org_xyz/outlook-acme",
    ...
  }

AWS Secrets Manager:
  "indemn/dev/integrations/org_xyz/outlook-acme":
    {
      "client_id": "...",
      "client_secret": "...",
      "access_token": "...",
      "refresh_token": "...",
      "token_expiry": "2026-04-22T18:30:00Z"
    }
```

### Enforcement

| Boundary | How Enforced |
|----------|-------------|
| Credentials never in API responses | `Integration` entity serializer strips `secret_ref` value. API returns `"secret_ref": "[redacted]"` |
| Credentials never in CLI output | CLI formatters replace credential fields with `***` |
| Credentials never in change records | `write_change_record()` excludes `secret_ref` from `changed_fields` diff |
| Credentials never in logs | Structured logger scrubs fields matching `secret|token|password|key` patterns |
| Credentials never in OTEL spans | Span attributes exclude credential-bearing fields |

### Rotation

Credential rotation is a first-class operation, not an afterthought:

```bash
# Rotate credentials for an integration
indemn integration rotate outlook-acme
# Fetches new credentials from provider (OAuth refresh or re-auth)
# Stores new credentials in Secrets Manager
# Old credentials archived as previous version
# Change record written (without credential values)
```

For OAuth integrations, the adapter handles token refresh automatically:
1. `execute_with_retry()` in `kernel/integration/dispatch.py` calls the adapter
2. If the adapter returns an auth error, `needs_token_refresh()` is checked
3. If true, `refresh_token()` is called, new tokens stored in Secrets Manager
4. Original operation retried with fresh credentials
5. All of this is transparent to the calling code

Implementation: `kernel/integration/credentials.py` for Secrets Manager read/write. `kernel/integration/dispatch.py` for retry-with-refresh logic.

---

## Skill Integrity

Skills are behavioral instructions for AI associates. A modified skill changes what an associate does. Skill integrity verification ensures that skills cannot be modified outside the normal update path without detection.

### Content Hash

Every skill has a `content_hash` computed on creation or update:

```python
content_hash = SHA256(skill.name + skill.version + skill.body + skill.tools_json)
```

The hash is stored in the Skill entity and in the change record for that update.

### Verification on Load

When a skill is loaded for associate execution (in `kernel/temporal/activities.py::load_actor()`), the content hash is recomputed and compared:

```python
loaded_hash = compute_skill_hash(skill)
if loaded_hash != skill.content_hash:
    raise SkillIntegrityError(f"Skill {skill.name} modified outside normal update path")
```

A hash mismatch means the skill document was modified directly in MongoDB (bypassing `save_tracked()`) or the content was tampered with. The associate will not execute with a modified skill.

### Version Approval Workflow

Skills support a version approval workflow:

```bash
# Create or update a skill (enters draft state)
indemn skill update classifier-prompt --body "New instructions..."
# Skill version 4 created (draft)

# Approve for production use
indemn skill approve classifier-prompt --version 4
# Skill version 4 approved. Associates will use this version.

# Roll back to previous version
indemn skill approve classifier-prompt --version 3
# Skill version 3 re-approved. Associates will use this version.
```

Only approved skill versions are loaded for associate execution. Draft versions can be tested via `indemn skill test`.

Implementation: `kernel/skill/integrity.py`.

---

## Audit Trail

The changes collection is the system's audit trail. It is append-only, hash-chained, and tamper-evident. See `observability.md` for the full changes collection specification.

### Append-Only Enforcement

The changes collection is written only by `write_change_record()` inside `save_tracked()` transactions. There is no update or delete operation on change records in the kernel codebase. MongoDB role-based access control (RBAC) can further restrict the database user to insert-only on the changes collection.

### Hash Chain

Each change record hashes its content together with the previous record's hash (scoped per entity). Modifying or deleting any record breaks the chain from that point forward. Verification:

```bash
indemn audit verify
# Checks every entity's hash chain, reports any breaks
```

See `observability.md` for detailed hash chain mechanics and CLI usage.

Implementation: `kernel/changes/hash_chain.py`.

---

## Rule Validation

Rules are the deterministic logic layer -- they evaluate conditions and set fields or veto to LLM fallback. Because rules modify entity state, they are validated to prevent privilege escalation and data corruption.

### State Machine Field Exclusion

Rules cannot set fields that are part of an entity's state machine. State transitions must go through `transition_to()`, which enforces allowed transitions:

```python
# This rule would be rejected at creation time:
{
  "action": "set_fields",
  "fields": {"status": "approved"}  # REJECTED: status is a state machine field
}
```

### Field Validation Against Schema

Rules that use `set_fields` are validated against the entity's field definitions:

- Field must exist on the entity type
- Value must match the field's type
- Required field validation still applies after rule application

### Permission Check on Rule Creation

The actor creating a rule must have write permission for every field the rule touches:

```python
# Actor with write permission on ["classification", "priority"] can create:
{"action": "set_fields", "fields": {"classification": "auto", "priority": 5}}  # OK

# But cannot create:
{"action": "set_fields", "fields": {"handling_actor_id": "actor_xyz"}}  # REJECTED: no write on handling_actor_id
```

Implementation: `kernel/rule/validation.py`.

---

## Trust Boundary

The trust boundary separates services that have direct MongoDB access from those that authenticate via the API.

| Inside Trust Boundary | Outside Trust Boundary |
|----------------------|----------------------|
| API Server | Base UI |
| Queue Processor | Chat Harness |
| Temporal Worker | Async Harness |
| | CLI Package |

**Only three processes have database credentials.** Everything else authenticates via the API server, which enforces auth, permissions, org scoping, and audit logging.

This means:
- Harnesses cannot bypass permissions (they use CLI subprocess, which calls the API)
- The UI cannot query MongoDB directly (it calls the REST API)
- CLI users cannot bypass org scoping (the API injects org_id from their JWT)
- A compromised harness can only do what its actor's roles allow

---

## Session Security

### JWT with JTI Revocation

Every access token contains a `jti` (JWT ID). When a session is revoked, the `jti` is added to an in-memory revocation set on every API server instance (synchronized via MongoDB Change Streams on the Session collection). This enables immediate revocation without waiting for token expiry.

### Refresh Token Rotation

Refresh tokens are rotated on every use. The old token remains valid for 30 seconds (overlap window) to handle concurrent requests. After 30 seconds, the old token is invalidated. If a refresh token is used after it has been rotated (replay attack), all sessions for that actor are revoked.

### Rate Limiting

Authentication endpoints are rate-limited to prevent brute force:
- 5 login failures per 10 minutes per actor triggers a 30-minute lockout
- In-memory counters synchronized across instances via Change Streams

See `authentication.md` for the full rate limiting table.

---

## Platform Admin Scope Limits

Platform administrators (Indemn staff) can access customer organizations for build, debug, and incident work. Their access is constrained:

| Can Do | Cannot Do |
|--------|-----------|
| Read entity data in customer org | Read integration credentials (secret_ref values) |
| Write entities within declared scope limits | Modify credentials (except audited rotation) |
| View change history and message log | Impersonate customer actors |
| Run diagnostic commands | Escalate own privileges in customer org |
| Rotate integration credentials (audited) | Grant themselves roles in customer org |
| Create time-limited admin sessions | Create permanent access |

Every platform admin action is audited in the customer's organization with the admin's identity, work type, scope, and reason. Customers see a notification in their activity feed when an admin session starts. See `authentication.md` for the full admin access specification.

---

## Sandbox Contract for Associates

AI associates execute within a sandbox defined by their roles, skills, and runtime. The sandbox contract constrains what an associate can do:

### Boundaries

| Boundary | Enforcement |
|----------|------------|
| Entity access | Associate's roles determine read/write permissions per entity type |
| Skill scope | Associate can only use tools listed in its approved skills |
| Org scope | Associate operates within a single org (inherited from triggering session) |
| Cascade depth | Circuit breaker at depth 10 prevents infinite loops |
| Timeout | Temporal workflow timeout per associate (default: 5 minutes for async, none for real-time) |
| Resource limits | Runtime.capacity constrains concurrent executions |

### Harness Enforcement

Harnesses enforce the sandbox by using CLI subprocess for all OS operations:

```bash
# Harness calls CLI (which calls API with associate's auth)
indemn submission create --data '{...}'
# API validates: does this associate's role have write:Submission? Is the data valid?
```

The harness never imports kernel code, never has database credentials, and never constructs raw API calls. The CLI binary handles authentication and serialization. The API server enforces permissions.

Implementation details may vary by harness type (chat vs. async), but the trust boundary contract is the same: all OS operations go through the API.

---

## Associate Owner Consent

Associates can be granted integrations (e.g., access to a customer's email via an Outlook integration). The `owner_actor_id` field on an Integration requires explicit consent from the owning actor.

### Consent Flow

```
1. Admin assigns integration to associate:
   indemn integration assign outlook-acme --to actor_associate_001

2. System checks: does the integration have an owner_actor_id?
   Yes --> consent required from owner

3. Consent request sent to owner (human actor)
   Message in their queue: "Associate 'classifier' requests access to your Outlook integration"

4. Owner approves or denies:
   indemn integration consent outlook-acme --approve
   # or
   indemn integration consent outlook-acme --deny

5. If approved: Integration.access updated, associate can use it
   If denied: Assignment blocked, change record written with denial
```

### Revocation

The owner can revoke consent at any time:

```bash
indemn integration revoke outlook-acme --from actor_associate_001
```

Revocation is immediate. The associate's next attempt to use the integration fails with a permission error. The revocation is recorded in the changes collection.

### Audit

Every consent grant, denial, and revocation is recorded in the changes collection with full provenance:

```bash
# View consent history for an integration
indemn trace entity Integration outlook-acme --filter '{"event_type": {"$in": ["consent_granted", "consent_denied", "consent_revoked"]}}'
```

---

## Implementation Files

| File | Responsibility |
|------|----------------|
| `kernel/scoping/org_scoped.py` | `OrgScopedCollection` -- org_id injection on every query |
| `kernel/scoping/platform.py` | `PlatformCollection` -- cross-org admin access with audit |
| `kernel/integration/credentials.py` | AWS Secrets Manager read/write, credential serialization stripping |
| `kernel/integration/dispatch.py` | `execute_with_retry()` -- retry with automatic token refresh |
| `kernel/integration/resolver.py` | Credential resolution chain: actor -> owner -> org |
| `kernel/skill/integrity.py` | Content hash computation and verification |
| `kernel/changes/hash_chain.py` | SHA-256 hash chain for tamper-evident audit |
| `kernel/changes/collection.py` | `write_change_record()` -- append-only change writing |
| `kernel/rule/validation.py` | State machine exclusion, field validation, permission check |
| `kernel/auth/middleware.py` | JWT verification, jti revocation, permission enforcement |
| `kernel/auth/session_manager.py` | Refresh token rotation, session revocation |
| `kernel/auth/rate_limit.py` | Brute force protection with Change Stream sync |
| `kernel_entities/session.py` | Session entity with auth state |
| `kernel_entities/integration.py` | Integration entity with consent model |
