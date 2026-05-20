# Adding Deployments

Deployments are placements of associates on specific surfaces — where end-users encounter the agent. This guide walks through creating a Deployment from scratch: the SurfaceConfig + BrandAssets it references, the parameter contract, the auth identity model, and how a surface (a customer site or internal UI) consumes it via the embed.js SDK.

Before reading this guide: read [`architecture/deployments.md`](../architecture/deployments.md) for the entity design.

---

## 1. Planning

Before creating any records, answer four questions:

**Where will the associate be placed?** A specific page on a customer site? An internal team UI? A standalone product? The answer determines the surface URL, allowed origins, and visual treatment.

**Who is the user?** An Indemn employee with an OS identity? A customer's employee (with a sub-org identity)? An anonymous web visitor (no OS identity)? This determines `acts_as`:
- User has OS identity → `acts_as = session_actor` (impersonate the user; their permissions enforced)
- User is anonymous → `acts_as = associate_self` (agent acts with its own permissions; deployment_context scopes the conversation)

**What does the surface know at session start?** What data does the page have that the associate should be initialized with? Customer ID? Policy ID? Current page section? Actor ID of the logged-in employee? This is your `parameter_schema`.

**What's the channel?** Web chat (uses `indemn-runtime-chat`, the WebSocket runtime)? Voice (uses `indemn-runtime-voice`)? Both (two Deployments, one per channel)? Future: Slack, Teams, SMS, email.

---

## 2. Creating BrandAssets (if needed)

If the venue uses brand colors / logo / fonts you haven't already captured, create a BrandAssets record. Often you'll reuse an existing one.

```bash
indemn brand-assets create --data '{
  "name": "Acme Insurance Brand",
  "logo_url": "https://cdn.acme.com/logo.svg",
  "favicon_url": "https://cdn.acme.com/favicon.ico",
  "primary_color": "#1a3a8f",
  "secondary_color": "#4a5d8a",
  "accent_color": "#f59e0b",
  "font_family_heading": "Inter",
  "font_family_body": "Inter"
}'
```

Note the returned `_id` — you'll reference it from SurfaceConfig.

---

## 3. Creating a SurfaceConfig

A SurfaceConfig is per-vendor-and-channel. For a chat widget using prompt-kit, you'll have one SurfaceConfig. For a voice widget using LiveKit, you'll have a different SurfaceConfig. Same brand, different surfaces.

```bash
indemn surface-config create --data '{
  "name": "Acme Renewal Chat Widget",
  "channel_kind": "chat",
  "vendor": "prompt-kit",
  "brand_assets_id": "<brand_assets_id>",
  "config": {
    "widget_position": "bottom-right",
    "primary_color_ref": "brand.primary",
    "show_header": true,
    "header_text": "Renewal Assistant",
    "input_placeholder": "Ask about your renewal…",
    "show_voice_toggle": false,
    "open_on_load": false
  }
}'
```

The `config` field is validated against the per-vendor JSON Schema at `indemn-os/schemas/surface_configs/prompt-kit.schema.json`. Adding a new vendor = adding a new schema file (no Python class change).

For a voice SurfaceConfig:

```bash
indemn surface-config create --data '{
  "name": "Acme Renewal Voice Widget",
  "channel_kind": "voice",
  "vendor": "livekit",
  "brand_assets_id": "<brand_assets_id>",
  "config": {
    "widget_style": "floating-orb",
    "show_transcription": true,
    "show_waveform": true,
    "primary_color_ref": "brand.primary",
    "stt_provider": "deepgram",
    "stt_model": "nova-3",
    "tts_provider": "cartesia",
    "tts_model": "sonic-3",
    "tts_voice_id": "<voice_uuid>",
    "interrupt_enabled": true,
    "max_endpointing_delay_ms": 2000
  }
}'
```

---

## 4. Creating the Deployment

Now bind the associate + Runtime + SurfaceConfig + parameter contract together:

