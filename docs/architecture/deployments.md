# Deployments, SurfaceConfigs, and BrandAssets

This document covers three kernel entities that together describe **how an associate is placed in the world** — where end-users encounter it, what they see, what initialization context the associate receives. A senior developer who has never seen this part of the system should understand the venue model, the Deployment entity, the SurfaceConfig + BrandAssets supporting entities, the session lifecycle, the auth identity model, and the embed.js SDK pattern after reading this document.

---

## The Venue Model

The OS draws a clean distinction between **the associate** (what the agent does) and **the placement** (where end-users encounter it).

- **Actor (associate)** is the abstract agent. One Actor record. One skill. One role's permissions. Same code, same prompt, same tools regardless of where it shows up.
- **Deployment** is a placement of that associate — a venue. The same Sales Assistant might be deployed on Branch's customer portal, on GIC's renewal page, on Indemn's internal sales-team UI, and as a phone agent reachable at a specific number. Four Deployments. One associate.

What differs per Deployment:
- The visual surface (a chat widget vs a voice button vs a Slack thread)
- The initialization context (Branch's customer_id + policy_id vs sales rep's actor_id vs nothing)
- The greeting ("Welcome to your renewal" vs "Hi, what proposal can I help you build?")
- Per-deployment LLM tuning (a "more cautious" model for customer-facing surfaces, faster model for internal)
- Whose permissions are enforced (the user driving the conversation, or the associate itself)
- Which brand colors + logo are rendered

What stays the same per associate across Deployments:
- The agent's reasoning, skill, tools, and conversation style
- The role's permissions baseline (what the associate CAN do, in principle)
- The execution environment (the Runtime + framework)

This is the "one associate, many venues" pattern. It lets us build a single set of capable agents and place them anywhere users need them, without rebuilding the agent per venue.

---

## The Three Entities

### Deployment

A `Deployment` is a kernel entity representing one specific placement of one associate. It binds the associate to a Runtime (which determines the channel), points at a SurfaceConfig (which configures the visual presentation), and carries the per-venue configuration (greeting, parameter contract, LLM override, auth identity policy).

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable identifier (e.g., "Sales Assistant — Web") |
| `associate_id` | ObjectId | The Actor (associate) being deployed |
| `runtime_id` | ObjectId | The Runtime that serves this Deployment. The Runtime's `kind` determines the channel. |
| `surface_config_id` | Optional ObjectId | Reference to a SurfaceConfig for UI-having Deployments. Omitted for non-surface placements (e.g., async fetcher placements per the long-term direction). |
| `parameter_schema` | object | JSON Schema (draft 2020-12) describing what the surface must/may pass at session start. |
| `static_parameters` | dict | Values baked into this Deployment, constant across all sessions. |
| `parameter_schema_validation_mode` | enum | `strict` or `forgiving` — what happens when dynamic params fail validation. |
| `llm_override` | dict | Per-deployment overrides on the three-layer LLM merge. |
| `greeting` | string | Opening text the harness speaks/sends at session start. |
| `acts_as` | enum | `session_actor` or `associate_self` — see Auth Identity below. |
| `allowed_origins` | list[string] | CORS allowlist; empty list rejects all origins. |
| `resumption_config` | dict | `{ttl_seconds, kill_on_resume}` for reconnection policy. |
| `status` | enum | `configured → active → paused → active`, plus `error` (recovery: `error → configured`) and terminal `archived`. |
| `org_id` | ObjectId | Standard org isolation. |

Plus standard audit fields (`created_at`, `updated_at`, version for optimistic concurrency).

**Required indexes:**
- `(org_id, name)` unique
- `(org_id, associate_id, status)` — find active Deployments of an associate
- `(org_id, runtime_id, status)` — find active Deployments served by a Runtime
- `(org_id, status)` — list active Deployments in an org

**State machine:**

```
configured --> active --> paused --> active
                 |          |          |
                 +----------+----------+--> archived
                 |          |
                 +-> error -+
                    (recovery: error -> configured)
```

