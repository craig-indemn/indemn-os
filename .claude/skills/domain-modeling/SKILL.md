---
name: domain-modeling
description: The 8-step process for building any domain on the Indemn OS. Use when creating a new business domain (CRM, GIC, EventGuard), defining entities, setting up associates, or onboarding a customer.
---

# Domain Modeling — Building on the Indemn OS

## The Process

Every domain follows the same 8 steps. No shortcuts.

### Step 1: Understand the Business

Before touching the OS, understand what the business actually does.

- What workflows happen daily?
- Who are the people and what are their roles?
- What systems do they currently use?
- What's painful? What takes too long? What falls through cracks?

**Output**: A narrative description. Not technical. Just "here's how this business works."

### Step 2: Identify Entities

What are the nouns? What data does this business create, manage, and act on?

For each entity:
- **Fields**: what data does it carry?
- **Lifecycle** (state machine): what states does it go through?
- **Relationships**: what other entities does it connect to?

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

**Test**: Can you describe the business entirely in terms of these entities and their states?

### Step 3: Identify Roles and Actors

Who participates? What do they care about?

For each role:
- **Permissions**: what entities can they read/write?
- **Watches**: what entity changes matter to them?

```bash
indemn role create --data '{
  "name": "underwriter",
  "permissions": {"read": ["Submission", "Carrier"], "write": ["Submission"]},
  "watches": [{
    "entity_type": "Submission",
    "event": "transitioned",
    "conditions": {"field": "status", "op": "equals", "value": "triaging"}
  }]
}'
```

**Test**: For every entity state change, is there a role whose watch catches it?

### Step 4: Define Rules and Configuration

What per-org business logic exists? Rules parameterize kernel capabilities.

```bash
indemn rule create --data '{
  "entity_type": "Submission",
  "capability": "auto_classify",
  "name": "carrier-routing",
  "conditions": {"field": "lob", "op": "equals", "value": "GL"},
  "action": "set_fields",
  "sets": {"carrier_id": "<carrier_objectid>"},
  "priority": 100
}'
```

**Test**: Can you trace through the common case entirely with rules (no LLM)?

### Step 5: Write Skills

For each associate role: behavioral instructions in markdown.

```bash
indemn skill create --name submission-classifier \
  --content-from-file skills/submission-classifier.md
```

Skill content should describe:
- What the associate does
- What CLI commands it uses
- When it needs reasoning vs following a procedure
- What entities it works with and how

**Test**: Can a human reading the skill understand what the associate does?

### Step 6: Set Up Integrations

What external systems does this domain connect to?

```bash
indemn integration create --name "Outlook" --system-type email \
  --provider outlook --owner-type org
indemn integration set-credentials <id> \
  --secret-ref indemn/prod/integrations/outlook-oauth
```

For each: org-level or actor-level? Which adapter exists? If no adapter exists, that's a kernel contribution.

### Step 7: Test in Staging

```bash
# Create staging org
indemn org clone --from _template --to staging-acme

# Apply configuration
indemn platform seed --org-id <staging_id> --dir seed/acme/

# Verify watches fire
indemn trace entity Submission <test_id>
indemn trace cascade <correlation_id>

# Check queue
indemn queue stats
```

**Validate**: watches fire, associates process correctly, humans see the right queue items.

### Step 8: Deploy and Tune

```bash
indemn org deploy --from staging-acme --to acme --apply
```

Monitor. Tune rules based on real data. The `needs_reasoning` rate tells you which deterministic rules are missing. The system gets more deterministic over time.

## The Universal Pattern

All systems follow this:

```
Entry point (email, webhook, chat, form, schedule)
  → Creates an entity
    → Watches fire
      → Associates process (deterministic first, reasoning if needed)
        → Entity state changes
          → More watches fire
            → Eventually reaches a human checkpoint or final state
```

## Verification Checklist

After completing all 8 steps:
- [ ] Every entity has fields, lifecycle, and relationships defined
- [ ] Every state change has a watch that catches it
- [ ] Every associate has a skill that describes its behavior
- [ ] Rules handle the common cases deterministically
- [ ] Integrations are connected and healthy (`indemn integration health`)
- [ ] End-to-end trace shows the full cascade (`indemn trace cascade`)
- [ ] Queue shows work flowing to the right roles (`indemn queue stats`)