```bash
indemn deployment create --data '{
  "name": "Acme Renewal — Web Chat",
  "associate_id": "<sales_assistant_actor_id>",
  "runtime_id": "<indemn-runtime-chat_id>",
  "surface_config_id": "<surface_config_id>",
  "parameter_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["customer_id", "policy_id"],
    "properties": {
      "customer_id":  {"type": "string", "pattern": "^[0-9a-f]{24}$"},
      "policy_id":    {"type": "string", "pattern": "^[0-9a-f]{24}$"},
      "page_section": {"type": "string", "enum": ["overview", "documents", "billing"]}
    },
    "additionalProperties": false
  },
  "static_parameters": {"tenant": "acme-insurance"},
  "parameter_schema_validation_mode": "strict",
  "llm_override": {"temperature": 0.3},
  "greeting": "Welcome to your renewal — how can I help?",
  "acts_as": "session_actor",
  "allowed_origins": ["https://acme.example.com"],
  "resumption_config": {"ttl_seconds": 86400, "kill_on_resume": true}
}'
```

The Deployment is created in `configured` status. Transition to `active` when ready:

```bash
indemn deployment transition <deployment_id> --to active
```

The Deployment is now live. The runtime accepts connections referencing it.

---

## 5. Verifying the Deployment

Confirm the Deployment exists and is healthy:

```bash
# Show the Deployment
indemn deployment get <deployment_id>

# Show the public-safe view (this is what the embed.js SDK sees)
curl https://api.os.indemn.ai/api/deployments/<deployment_id>/public

# List all active Deployments in your org
indemn deployment list --filter '{"status": "active"}'

# Find all Deployments of a specific associate (the "one associate, many venues" view)
indemn deployment list --filter '{"associate_id": "<actor_id>"}'

# Find all Deployments served by a runtime (the "what's deployed on this channel" view)
indemn deployment list --filter '{"runtime_id": "<runtime_id>", "status": "active"}'
```

---

## 6. Embedding on the Surface

The customer site (or internal product UI) embeds the agent via the embed.js SDK:

```html
<!-- On the customer's renewal page -->
<script src="https://cdn.indemn.ai/embed.js"></script>
<script>
  Indemn.deploy({
    deployment_id: "dep_xxx",
    params: {
      customer_id: "<from your auth context>",
      policy_id: "<from the page>",
      page_section: "overview"
    }
  });
</script>
```

The SDK:
1. Fetches `/api/deployments/dep_xxx/public` to get channel kind, runtime URL, schema, greeting
2. Validates `params` against the schema (client-side via `ajv`)
3. Opens the appropriate channel (WebSocket for chat; HTTP `/sessions` + LiveKit for voice)
4. Renders the chat widget (prompt-kit) or voice widget (LiveKit components) styled per the SurfaceConfig + BrandAssets

---

## 7. Operating the Deployment

**Pause during incidents.** If something's wrong (e.g., the model provider is down, the associate is misbehaving), pause the Deployment immediately:

```bash
indemn deployment transition <deployment_id> --to paused
```

Paused Deployments reject new sessions with HTTP 409. Existing sessions continue to completion. Resume by transitioning back to `active`.

**Inspect live sessions.** Find all Interactions tied to a Deployment:

```bash
indemn interaction list --filter '{"deployment_id": "<deployment_id>", "status": "active"}'
```

**Update the greeting or LLM override without re-deploying the harness.** Just update the Deployment record — the runtime reads fresh on each session start:

```bash
indemn deployment update <deployment_id> --data '{"greeting": "Welcome back!"}'
```

Changes take effect immediately for new sessions (existing sessions continue with their loaded config).

**Archive when retiring a placement.** Use the archived status (terminal) when you're permanently retiring a venue:

```bash
indemn deployment transition <deployment_id> --to archived
```

Archived Deployments are preserved for historical analytics (Interaction.deployment_id still resolves), but reject new sessions. Use `paused` instead if you might bring it back.

---

## 8. Auth Identity Model — Choosing `acts_as`

The biggest design decision per Deployment is the `acts_as` field. Use this decision tree:

**Does the user have an OS identity (actor_id)?**

- **Yes** (Indemn employee, Branch employee with sub-org actor, etc.):
  - Use `acts_as = session_actor`
  - The agent's CLI calls run with the user's permissions
  - The user's permission boundary is preserved through the agent (no privilege escalation via the agent)
  - Required: `parameter_schema` must include `actor_id` as required
  - At session start: the runtime extracts `actor_id` from the validated JWT; if `dynamic_params.actor_id` is provided, it MUST equal the JWT's actor (mismatch = reject)