| State | Meaning |
|-------|---------|
| `configured` | Created but not yet accepting sessions. Use for staging / dry-run. |
| `active` | Accepting sessions. Runtime opens connections normally. |
| `paused` | Not accepting new sessions. Existing sessions continue to completion. Use during incident response or A/B test off-periods. |
| `error` | Health failure. Sessions rejected. Recovery via investigation + transition to `configured`. |
| `archived` | Permanently retired. The record stays for historical analytics. |

Implementation: `kernel_entities/deployment.py`.

### SurfaceConfig

A `SurfaceConfig` is the visual + vendor configuration for a Deployment's UI. Same associate on Branch's portal and on GIC's portal needs two different SurfaceConfigs (different brand styling). Same associate on a chat widget and on a voice widget needs two different SurfaceConfigs (different vendor — `prompt-kit` for chat, `livekit` for voice).

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Identifier (e.g., "Indemn Sales — prompt-kit chat") |
| `channel_kind` | enum | `chat`, `voice`, `slack`, `email`, `teams`, `sms` |
| `vendor` | string | `prompt-kit`, `livekit`, `slack-api`, `gmail`, `msteams`, etc. |
| `config` | dict | Vendor-specific configuration, validated against a per-vendor JSON Schema at save_tracked time |
| `brand_assets_id` | Optional ObjectId | Reference to shared BrandAssets for colors/logo/fonts |
| `status` | enum | `configured → active → archived` |
| `org_id` | ObjectId | Standard org isolation |

Per-vendor JSON Schema files live at `indemn-os/schemas/surface_configs/{vendor}.schema.json`. The kernel validates `SurfaceConfig.config` against the appropriate schema based on `vendor`. **Adding a new vendor = adding a new schema file** — no Python class proliferation, no entity migration.

Example `config` for a prompt-kit chat SurfaceConfig:
```json
{
  "widget_position": "bottom-right",
  "primary_color_ref": "brand.primary",
  "show_header": true,
  "header_text": "Indemn Proposal Assistant",
  "input_placeholder": "Type to chat, or tap the mic…",
  "show_voice_toggle": true,
  "open_on_load": false
}
```

Example `config` for a LiveKit voice SurfaceConfig:
```json
{
  "widget_style": "floating-orb",
  "show_transcription": true,
  "show_waveform": true,
  "primary_color_ref": "brand.primary",
  "stt_provider": "deepgram",
  "stt_model": "nova-3",
  "tts_provider": "cartesia",
  "tts_model": "sonic-3",
  "tts_voice_id": "...",
  "vad": "silero",
  "interrupt_enabled": true,
  "max_endpointing_delay_ms": 2000
}
```

Implementation: `kernel_entities/surface_config.py`.

### BrandAssets

A `BrandAssets` record carries shared visual primitives reusable across many SurfaceConfigs (and many Deployments). One brand, many surface placements.

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | e.g., "Indemn Brand", "Branch Insurance Brand" |
| `logo_url` | string | URL to the logo asset |
| `favicon_url` | Optional string | URL to the favicon |
| `primary_color` | string | hex |
| `secondary_color` | string | hex |
| `accent_color` | string | hex |
| `font_family_heading` | string | |
| `font_family_body` | string | |
| `status` | enum | `active → archived` (simple lifecycle) |
| `org_id` | ObjectId | Standard org isolation |

No state machine beyond active/archived — BrandAssets are reference data, not behavior.

Implementation: `kernel_entities/brand_assets.py`.

---

## Relationships

```
Organization 1───* Actor (associate)        1───* Deployment 1───1 Runtime
                                            │
                                            *
                                            │
                                            └──► SurfaceConfig 1───* BrandAssets
                                                 (optional)        (optional)

                       Session 1───────* Interaction 1────────────► Deployment
                                                .deployment_id
                                                .correlation_id
                                                .channel_type (from runtime.kind)
```

- One Actor (associate) → many Deployments (the "one associate, many venues" pattern)
- One Deployment → one Runtime (the channel boundary)
- One Deployment → one SurfaceConfig (optional — only for UI-having Deployments)
- One SurfaceConfig → one BrandAssets (optional — for brand reuse)
- One BrandAssets → many SurfaceConfigs (reuse across vendors)
- One Deployment → many Interactions (each session)
- Each Interaction carries `deployment_id` (the placement), `correlation_id` (the lineage)

