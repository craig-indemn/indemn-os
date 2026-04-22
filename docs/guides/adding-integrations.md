# Adding Integrations

Integrations connect the Indemn OS to external systems -- email providers, payment processors, CRMs, and any service that sends or receives data. This guide covers creating, configuring, and maintaining integrations.

---

## 1. Check Existing Adapters

Before building anything, check what is already supported. The kernel ships with adapters for:

| System Type | Provider | Adapter | Notes |
|-------------|----------|---------|-------|
| `email` | Outlook | `outlook` | Microsoft Graph API, OAuth2 |
| `email` | Gmail | `gmail` | Google Workspace API, OAuth2 |
| `payment` | Stripe | `stripe` | Webhooks + API |
| `workspace` | Google Workspace | `google_workspace` | Drive, Calendar, Contacts |

List all registered adapters:

```bash
indemn integration adapters
```

If an adapter exists for your system, skip to section 2. If not, see section 9 for building a new adapter.

---

## 2. Creating an Org-Level Integration

Org-level integrations are shared across the organization. All actors with the specified access roles can use them. This is the right choice for company email accounts, shared inboxes, and organizational services.

```bash
indemn integration create --name "Company Outlook" --system-type email \
  --provider outlook --owner-type org --access-roles "admin,operations"
```

Parameters:

| Parameter | Description |
|-----------|-------------|
| `--name` | Human-readable name for the integration |
| `--system-type` | Category: `email`, `payment`, `workspace`, `crm`, `ams` |
| `--provider` | Which adapter to use: `outlook`, `gmail`, `stripe`, `google_workspace` |
| `--owner-type org` | Shared across the organization |
| `--access-roles` | Comma-separated roles that can use this integration |

The integration is created in `configured` state. It needs credentials before it can be activated.

---

## 3. Creating an Actor-Level (Personal) Integration

Actor-level integrations belong to a specific person. Their personal email, their calendar, their CRM login. Only the owning actor (and admins) can access the data.

```bash
indemn integration create --owner actor --actor jc@example.com \
  --name "JC Gmail" --system-type email --provider gmail
```

Personal integrations are important for:
- Individual email accounts that should not be shared
- Personal calendars
- CRM accounts where each user has their own login
- Any service where data access must be scoped to one person

---

## 4. Setting Credentials

Credentials are stored in AWS Secrets Manager, never in the integration record itself. You provide a reference to the secret:

```bash
indemn integration set-credentials <integration-id> \
  --secret-ref indemn/prod/integrations/outlook
```

The secret at that path must contain the fields the adapter expects. For Outlook:

```json
{
  "client_id": "...",
  "client_secret": "...",
  "tenant_id": "...",
  "refresh_token": "..."
}
```

For Gmail:

```json
{
  "client_id": "...",
  "client_secret": "...",
  "refresh_token": "..."
}
```

For Stripe:

```json
{
  "api_key": "sk_live_...",
  "webhook_secret": "whsec_..."
}
```

To check what fields an adapter requires:

```bash
indemn integration adapters --provider outlook --credentials-schema
```

---

## 5. Testing

Before activating, verify the integration can connect and authenticate:

```bash
indemn integration test <integration-id>
```

This performs a lightweight health check: authenticates, makes a read-only API call, and reports success or failure with details.

Example output:
```
Integration: Company Outlook (int_abc123)
Provider:    outlook
Status:      pending
Test result: OK
  - Authentication: success
  - API call: listed 3 folders
  - Latency: 420ms
```

If the test fails, check:
- Secret reference path is correct
- Credential values are current (not expired)
- Network connectivity to the provider
- OAuth scopes are sufficient

To check health across all integrations:

```bash
indemn integration health
```

---

## 6. Activating

Once credentials are set and tests pass:

```bash
indemn integration transition <integration-id> --to active
```

The integration is now live. The adapter will begin processing inbound events (if configured) and is available for outbound calls.

To pause an integration without removing it:

```bash
indemn integration transition <integration-id> --to paused
```

To decommission:

```bash
indemn integration transition <integration-id> --to paused
```

---

## 7. Inbound Webhooks

Inbound integrations receive data from external systems via webhooks. When you activate an integration with inbound capabilities, the system generates a webhook URL.

### Getting the webhook URL

```bash
indemn integration get <integration-id> --webhook-url
```

Returns a URL like:
```
https://api.os.indemn.ai/webhook/stripe/int_abc123
```

### Configuring the external system

Register this URL in the external system's webhook settings. For example:
- **Outlook**: Azure portal > App registrations > Webhook subscriptions
- **Stripe**: Stripe Dashboard > Developers > Webhooks > Add endpoint
- **Custom**: POST to the URL with the provider's expected payload format

### Webhook validation