- **No** (anonymous web visitor, customer policyholder with no Indemn identity):
  - Use `acts_as = associate_self`
  - The agent's CLI calls run with the associate's own permissions
  - The associate's role must be narrow enough that anyone who can talk to this Deployment should be able to do everything the agent can do
  - Per-session scoping comes from `deployment_context` (e.g., `customer_id` limits what the agent looks at), not from auth

**Why the rule about JWT-actor matching `dynamic_params.actor_id`?**

This is the load-bearing security gate. Without it, an attacker could supply `dynamic_params.actor_id = <someone_else>` to impersonate another actor. The runtime must extract `actor_id` from the authenticated JWT (which the runtime validated server-side), NOT trust it from the request body.

---

## 9. Common Patterns

**One associate, many venues (the canonical pattern):**

```
Sales Assistant Actor
  ├── Deployment: Sales-Web (sales.indemn.ai) — chat
  ├── Deployment: Sales-Voice (sales.indemn.ai) — voice
  ├── Deployment: Branch-Renewal — chat for Branch's portal
  └── Deployment: GIC-Quote — chat for GIC's quote page
```

Different SurfaceConfigs (different brands + vendor configs). Different `parameter_schema`s. Different greetings. Same associate underneath.

**Multi-channel on the same surface (chat + voice on one page):**

```
Sales Assistant Actor
  ├── Deployment: Sales-Web (chat) — uses indemn-runtime-chat
  └── Deployment: Sales-Voice (voice) — uses indemn-runtime-voice
```

Two Deployments. The page embeds both via the embed.js SDK; the user picks which to use.

**Multiple associates on one page:**

```
Page (e.g., customer dashboard):
  ├── Deployment for "Renewal Assistant" (chat widget — bottom right)
  └── Deployment for "Claims Q&A" (chat widget — top right)
```

Two Deployments, each pointing to a different associate. Same page, two embedded widgets.

**Per-customer brand + venue customization:**

Each customer (Branch, GIC, Acme) typically gets their own BrandAssets record + their own SurfaceConfig (so the widget looks like THEIR brand, not Indemn's). The Deployment record carries the `customer_id` in `static_parameters` so the agent knows what tenant it's serving — no leakage across tenants.

---

## 10. Testing Before Going Live

**Test in `configured` state.** Create the Deployment in `configured` status, then craft a test request:

```bash
# This will fail because the deployment isn't active yet
curl -X POST https://indemn-runtime-voice-frontdoor.up.railway.app/sessions \
  -H "Authorization: Bearer <test-jwt>" \
  -H "Origin: https://test.example.com" \
  -H "Content-Type: application/json" \
  -d '{"deployment_id": "<id>", "dynamic_params": {...}}'
```

Expected: HTTP 409 `{"error": "deployment_not_active", "status": "configured"}`. This confirms the front door is correctly enforcing status.

**Transition to active, retry.** Expected: HTTP 200 with `{room_name, livekit_url, livekit_token, interaction_id}`. Use a LiveKit playground client (or your embed.js SDK) to join the room and verify the agent greets correctly.

**Verify metadata propagation.** Check LangSmith for the trace — `metadata.thread_id` should equal `correlation_id`; `metadata.deployment_id` should equal your Deployment's id; `metadata.interaction_id` should match what the runtime returned.

**Test the parameter contract.** Send a request with malformed `dynamic_params` — expected HTTP 400 (strict mode) or 200 with `validation_warnings` (forgiving mode). Send a request with an `actor_id` that doesn't match the JWT — expected HTTP 403 `{"error": "forbidden", "reason": "actor_mismatch"}`.

**Test resumption.** Open a session, disconnect, reconnect within `resumption_config.ttl_seconds` with `resume_interaction_id`. The agent should have the prior conversation context. Then disconnect, wait past TTL, retry — expected HTTP 410 `{"error": "resume_expired"}`.

---

## See Also

- [`../architecture/deployments.md`](../architecture/deployments.md) — full entity design + the venue model
- [`../architecture/realtime.md`](../architecture/realtime.md) — Attention, Runtime, scoped watches, harness lifecycle
- [`../architecture/observability.md`](../architecture/observability.md) — correlation_id + interaction_id + LangSmith thread_id rule
- [`adding-associates.md`](adding-associates.md) — creating the underlying Actor + skill before placing it
- [`../white-paper.md`](../white-paper.md) — three-layer customer-facing flexibility + customer-facing surfaces as separate products