**Granularity rule:** one Deployment is per `(associate, channel/transport)`. The same associate on web chat AND voice on the same page = two Deployment records. Different associates on the same page = different Deployments.

---

## The Three-Layer Config Model (extended)

The OS supports per-session behavioral flexibility via three layers that merge at invocation time:

| Layer | Where it lives | What it configures |
|-------|----------------|-------------------|
| **Runtime** | Runtime entity | Default LLM provider/model, framework, capacity |
| **Associate** | Actor entity + skill documents | Conversation style, tools, persona, per-agent LLM override |
| **Deployment** | Deployment entity | Surface-specific config: branding (via SurfaceConfig), greeting, per-venue LLM override, parameter contract, auth identity (acts_as) |

**Merge order:** Runtime defaults → Associate override → Deployment override (shallow merge, last writer wins).

Code:
```python
def _merge_llm_config(runtime, associate, deployment):
    return {
        **(runtime.get("llm_config") or {}),
        **(associate.get("llm_config") or {}),
        **((deployment.get("llm_override") or {}) if deployment else {}),
    }
```

Implementation: `harnesses/_base/harness_common/` (the merge function is shared across all three harnesses) + `kernel/temporal/activities.py::load_actor()` (the async path).

---

## Parameter Contract — The Data the Surface Passes to the Associate

A Deployment defines what its surface MUST/MAY pass at session start. Two pieces:

**`parameter_schema`** — JSON Schema (draft 2020-12) describing the contract. Server-side validation via `jsonschema` Python library; client-side validation in embed.js SDK via `ajv`. Stored on the Deployment record.

Example for an internal sales-team Deployment:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["actor_id"],
  "properties": {
    "actor_id":      {"type": "string", "pattern": "^[0-9a-f]{24}$"},
    "current_route": {"type": "string"}
  },
  "additionalProperties": false
}
```

Example for a customer-facing renewal page Deployment:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["customer_id", "policy_id"],
  "properties": {
    "customer_id":  {"type": "string", "pattern": "^[0-9a-f]{24}$"},
    "policy_id":    {"type": "string", "pattern": "^[0-9a-f]{24}$"},
    "page_section": {"type": "string", "enum": ["overview", "documents", "billing"]}
  },
  "additionalProperties": false
}
```

**`static_parameters`** — values defined on the Deployment that don't change per session:
```python
static_parameters = {"role": "sales", "tenant": "indemn-internal", "language": "en"}
```

**Validation timing:**
- `parameter_schema` is validated at Deployment `save_tracked` time (must be syntactically valid JSON Schema)
- `static_parameters` is validated against `parameter_schema` at `save_tracked` time (catches operator errors)
- `dynamic_params` (the per-session values supplied by the surface) is validated at session start in the runtime — either in the HTTP `/sessions` endpoint (voice) or the WebSocket connect handler (chat)

**Validation failure policy:**
- `strict` mode (default for internal Deployments with `acts_as = session_actor`): reject the session with HTTP 400 / WebSocket close
- `forgiving` mode (default for public Deployments with `acts_as = associate_self`): open the session, attach `validation_warnings` to the deployment_context, let the agent decide

The mode is stored as `Deployment.parameter_schema_validation_mode`.

---

## How Parameters Reach the Agent — the `<deployment_context>` SystemMessage

At session start, the harness merges `static_parameters` + the surface-supplied `dynamic_params`, validates against `parameter_schema`, and composes a SystemMessage:

```python
ctx = {**deployment.static_parameters, **connection_dynamic_params}
validate(ctx, deployment.parameter_schema)
system_msg = SystemMessage(content=f"""<deployment_context>
{format_context(ctx)}
</deployment_context>

Read this block before responding. It tells you who the user is and what context this session has.""")
agent_input_messages = [system_msg, *conversation_history]
```

The SystemMessage is prepended **once at session start** and persisted via the MongoDB checkpointer (keyed by `interaction_id` for real-time sessions; see `observability.md` § thread_id semantics). It is NOT re-prepended on every turn — the checkpointer carries it forward across turns.