Each adapter validates inbound webhooks according to the provider's security model:

| Provider | Validation |
|----------|-----------|
| Outlook | Microsoft signature verification |
| Stripe | Stripe signature header (`stripe-signature`) |
| Gmail | Google push notification verification |

Invalid webhooks are rejected with 401 and logged.

### What happens on inbound

1. Webhook received at the integration URL
2. Adapter validates the signature
3. Adapter transforms the payload into a kernel entity or entity update
4. Entity event fires, triggering any matching watches
5. Actors with matching watches receive queue messages

---

## 8. Credential Rotation

Credentials expire or need rotation. The system supports zero-downtime rotation:

```bash
indemn integration rotate-credentials <integration-id> \
  --secret-ref indemn/prod/integrations/outlook-new
```

This:
1. Loads the new credentials from the specified secret
2. Tests them against the provider
3. If the test passes, swaps the active credentials
4. If the test fails, keeps the old credentials and reports the error

The integration remains active throughout. No downtime.

To check when credentials were last rotated:

```bash
indemn integration get <integration-id> --credentials-info
```

---

## 9. Building a New Adapter

When no adapter exists for your target system, you need to build one.

### What to implement

Every adapter extends the base `Adapter` class in `kernel/integration/adapter.py` with two categories of methods:

**Outbound methods** -- calling the external system:

| Method | Purpose |
|--------|---------|
| `test()` | Health check, used by `indemn integration test` |
| `send(payload)` | Send data to the external system |
| `fetch(query)` | Read data from the external system |
| `charge(params)` | Process a payment (payment adapters) |

**Inbound methods** -- receiving from the external system:

| Method | Purpose |
|--------|---------|
| `validate_webhook(request)` | Verify the inbound webhook is authentic |
| `parse_webhook(payload)` | Convert external payload to entity operations |
| `auth_initiate()` | Start OAuth flow (OAuth-based providers) |
| `auth_callback(params)` | Handle OAuth callback |
| `refresh_token()` | Refresh expired OAuth tokens |

Not every adapter needs all methods. A read-only integration skips `send`. A push-only integration skips `fetch`.

### Where to put it

```
kernel/
  integration/
    adapters/
      outlook.py      # existing
      gmail.py         # existing
      stripe.py        # existing
      your_adapter.py  # new
    registry.py        # register here
```

### Implementation skeleton

```python
from kernel.integration.adapter import Adapter

class YourAdapter(Adapter):
    system_type = "crm"
    provider = "your_provider"
    version = "your_provider_v1"

    async def test(self):
        # Make a lightweight API call to verify credentials
        response = await self.http.get(f"{self.config['base_url']}/me")
        return response.ok

    async def fetch(self, query: dict):
        # Read data from the external system
        ...

    async def send(self, payload: dict):
        # Write data to the external system
        ...

    async def validate_webhook(self, request) -> bool:
        # Verify webhook signature using secret from credentials
        ...

    async def parse_webhook(self, payload: dict) -> dict:
        # Convert external payload to entity operations
        ...
```

### Registering the adapter

Adapters self-register at import time. In your adapter file, call `register_adapter()`:

```python
from kernel.integration.registry import register_adapter

# At module level — registers when the module is imported
register_adapter("your_provider:your_provider_v1", YourAdapter)
```

### Testing

```bash
# Unit test the adapter
uv run pytest tests/unit/integration/test_your_adapter.py

# Integration test with real credentials
uv run pytest tests/integration/test_your_adapter.py

# End-to-end: create integration, set credentials, test, activate
indemn integration create --name "Test CRM" --system-type crm \
  --provider your_provider --owner-type org --access-roles admin
indemn integration set-credentials <id> --secret-ref indemn/dev/integrations/your-provider
indemn integration test <id>
```

---

## 10. Content Visibility

Personal integrations have a visibility setting that controls what other actors can see:

| Setting | What others see | What the owner sees |
|---------|----------------|-------------------|
| `metadata_shared` | Subject, sender, timestamp -- no body | Everything |
| `full_shared` | Everything | Everything |
| `owner_only` | Nothing | Everything |

Default is `metadata_shared`. To change:

```bash
indemn integration update <integration-id> --visibility full_shared
```

**When to use each:**

- `metadata_shared` (default): Personal email where associates need to know an email exists and who it is from, but the full content is private. Associates can classify by subject line and sender; human reviews the body.
- `full_shared`: Work email on a personal account where content should be accessible to the team and associates.
- `private`: Strictly personal integration where no other actor should see any data. Rare in a work context.

Visibility applies to entity reads. When an associate claims a message about an entity from a `metadata_shared` integration, the associate sees only the shared fields. If it needs the full content to do its job (e.g., classify by body text), the integration must be `full_shared` or the associate must escalate to the owning actor.