This pattern is one of three SystemMessages the harness composes at session start:
- `<skill>` — the associate's operating skill content (pre-fetched by the harness via `indemn skill get`; replaces the older "load via CLI on turn 1" pattern)
- `<deployment_context>` — the merged static + dynamic parameters for this session
- (For some channels) an additional behavioral framing message — but typically the agent's `system_prompt` parameter at build time handles this

The associate's skill is written assuming these SystemMessages exist. A typical voice skill begins: "Read the `<deployment_context>` block first — it tells you who you're talking with and what context this session has."

---

## Auth Identity — The `acts_as` Model

When the agent runs `indemn <verb>` CLI subprocesses during a session, whose permissions are enforced? Three candidates:

1. The associate's own actor (its own role, its own permissions)
2. The user driving the conversation (if they have an OS actor)
3. The runtime's service token (broad — appropriate for some channels, not others)

The OS uses `INDEMN_EFFECTIVE_ACTOR_ID` (existing env var) to control this. The Deployment's `acts_as` field determines what value the harness sets:

**`session_actor`** — the harness sets `INDEMN_EFFECTIVE_ACTOR_ID = dynamic_params.actor_id` per session. The agent's CLI calls are authenticated as the user driving the conversation. The user's permissions are enforced — preserving the user's permission boundaries through the agent. Required for: internal team UIs (where users have OS identities) and customer-employee portals (where Branch's CSR has an OS identity in their sub-org).

**`associate_self`** — the harness sets `INDEMN_EFFECTIVE_ACTOR_ID = associate_id`. The agent's CLI calls are authenticated as the associate itself, with the associate's own role. Required for: public surfaces where users have no OS identity (anonymous web visitors, customer policyholder portals). The associate's permissions must be narrow enough that "if anyone can talk to this Deployment, they should be able to do everything the agent can do." Scoping per-conversation comes from `deployment_context` (e.g., `customer_id` limits what the agent looks at via skill logic), not from auth.

**Default rule:** if `parameter_schema` requires `actor_id`, default `acts_as = session_actor`. Otherwise default `associate_self`. Operators can override explicitly.

**Security model — the load-bearing gate:**

`session_actor` is a NEW capability introduced with the Deployment entity. Before this design, every harness always set `INDEMN_EFFECTIVE_ACTOR_ID = associate_id` (verified in `harnesses/async-deepagents/main.py`). User-impersonation via the harness is new.

The critical rule: **the JWT-to-actor_id mapping must be authenticated server-side**, never trusted from `dynamic_params`. Specifically, the runtime's `POST /sessions` endpoint:

1. Validates the user's JWT (RS256, signing key from AWS Secrets, audience claim, expiry tolerance)
2. Extracts `actor_id` from the JWT's `sub` claim
3. If the request also supplies `dynamic_params.actor_id`, it MUST equal the JWT's actor — mismatch = reject with HTTP 403

This prevents an attacker from setting an arbitrary `actor_id` in `dynamic_params` to impersonate a different actor. It is the highest-stakes security gate in the entire Deployment system.

**Permission vs scope — two independent concerns:**

- **Auth** (impersonation): can the user see *any* of customer X's data? Enforced via `INDEMN_EFFECTIVE_ACTOR_ID`.
- **Scope** (deployment_context): should *this conversation* be about customer X? Enforced by the agent's skill (which uses `deployment_context.customer_id` to scope its queries).

A Branch customer-service rep can probably see hundreds of customers (auth). But this conversation should be about ONE customer (scope). Auth + scope together produce correct behavior. Don't conflate them.

**Audit lineage:**

When `acts_as = session_actor`, the changes collection records `actor_id = <user>` (the impersonated identity is what's enforced and what's audited). The fact that the work was done VIA the associate is recoverable through `correlation_id → Interaction → Interaction.deployment_id → Deployment.associate_id`. For frequent "show me everything this associate did" queries, an indexed denormalization (e.g., `Interaction.acting_associate_id`) can be added later — for v1, the indirect path is sufficient.

---

## Session Lifecycle (Real-Time)

A complete end-to-end real-time session flow:

```
1. User visits a venue page (a customer portal, sales.indemn.ai, etc.)
   |
2. The page's embedded JS SDK calls Indemn.deploy({deployment_id, params})
   |
3. SDK fetches Deployment's public metadata: GET /api/deployments/{id}/public
   Returns {channel_kind, runtime_endpoint, surface_config_summary,
            greeting, parameter_schema, acts_as, allowed_origins}
   |
4. SDK validates `params` against parameter_schema (client-side, via ajv)
   |
5. For voice channel: SDK calls POST {runtime_endpoint}/sessions
   with {deployment_id, dynamic_params} + Authorization: Bearer <jwt>.
   For chat channel: SDK opens WebSocket to {runtime_endpoint} and sends
   first message {type: "connect", deployment_id, dynamic_params, auth_token}.
   |
6. Runtime validates: Origin, JWT, Deployment.status=active,
   dynamic_params against parameter_schema, acts_as gate
   (if session_actor, verify JWT actor matches dynamic_params.actor_id)
   |
7. Runtime creates Interaction(deployment_id, correlation_id, created_by=<jwt-actor>)
   |
8. (Voice only) Runtime creates LiveKit room with metadata={deployment_id,
   dynamic_params, interaction_id, correlation_id}; AgentDispatches worker;
   mints participant token; returns to SDK
   |
9. SDK opens transport (WebSocket already open for chat; LiveKit JS SDK for voice)
   |
10. Worker picks up the session. Reads metadata. Sets env vars:
    INDEMN_SERVICE_TOKEN (the runtime's identity)
    INDEMN_EFFECTIVE_ACTOR_ID (per acts_as)
    INDEMN_CORRELATION_ID (the session's correlation_id)
   |
11. Worker loads (in parallel): Deployment, Associate, Runtime, SurfaceConfig,
    BrandAssets, Skill content
   |
12. Worker composes <skill> + <deployment_context> SystemMessages;
    builds deepagents agent; wraps in DeepagentsLLM adapter (for voice);
    wires MongoDB checkpointer keyed by interaction_id
   |
13. Worker opens Attention(purpose=real_time_session); starts heartbeat
    loop (30s); starts indemn events stream subprocess
   |
14. Worker plays/sends greeting from Deployment.greeting
   |
15. Conversation loop runs (turns -> agent.ainvoke with checkpointer +
    SystemMessages baked into the state)
   |
16. On disconnect: graceful close (or Attention TTL handles)
    Interaction stays open until resumption_config.ttl_seconds elapse
    OR is explicitly closed
```

---

## Resumability

If a Deployment has `resumption_config.ttl_seconds > 0`, sessions are resumable for that window. The mechanism:

1. **Disconnect:** Surface UI retains the `interaction_id` returned at session start.
2. **Reconnect:** SDK calls `POST /sessions` (voice) or sends connect message (chat) with `resume_interaction_id` set.
3. **Runtime validates:**
   - Authenticate JWT
   - Look up Interaction by `resume_interaction_id`
   - **Critical security check:** verify `Interaction.created_by` matches the authenticated actor (mismatch = reject — resumption hijacking prevention)
   - Check Interaction age vs `Deployment.resumption_config.ttl_seconds` (expired = reject with HTTP 410)
   - Check Interaction status (closed/archived = reject)
4. **Handle prior worker:** if the prior LiveKit room still exists AND `kill_on_resume = true` (the v1 default), signal the prior agent participant to gracefully disconnect. If `false`, attempt to reuse the existing room (race-condition risk).
5. **Create new transport instance** with the SAME interaction_id in metadata.
6. **Worker re-loads:** Same `VoiceSession.start(interaction_id=<existing>)`. The MongoDB checkpointer keyed by `interaction_id` restores the full prior conversation state.
7. **Agent's first invocation after resume:** sees the SystemMessages (skill + deployment_context) PLUS the prior conversation messages from the checkpointer. The agent's skill should include guidance for the "continuing where we left off" UX.

**Race conditions:** if two reconnect requests race for the same interaction_id (network glitch + retry), the Interaction's optimistic concurrency check + the kill_on_resume mechanism prevent double-bind. Worst case: the second request fails gracefully and retries.

**Cross-channel resume (future):** because resume keys by `interaction_id` and the new transport carries it as room/connect metadata, a voice session could be resumed as a chat session (or vice versa) — same Interaction, same correlation_id, same checkpointer state. Not in v1 scope but the mechanism supports it.

---

## The embed.js SDK Pattern

Customer-facing surfaces (and internal product UIs like sales.indemn.ai) embed Indemn agents via a thin JS SDK. The pattern mirrors the V1 customer-deploys-script-tag model and carries over to the OS:

```html
<script src="https://cdn.indemn.ai/embed.js"></script>
<script>
  Indemn.deploy({
    deployment_id: "dep_xxx",
    params: { customer_id: "abc123" }
  });
</script>
```

The SDK:

1. Fetches the Deployment's public metadata from `GET /api/deployments/{id}/public` — returns the surface-safe field subset (`channel_kind`, `runtime_endpoint`, `surface_config_summary`, `greeting`, `parameter_schema`, `acts_as`, `allowed_origins`).
2. Validates `params` against `parameter_schema` client-side (using `ajv`).
3. Reads `channel_kind`:
   - `chat` → opens WebSocket to `runtime_endpoint` with the connect message
   - `voice` → calls `POST {runtime_endpoint}/sessions` → gets LiveKit creds → joins LiveKit room via LiveKit JS SDK
   - `slack`/`teams`/etc. (future) → uses the appropriate vendor's SDK
4. Renders the channel-appropriate UI (chat widget, voice button) styled per the SurfaceConfig + BrandAssets.

For internal Indemn products (sales.indemn.ai, the OS Base UI), the same SDK powers more elaborate React apps' "Start chat" / "Start voice" buttons. Different surface, same SDK — same connection mechanics.

**Why this matters architecturally:** customer-facing surfaces are SEPARATE PRODUCTS built on the OS primitives (per the white paper § 5). They don't run inside the kernel. The SDK is the contract between the OS and any surface — customer site, partner integration, internal team UI. One contract; many surfaces.

---

## Putting It All Together — Worked Example

Indemn deploys the Sales Assistant in three places:

```
Actor: "Sales Assistant" (one associate)
   |
   ├── Deployment: Sales-Web   ──► Runtime: indemn-runtime-chat
   |   ├── SurfaceConfig: "Indemn Sales Light — prompt-kit chat"
   |   |   └── BrandAssets: "Indemn Brand"
   |   ├── parameter_schema: {actor_id: required}
   |   ├── static_parameters: {role: "sales", tenant: "indemn-internal"}
   |   ├── greeting: "Hi! What proposal can I help you build?"
   |   ├── acts_as: session_actor
   |   ├── allowed_origins: ["https://sales.indemn.ai"]
   |
   ├── Deployment: Sales-Voice ──► Runtime: indemn-runtime-voice
   |   ├── SurfaceConfig: "Indemn Sales — livekit voice"
   |   |   └── BrandAssets: "Indemn Brand"
   |   ├── parameter_schema: {actor_id: required}
   |   ├── static_parameters: {role: "sales", tenant: "indemn-internal"}
   |   ├── greeting: "Hi, this is your proposal assistant. Who are we writing for?"
   |   ├── acts_as: session_actor
   |   ├── allowed_origins: ["https://sales.indemn.ai"]
   |
   └── Deployment: Branch-Renewal ──► Runtime: indemn-runtime-chat
       ├── SurfaceConfig: "Branch Renewal Widget — prompt-kit chat"
       |   └── BrandAssets: "Branch Insurance Brand"
       ├── parameter_schema: {customer_id: required, policy_id: required}
       ├── static_parameters: {tenant: "branch-insurance"}
       ├── greeting: "Welcome to your renewal — how can I help?"
       ├── acts_as: session_actor  (Branch employees authenticate via their sub-org JWTs)
       ├── allowed_origins: ["https://branch.example.com"]
```

Same Sales Assistant Actor underneath. Three different placements with different visual styling, different initialization context, different greetings, different allowed origins. Two share the same Runtime (`indemn-runtime-chat`); the voice Deployment uses a different Runtime.

Each Deployment's surface knows its own `deployment_id` (hardcoded in the embed snippet or fetched from the OS at page load). The SDK does the rest.

---

## Design Decisions

### Why Deployment is a Separate Entity (Not a Field on Actor)

Earlier iterations had `Actor.deployment_id` — a single Deployment per Actor. That broke the "one associate, many venues" pattern: the same Sales Assistant can't have both a chat AND a voice placement, nor be deployed on Branch's site AND GIC's site simultaneously, if there's only one slot. Deployment had to become its own entity with N:1 relationship to Actor.

### Why SurfaceConfig is Separate from Deployment

SurfaceConfig is per-vendor-and-brand. Deployment is per-placement. They have different lifecycles (designer owns themes; ops owns deployments), different update cadences (themes change rarely; deployments come and go), different reuse patterns (one theme used across many deployments). Embedding visual config inline on Deployment would duplicate "Indemn Sales Light" across N Deployment records and force N updates when the brand changes.

### Why JSON Schema for `parameter_schema` and `SurfaceConfig.config`

- Industry standard — mature server-side (`jsonschema` Python) and client-side (`ajv` JS) libraries
- Self-describing — the schema itself is queryable + introspectable by the embed.js SDK via `/api/deployments/{id}/public`
- New vendor support = drop a new schema file (no Python class proliferation, no entity migration)
- Strict-by-default validation (`additionalProperties: false`)

### Why `acts_as` Is on Deployment (Not on Actor or Runtime)

Auth identity is a venue concern, not an associate concern. The same Sales Assistant should act as the user's identity when deployed internally (where users have OS identities) but as itself when deployed publicly (where users don't). Putting `acts_as` on the associate would lock all the associate's placements to one auth model. Putting it on Runtime would couple the auth model to the channel. Putting it on Deployment lets each placement choose appropriately.

### Why Runtime-as-Front-Door (Not API as Mediator)

Each runtime owns its channel's protocol — chat-deepagents IS its own WebSocket server today; the new voice runtime grows an HTTP `/sessions` front door + LiveKit Agents worker. The OS API kernel is channel-agnostic — it manages Deployment + SurfaceConfig + Associate records via auto-generated CRUD; it does NOT mediate session-start traffic. This matches the OS principle: kernel manages entities; harnesses bridge to specific transports.

### Why the Voice Runtime Is Two Railway Services (Not One)

LiveKit Agents' `cli.run_app(WorkerOptions(...))` owns the asyncio event loop in its worker process. Adding a Starlette HTTP server to the same process is awkward — they fight for event-loop ownership. Two Railway services (`indemn-runtime-voice-frontdoor` + `indemn-runtime-voice-worker`) gives clean separation: each owns its own process model; they share LiveKit API credentials via AWS Secrets; they communicate through LiveKit (the frontdoor mints tokens + dispatches via AgentDispatch; the worker handles the per-room job).

### Why Pre-Fetch Skill Content (Not Load via CLI on Turn 1)

Latency. For voice especially, the first turn matters — the user is waiting to be heard. Loading the skill via CLI on turn 1 adds ~300-500ms (one subprocess + one LLM round-trip). Pre-fetching at session start eliminates the turn-1 fetch entirely. The skill content is prepended as a SystemMessage at session start and persisted via MongoDB checkpointer — present on every subsequent turn without re-fetching.

### Why the MongoDB Checkpointer Key Differs by Channel

Real-time sessions accumulate state across turns within one session — checkpointer thread_id = `interaction_id`. Async cascades have independent agent invocations on different entities — checkpointer thread_id = `message_id` (each invocation is its own clean state; no cross-contamination between cascade hops). This is the "the checkpointer key tracks the SUBJECT of the work" rule. See `observability.md` for the full thread_id semantics including LangSmith metadata.

---

## See Also

- [`realtime.md`](realtime.md) — Attention entity, Runtime entity, scoped watches, harness lifecycle, three-layer config
- [`associates.md`](associates.md) — actor model, harness pattern, skill loading, three-layer config from the associate's perspective
- [`observability.md`](observability.md) — correlation_id, interaction_id, message_id semantics, LangSmith thread_id rule
- [`overview.md`](overview.md) — entity inventory, kernel architecture
- [`../guides/adding-deployments.md`](../guides/adding-deployments.md) — practical how-to guide
- [White paper § 2 + § 5](../white-paper.md) — three-layer customer-facing flexibility; customer-facing surfaces as separate products
