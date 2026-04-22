# Domain Modeling Guide

How to model any business on the Indemn OS. This is a hands-on, 8-step process that takes you from "we need to build a system for X" to a running, observable, tunable system.

The OS is domain-agnostic. The kernel provides entity management, message routing, state machines, rules, watches, audit trails, CLI, API, UI, and AI associate execution. Your job is to describe the domain -- the kernel does the rest.

---

## The Universal Pattern

Before the steps, understand the pattern. Every system built on the OS works the same way:

```
Entry point (email, webhook, chat, form, API call, schedule)
  --> Creates or updates an entity
    --> Watches fire
      --> Actors process (deterministic first, reasoning if needed)
        --> Entity state changes
          --> More watches fire
            --> Eventually reaches a human checkpoint or a final state
```

This is the churning loop. Entity changes produce messages. Actors process messages by making entity changes. Those changes produce more messages. The system runs until it hits a human checkpoint or a final state.

Everything you build in the 8 steps is configuring this loop for your domain.

---

## Step 1: Understand the Business

You cannot model what you do not understand. Before opening a terminal, talk to the people who do the work.

### What to Map

| Area | Questions | Output |
|------|-----------|--------|
| **Narrative** | What does this business do day-to-day? What does a typical day look like? | A paragraph describing the work in their words, not yours |
| **Workflows** | What happens when X arrives? Then what? Who decides? Where does it go? | Sequence of steps, branching points, handoffs |
| **People** | Who participates? What are their roles? What decisions do they make? | Role list with responsibilities |
| **Pain points** | Where do things get stuck? What takes too long? What gets dropped? | Priority list of what to automate first |
| **Current systems** | What tools exist? What data lives where? What integrations matter? | Inventory of systems and data sources |

### How to Do It

Sit with the people. Watch them work. Ask "then what happens?" repeatedly until you reach the end. Write it down as a narrative, not a technical document.

**Good output:**

> JC and Maribel receive 100+ emails a day from carriers, agents, and policyholders. They manually open each one, figure out what it is (a quote, a claim, a renewal notice, a question), extract the relevant data, and route it to the right person. Most of their time is spent on carrier responses -- figuring out which submission a quote belongs to, pulling numbers from PDF attachments, and creating a record in their system.

**Bad output:**

> Email entity with classification field, enum values: quote, claim, renewal, inquiry.

The second one is a solution. You do not have a solution yet. You have a business to understand.

### Resist the Urge to Model

Step 1 has no CLI commands. No entity definitions. No state machines. If you find yourself writing JSON, stop. Go back to the narrative.

---

## Step 2: Identify Entities

Now you have a narrative understanding of the business. Walk through it and identify the things with identity and lifecycle.

### The 7-Test Entity Criteria

For every candidate noun in your narrative, apply all seven tests:

| # | Test | Question | If the answer is NO |
|---|------|----------|---------------------|
| 1 | **Identity** | Does it have a unique identity (name, ID, reference number) that people use to refer to it? | It is a field on something else |
| 2 | **Lifecycle** | Does it have meaningful states that change over time? | It is a field with an enum, not a separate entity |
| 3 | **Independence** | Can it exist on its own, not purely as a property of another thing? | It is a child field or embedded document |
| 4 | **Not kernel mechanism** | Is it business data, not connective tissue the kernel already provides? | The kernel handles it -- do not model it |
| 5 | **CLI test** | Would someone naturally say `indemn <thing> list` or `indemn <thing> get <id>`? | It is probably a field |
| 6 | **Watchable** | Should changes to this thing notify people or trigger work? | Maybe a field is sufficient |
| 7 | **Multiplicity** | Can there be many per parent? | It is a section of the parent entity, not its own entity |

A candidate should pass most of these tests to be an entity. Not every test is equally weighted -- identity and lifecycle are the strongest signals.

### Design Principles

**Entities are cheap.** The OS auto-generates CLI commands, API endpoints, UI views, skill documentation, permissions checks, and audit trail from every entity definition. A richer model means more capability, not more burden.

**AI populates everything.** Design for extraction, not manual entry. Your entities will be populated by AI processing meetings, emails, documents, and integrations. Fields should be things AI can classify or extract.

**Enums over free text.** AI classifies more reliably into defined categories than it generates consistent free text. Wherever you are tempted to use a string, ask: is there a finite set of values this should be? If yes, use an enum.

**If it passes the criteria, make it an entity.** Do not cram things into fields on other entities to keep the model small. The OS is designed for rich models.

### What NOT to Model as Entities

The kernel provides these mechanisms. Do not recreate them as domain entities:

| Do Not Model | Kernel Provides |
|---|---|
| Activity log / history | Changes collection -- field-level diffs, hash-chained, append-only |
| Notifications / alerts | Watches on roles -- fire when entity changes match conditions |
| Team member identity | Actor kernel entity -- humans, associates, and tier-3 developers |
| Account ownership | Role assignments with watch scoping |
| Audit trail | Changes collection -- tamper-evident, includes who/what/when/why |
| Communication / messaging | Message queue + watches -- the nervous system of the OS |
| Permissions / access control | Roles with per-entity-type read/write permissions |
| Scheduled tasks | Associate trigger_schedule (cron) on Actor entity |

If you catch yourself defining an entity called "ActivityLog", "Notification", "AuditRecord", or "Permission", stop. The kernel has it.

### Creating Entity Definitions

Once you have identified your entities, define them via CLI:

```bash
indemn entity create --data '{
  "name": "Submission",
  "collection_name": "submissions",
  "fields": {
    "title": {"type": "str", "required": true},
    "carrier_id": {"type": "objectid", "is_relationship": true, "relationship_target": "Carrier"},
    "status": {"type": "str", "default": "received", "is_state_field": true},
    "lob": {"type": "str", "enum_values": ["general_liability", "workers_comp", "commercial_auto"]},
    "premium": {"type": "decimal"},
    "effective_date": {"type": "date"},
    "notes": {"type": "str"}
  },
  "state_machine": {
    "received": ["triaging"],
    "triaging": ["quoted", "declined", "needs_info"],
    "needs_info": ["triaging"],
    "quoted": ["bound", "expired"],
    "bound": [],
    "declined": [],
    "expired": []
  }
}'
```

### Field Type Reference

| Design Concept | OS Type | Field Definition Example |
|---|---|---|
| Text / name / description | `str` | `{"type": "str"}` |
| Text with defined values | `str` + enum | `{"type": "str", "enum_values": ["A", "B", "C"]}` |
| Whole number | `int` | `{"type": "int"}` |
| Money / decimal | `decimal` | `{"type": "decimal"}` |
| True / false | `bool` | `{"type": "bool"}` |
| Calendar date | `date` | `{"type": "date"}` |
| Date and time | `datetime` | `{"type": "datetime"}` |
| Reference to another entity | `objectid` | `{"type": "objectid", "is_relationship": true, "relationship_target": "Carrier"}` |
| List of strings / tags | `list` | `{"type": "list"}` |
| Arbitrary structured data | `dict` | `{"type": "dict"}` |

**Special field properties:**

| Property | What It Does |
|---|---|
| `required: true` | Field must be present on create |
| `default: <value>` | Value used when not provided |
| `is_state_field: true` | This field is the state machine field (one per entity) |
| `is_relationship: true` | This field references another entity |
| `relationship_target: "<EntityName>"` | Which entity type the reference points to |
| `enum_values: [...]` | Restrict to these values |

### State Machine Design

State machines enforce lifecycle. Define them as a map of `state -> [allowed next states]`:

```json
{
  "received": ["triaging"],
  "triaging": ["quoted", "declined"],
  "quoted": ["bound", "expired"],
  "bound": [],
  "declined": [],
  "expired": []
}
```

Rules:

- An empty array `[]` means a terminal state -- no transitions out.
- Transitions are enforced by the kernel. Attempting an invalid transition raises an error.
- State transitions emit `transitioned` events that watches can match on.
- A state change is the most common trigger for downstream work.

**Design tips:**

- States should represent meaningful milestones, not micro-steps. If two states always transition in sequence with no branching, they might be one state.
- Terminal states should be explicit. If something can be `completed`, `cancelled`, `expired`, or `lost`, those are all separate terminal states.
- Branching points are where the model gets interesting. `triaging` can go to `quoted` OR `declined` -- that is a decision point that might be automated.

### Reference Entities

Some entities are small, stable lookup tables -- not business objects with lifecycle. They still pass the entity criteria (identity, independence, multiplicity) but serve as reference data.

```bash
indemn entity create --data '{
  "name": "Stage",
  "collection_name": "stages",
  "fields": {
    "name": {"type": "str", "required": true},
    "probability": {"type": "decimal"},
    "stale_after_days": {"type": "int"},
    "order": {"type": "int"},
    "definition": {"type": "str"}
  }
}'
```

Reference entities often have no state machine. They are populated once (or rarely updated) and referenced by many other entities.

---

## Step 3: Identify Roles and Actors

Roles determine two things: what an actor can access (permissions) and what flows to them (watches). Every system behavior is the set of watches across all roles.

### Thinking About Roles

Walk through your narrative from Step 1. For every action someone takes, ask:

- **Who** does this?
- **What** do they need to see to do it?
- **What** should they be notified about?

Each distinct combination of permissions and watches is a role.

### Role Structure

A role has:

| Component | What It Is |
|---|---|
| **Permissions** | Which entity types can actors in this role read? Write? |
| **Watches** | Which entity changes should produce messages for actors in this role? |

### Start Simple

Begin with one role that has full access. Differentiate later as you understand the access patterns.

```bash
# Start with an admin role
indemn role create --data '{
  "name": "admin",
  "permissions": {
    "read": ["Email", "Submission", "Carrier", "Assessment", "Draft"],
    "write": ["Email", "Submission", "Carrier", "Assessment", "Draft"]
  },
  "watches": []
}'
```

Then add specialized roles as you define associates:

```bash
# A classifier role -- reads emails, writes classification results
indemn role create --data '{
  "name": "email_classifier",
  "permissions": {
    "read": ["Email", "Submission", "Carrier"],
    "write": ["Email"]
  },
  "watches": [{
    "entity_type": "Email",
    "event": "created",
    "conditions": {"field": "status", "op": "equals", "value": "received"}
  }]
}'
```

### Watch Design

Watches are the wiring. They declare what entity changes matter to a role.

A watch is: **entity type + event type + optional conditions + optional scope**.

```bash
# Watch: notify when a submission is created
indemn role add-watch underwriter --entity Submission --on created

# Watch: notify when an assessment needs review
indemn role add-watch underwriter --entity Assessment --on created \
  --when '{"field": "needs_review", "op": "equals", "value": true}'

# Watch: notify the specific account owner when their deal goes stale
indemn role add-watch account_owner --entity Deal --on "method:stale_check" \
  --scope '{"type": "field_path", "path": "company.owner_id"}'
```

**Event types** a watch can match:

| Event | Fires When |
|---|---|
| `created` | A new entity is inserted |
| `transitioned` | A state machine transition occurred |
| `transitioned:<state>` | Transition specifically to that state (e.g., `transitioned:quoted`) |
| `method_invoked` | An `@exposed` method was called |
| `method:<name>` | A specific method was called (e.g., `method:classify`) |
| `fields_changed` | Any exposed method changed fields |
| `deleted` | An entity is removed |

### The Coverage Test

For every entity state change in your model, there should be a role whose watch catches it. Walk through each state machine transition and ask: "Who needs to know about this?"

If nobody needs to know, the transition might be unnecessary. If someone needs to know but no watch covers it, add one.

```bash
# Verify: list all watches across all roles
indemn role list --show-watches
```

This is the complete wiring diagram of your system. There is no hidden routing.

### Actors

Actors are participants. Create them after roles exist.

```bash
# Human actor
indemn actor create --type human --name "JC" --email jc@gic.com
indemn actor add-role <actor_id> --role admin

# Associate actor (AI)
indemn actor create --type associate --name "Email Classifier" \
  --mode hybrid --role email_classifier \
  --skills '["email-classification"]' \
  --runtime-id <runtime_id>
indemn actor transition <actor_id> --to active
```

Associates get their own roles with specific watches. This is how gradual rollout works -- add an associate to a role alongside a human. Both see the same queue. Remove the human when the associate is trusted.

---

## Step 4: Define Rules and Configuration

Rules are per-org business logic that determines behavior without code. They are evaluated before AI -- this is the `--auto` pattern (rules first, LLM fallback).

### Rules

Rules have exactly two actions:

| Action | What It Does | Use When |
|---|---|---|
| `set_fields` | Applies field values deterministically | The answer is known for this pattern |
| `force_reasoning` | Vetoes all deterministic matches, forces LLM | This pattern LOOKS simple but needs judgment |

```bash
# set_fields: emails from known carrier domains get auto-classified
indemn rule create \
  --entity Email \
  --capability auto_classify \
  --name known-carrier-domain \
  --when '{"field": "sender_domain", "op": "in", "value": ["usli.com", "markel.com", "employers.com"]}' \
  --action set_fields \
  --sets '{"classification": "carrier_response"}' \
  --priority 200

# force_reasoning: even from known carriers, complaints need judgment
indemn rule create \
  --entity Email \
  --capability auto_classify \
  --name carrier-complaint-veto \
  --when '{"all": [
    {"field": "sender_domain", "op": "in", "value": ["usli.com", "markel.com"]},
    {"field": "subject", "op": "contains", "value": "complaint"}
  ]}' \
  --action force_reasoning \
  --forces-reasoning-reason "Carrier complaints require judgment even from known domains" \
  --priority 300
```

**Priority** determines evaluation order. Higher priority evaluates first. A `force_reasoning` rule at priority 300 vetoes a `set_fields` rule at priority 200 even if both match.

### Lookups

Lookups are key-value mapping tables that prevent rule explosion.

Without a lookup: 47 carrier prefix codes require 47 rules.
With a lookup: 1 rule references a lookup table with 47 entries.

```bash
# Create a lookup from inline JSON
indemn lookup create --name carrier-prefix-lob \
  --data '{"MGL": "general_liability", "WC": "workers_comp", "BOP": "business_owners"}'

# Or import from CSV
indemn lookup import carrier-prefix-lob --from-csv ./carrier-codes.csv

# Reference the lookup in a rule
indemn rule create \
  --entity Submission \
  --capability auto_classify \
  --name prefix-to-lob \
  --when '{"field": "quote_prefix", "op": "exists", "value": true}' \
  --action set_fields \
  --sets '{"line_of_business": {"lookup": "carrier-prefix-lob", "from_field": "quote_prefix"}}' \
  --priority 150
```

CSV format (first column = key, second column = value):

```csv
code,line_of_business
MGL,general_liability
WC,workers_comp
BOP,business_owners
CPP,commercial_package
```

### Capability Activation

Capabilities are reusable operations any entity type can opt into:

```bash
# Enable auto-classification on Email entities
indemn entity enable Email auto_classify --config '{
  "target_fields": ["classification", "sub_classification"],
  "confidence_threshold": 0.8
}'

# Enable stale detection on Deal entities
indemn entity enable Deal stale_check --config '{
  "when": {"field": "days_since_activity", "op": "gt", "value": 14},
  "sets_field": "is_stale",
  "sets_value": true
}'
```

### The Test

Can you trace through the most common case using only rules? Walk through the happy path:

1. Entry point creates entity
2. Watch fires on role
3. Associate claims message
4. Associate invokes capability with `--auto`
5. Rule matches, sets fields
6. Entity saves, watch fires on next role
7. Repeat until terminal state

If you can trace this without "and then the LLM figures it out," your rules are working. The LLM handles the cases you have not written rules for yet.

---

## Step 5: Write Skills

Skills are markdown behavioral instructions for associates. They describe HOW the associate should behave when processing a message.

### Two Kinds of Skills

**Entity skills** -- auto-generated by the kernel. Every entity definition produces a skill that documents its fields, lifecycle, CLI commands, and capability commands. You do not write these.

```bash
# View the auto-generated skill for any entity
indemn skill get Email
indemn skill get Submission
```

**Associate skills** -- hand-written. These describe the associate's behavior: what it should do when it receives a message, how to use `--auto`, when to escalate, what quality standards to apply.

### Writing an Associate Skill

A skill is a markdown file that an LLM reads as instructions. Write it for a human-level reader -- clear, specific, with examples.

```bash
indemn skill create --name email-classification \
  --content-from-file skills/email-classification.md
```

Example skill content (`skills/email-classification.md`):

```markdown
# Email Classification

You are the Email Classifier. You process incoming emails and determine
what type of communication each one is.

## When You Receive a Message

1. Load the email: `indemn email get <entity_id>`
2. Attempt auto-classification: `indemn email classify <entity_id> --auto`
3. If the result is deterministic (needs_reasoning: false), you are done.
   The classification has been applied.
4. If needs_reasoning is true, analyze the email yourself:
   - Read the subject, sender, and body
   - Determine the classification from: carrier_response, agent_inquiry,
     policyholder_request, internal, spam, unknown
   - Apply it: `indemn email classify <entity_id> --classification <value>`

## Classification Definitions

- **carrier_response**: A reply from an insurance carrier about a submission,
  quote, endorsement, cancellation, or claim.
- **agent_inquiry**: A question or request from an insurance agent.
- **policyholder_request**: A direct message from a policyholder.
- **internal**: Communication between team members.
- **spam**: Unsolicited commercial email.
- **unknown**: Cannot determine with confidence. Flag for human review.

## Quality Standards

- Never classify as "unknown" if the email clearly fits another category.
- When uncertain between two categories, prefer the one with more specific
  downstream handling (carrier_response over agent_inquiry).
- If the email references a submission number, always check if a matching
  Submission entity exists before classifying.

## Escalation

If you encounter an email in a language other than English, or if the email
appears to contain legal threats, transition the email to `escalated` status
instead of classifying it.
```

### What Makes a Good Skill

| Quality | What It Means |
|---|---|
| **Specific** | Name the CLI commands. Name the enum values. Do not say "classify it appropriately." |
| **Sequential** | Number the steps. The associate follows them in order. |
| **Decisive** | Handle edge cases explicitly. "If X, do Y" not "use your judgment." |
| **Self-contained** | The skill references entities by name. CLI commands are in the auto-generated entity skill, but the associate skill says WHEN and WHY to use them. |

### The Test

Can a human reading the skill understand exactly what the associate does? Give it to a colleague. If they have questions about what the associate should do in a specific scenario, the skill is not complete enough.

---

## Step 6: Set Up Integrations

Integrations connect the OS to external systems -- email providers, payment processors, carrier portals, CRM systems.

### Creating an Integration

```bash
indemn integration create --data '{
  "name": "GIC Outlook",
  "system_type": "email",
  "provider": "outlook",
  "owner_type": "org",
  "config": {
    "tenant_id": "abc-123",
    "client_id": "def-456"
  }
}'
```

### Setting Credentials

Credentials NEVER live in MongoDB. They go to AWS Secrets Manager via the CLI:

```bash
indemn integration set-credentials <integration_id> \
  --secret-ref indemn/prod/integrations/gic-outlook
```

The `secret_ref` is a path in AWS Secrets Manager. The kernel fetches credentials at runtime. OAuth token refresh is handled automatically by the integration dispatch layer.

### Integration Lifecycle

1. Create in `configured` state
2. Set credentials, transition to `connected`
3. Test connectivity, transition to `active`
4. If health check fails, auto-transition to `error`
5. Pause temporarily with `paused`
6. Return to `configured` for re-setup

```bash
indemn integration transition <id> --to connected
indemn integration transition <id> --to active
```

### Adapter Check

The kernel resolves an adapter for each `provider` value. Check which adapters exist:

```bash
indemn integration list-adapters
```

If no adapter exists for your provider, you need to contribute one in `kernel/integration/adapters/`. Adapters implement the `Adapter` base class with methods for `fetch()`, `send()`, `health_check()`, and optionally `refresh_token()`.

### Ownership

| Owner Type | Use For | Access Control |
|---|---|---|
| `org` | Shared infrastructure (company email, carrier APIs, payment processors) | `access.roles` list controls which roles can use it |
| `actor` | Personal connections (personal email sync, personal calendar) | Only the owning actor (and their bound associates) |

---

## Step 7: Test in Staging

Before production, validate that the system works end-to-end.

### Clone an Org for Staging

```bash
indemn org clone <prod-org> --as <staging-org>
```

This creates a copy of the entire organization -- entity definitions, roles, watches, rules, lookups, skills. Entity data is NOT copied (start clean).

### Load Realistic Data

Seed the staging org with realistic test data. Use the entity create commands from Step 2 with real-world values:

```bash
# Create test entities
indemn email create --data '{
  "subject": "RE: MGL-2026-001 Quote",
  "sender": "underwriting@usli.com",
  "sender_domain": "usli.com",
  "body": "Please find attached the quote for...",
  "status": "received"
}'
```

### Validate the Full Cycle

Trace through the complete lifecycle of an entity and verify:

1. **Entity creation triggers watches.** When you create the entity, messages should appear in the queue for the right roles.

```bash
# Create the entity
indemn email create --data '{...}'

# Check the queue -- message should be pending for the classifier role
indemn queue stats
```

2. **State transitions trigger downstream work.** Transition an entity and verify new messages appear.

```bash
# Transition
indemn email transition <id> --to classified

# Check for downstream messages
indemn queue stats
```

3. **Rules produce expected results.** Test auto-classification with entities that should match known rules.

```bash
# Create an entity that matches a rule
indemn email create --data '{"sender_domain": "usli.com", "subject": "Quote", "status": "received"}'

# Check the rule evaluation trace
indemn trace entity Email <id>
```

4. **Associates process messages correctly.** If associates are active, verify they claim messages and produce the expected entity changes.

5. **Cascade traces are coherent.** Follow a correlation_id through the full cascade:

```bash
indemn trace cascade <correlation_id>
```

### What to Look For

| Check | How to Verify |
|---|---|
| Watches fire for the right roles | `indemn queue stats` after entity creation/transition |
| Messages route to correct actors | `indemn queue list --role <role>` |
| Rules produce correct results | `indemn trace entity <Type> <id>` |
| State transitions are valid | Invalid transitions should error, not silently fail |
| Cascades terminate | No circuit-broken messages (depth > 10) |
| Permissions are enforced | Actor without write permission cannot modify entity |

---

## Step 8: Deploy and Tune

### Production Org

Create the production organization and populate it:

```bash
indemn org create --data '{"name": "GIC", "slug": "gic"}'
```

Then run your setup scripts (see Setup Script Pattern below) to configure entities, roles, rules, lookups, skills, and integrations.

### Monitor the needs_reasoning Rate

The single most important metric for a deployed system is the `needs_reasoning` rate -- how often the rule engine cannot produce a deterministic result and falls back to AI.

```bash
# Check rule evaluation results
indemn trace entity <Type> <id>
# Look at method_metadata.matched and method_metadata.vetoed
```

A high `needs_reasoning` rate means:
- The LLM is handling cases that could be rules
- You are paying for AI on patterns that repeat
- The system is slower than it needs to be

### The Tuning Loop

1. Find entities where `needs_reasoning` was true
2. Look at the LLM's decision -- is this a pattern?
3. If yes, write a rule for it
4. The next time that pattern appears, the rule catches it -- no LLM call

Over time, rules replace AI for every repeated pattern. Cost goes down. Speed goes up. Predictability increases. The LLM handles only genuinely novel situations.

### The --auto Progression

| Time | What Happens |
|---|---|
| **Week 1** | Few rules. LLM handles most cases. `needs_reasoning` rate is high. |
| **Week 2** | You study the LLM decisions. Write rules for the top 10 patterns. |
| **Week 4** | Rules handle 60-70% of volume. LLM handles the rest. |
| **Month 2** | Rules handle 85%+. LLM handles edge cases. Cost is a fraction of Week 1. |
| **Ongoing** | Every time a new pattern appears, the LLM handles it once. You write a rule. Done. |

---

## Setup Script Pattern

For repeatable deployments, organize setup as numbered scripts. The numbering reflects dependency order -- entities before roles (roles reference entity types), roles before actors (actors reference roles), and so on.

```
data/setup/
  01-bootstrap.sh    # Organization creation + first admin
  02-entities.sh     # Entity definitions (reference entities first)
  03-roles.sh        # Roles with permissions and watches
  04-actors.sh       # Human and associate actors
  05-rules.sh        # Rule groups, rules, lookups
  06-skills.sh       # Associate behavioral skills
  07-integrations.sh # External system connections
  08-seed.sh         # Reference data (lookup imports, catalog entries)
```

Each script is idempotent -- running it twice does not create duplicates. Use `--upsert` flags where available, or check for existence before creating.

Example `01-bootstrap.sh`:

```bash
#!/bin/bash
set -euo pipefail

ORG_NAME="${1:?Usage: 01-bootstrap.sh <org-name>}"

echo "==> Creating organization: $ORG_NAME"
indemn org create --data "{\"name\": \"$ORG_NAME\", \"slug\": \"$(echo $ORG_NAME | tr '[:upper:]' '[:lower:]' | tr ' ' '-')\"}"

echo "==> Creating admin actor"
indemn actor create --type human --name "Admin" --email admin@indemn.ai
# Role assignment happens in 03-roles.sh after roles exist
```

---

## Worked Example: Insurance Email Processing (GIC)

GIC Insurance receives 100+ emails a day from carriers, agents, and policyholders. JC and Maribel manually read, classify, extract data, create submissions, and draft responses. This example walks through all 8 steps.

### Step 1: Understand the Business

From sitting with JC and Maribel:

> Emails arrive in the shared inbox. JC opens each one and figures out what it is -- a quote from a carrier, a question from an agent, a policyholder claim, spam. For carrier responses, she matches it to the original submission, pulls numbers from the attached PDF, and updates the submission record. For complex quotes, she creates an assessment for the underwriter. Maribel handles agent inquiries and drafts responses. About 40% of their time is classification, 30% is data extraction, 20% is drafting responses, 10% is routing and admin.

**Pain points:** Classification takes time but is often obvious (carrier domains are known). Data extraction from PDFs is tedious and error-prone. Matching emails to submissions requires searching through multiple systems.

### Step 2: Identify Entities

Walking through the narrative, candidate nouns: Email, Submission, Carrier, Quote, Assessment, Draft, Agent, Policyholder, Attachment, PDF.

Applying the 7-test criteria:

| Candidate | Identity | Lifecycle | Independence | Not Kernel | CLI Test | Watchable | Multiplicity | Verdict |
|---|---|---|---|---|---|---|---|---|
| Email | Yes (message ID) | Yes (received -> classified -> processed) | Yes | Yes | `indemn email list` -- yes | Yes (new emails trigger work) | Many per day | **Entity** |
| Submission | Yes (submission #) | Yes (received -> triaging -> quoted -> bound) | Yes | Yes | `indemn submission list` -- yes | Yes (state changes matter) | Many per carrier | **Entity** |
| Carrier | Yes (company name) | No (carriers are what they are) | Yes | Yes | `indemn carrier list` -- yes | Not really | Many exist | **Entity** (reference) |
| Assessment | Yes (per submission) | Yes (draft -> under_review -> approved -> rejected) | Yes | Yes | `indemn assessment get` -- yes | Yes (completion triggers next step) | Many per submission | **Entity** |
| Draft | Yes (per email) | Yes (drafting -> review -> sent) | Yes | Yes | `indemn draft get` -- yes | Yes (ready for review) | Many per email | **Entity** |
| Agent | Yes (name/email) | No | Depends | Overlaps with Contact | Maybe | No | N/A | **Field on Submission** or Contact in CRM |
| Quote | Overlaps with Submission | Overlaps with Assessment | No -- it IS the carrier's response | Yes | Not distinct | Overlaps | N/A | **Part of Assessment** |
| Attachment | No unique identity beyond email | No lifecycle | No -- property of Email | Yes | No | No | N/A | **Field on Email** |

**Result:** 5 entities -- Email, Submission, Carrier (reference), Assessment, Draft.

**Entity definitions:**

**Email:**
```bash
indemn entity create --data '{
  "name": "Email",
  "collection_name": "emails",
  "fields": {
    "subject": {"type": "str", "required": true},
    "sender": {"type": "str", "required": true},
    "sender_domain": {"type": "str"},
    "recipients": {"type": "list"},
    "body": {"type": "str"},
    "received_at": {"type": "datetime"},
    "has_attachment": {"type": "bool", "default": false},
    "attachment_types": {"type": "list"},
    "classification": {"type": "str", "enum_values": [
      "carrier_response", "agent_inquiry", "policyholder_request",
      "internal", "renewal_notice", "endorsement", "cancellation",
      "claim", "spam", "unknown"
    ]},
    "classification_confidence": {"type": "decimal"},
    "submission_id": {"type": "objectid", "is_relationship": true, "relationship_target": "Submission"},
    "status": {"type": "str", "default": "received", "is_state_field": true},
    "external_ref": {"type": "str"},
    "notes": {"type": "str"}
  },
  "state_machine": {
    "received": ["classified"],
    "classified": ["linked", "processed", "escalated"],
    "linked": ["processed"],
    "processed": [],
    "escalated": ["classified"]
  }
}'
```

**Submission:**
```bash
indemn entity create --data '{
  "name": "Submission",
  "collection_name": "submissions",
  "fields": {
    "title": {"type": "str", "required": true},
    "carrier_id": {"type": "objectid", "is_relationship": true, "relationship_target": "Carrier"},
    "insured_name": {"type": "str"},
    "lob": {"type": "str", "enum_values": [
      "general_liability", "workers_comp", "commercial_auto",
      "business_owners", "commercial_package", "professional_liability",
      "cyber", "other"
    ]},
    "effective_date": {"type": "date"},
    "expiration_date": {"type": "date"},
    "premium": {"type": "decimal"},
    "status": {"type": "str", "default": "received", "is_state_field": true},
    "assigned_to": {"type": "str"},
    "notes": {"type": "str"}
  },
  "state_machine": {
    "received": ["triaging"],
    "triaging": ["quoted", "declined", "needs_info"],
    "needs_info": ["triaging"],
    "quoted": ["bound", "expired"],
    "bound": [],
    "declined": [],
    "expired": []
  }
}'
```

**Carrier** (reference entity):
```bash
indemn entity create --data '{
  "name": "Carrier",
  "collection_name": "carriers",
  "fields": {
    "name": {"type": "str", "required": true},
    "domain": {"type": "str"},
    "prefix_code": {"type": "str"},
    "lob_specialties": {"type": "list"},
    "contact_email": {"type": "str"},
    "portal_url": {"type": "str"},
    "notes": {"type": "str"}
  }
}'
```

**Assessment:**
```bash
indemn entity create --data '{
  "name": "Assessment",
  "collection_name": "assessments",
  "fields": {
    "submission_id": {"type": "objectid", "is_relationship": true, "relationship_target": "Submission"},
    "email_id": {"type": "objectid", "is_relationship": true, "relationship_target": "Email"},
    "premium_quoted": {"type": "decimal"},
    "deductible": {"type": "decimal"},
    "coverage_limits": {"type": "str"},
    "effective_date": {"type": "date"},
    "carrier_id": {"type": "objectid", "is_relationship": true, "relationship_target": "Carrier"},
    "needs_review": {"type": "bool", "default": false},
    "reviewer": {"type": "str"},
    "status": {"type": "str", "default": "draft", "is_state_field": true},
    "notes": {"type": "str"}
  },
  "state_machine": {
    "draft": ["under_review"],
    "under_review": ["approved", "rejected", "needs_revision"],
    "needs_revision": ["under_review"],
    "approved": [],
    "rejected": []
  }
}'
```

**Draft:**
```bash
indemn entity create --data '{
  "name": "Draft",
  "collection_name": "drafts",
  "fields": {
    "email_id": {"type": "objectid", "is_relationship": true, "relationship_target": "Email"},
    "subject": {"type": "str"},
    "body": {"type": "str"},
    "recipients": {"type": "list"},
    "draft_type": {"type": "str", "enum_values": ["reply", "forward", "new"]},
    "status": {"type": "str", "default": "drafting", "is_state_field": true},
    "reviewed_by": {"type": "str"},
    "notes": {"type": "str"}
  },
  "state_machine": {
    "drafting": ["review"],
    "review": ["approved", "revision_needed"],
    "revision_needed": ["review"],
    "approved": ["sent"],
    "sent": []
  }
}'
```

### Step 3: Roles and Actors

From the narrative, six distinct responsibilities:

```bash
# Admin -- full access, JC's primary role
indemn role create --data '{
  "name": "admin",
  "permissions": {
    "read": ["Email", "Submission", "Carrier", "Assessment", "Draft"],
    "write": ["Email", "Submission", "Carrier", "Assessment", "Draft"]
  },
  "watches": [
    {"entity_type": "Assessment", "event": "transitioned:approved"},
    {"entity_type": "Draft", "event": "transitioned:approved"},
    {"entity_type": "Email", "event": "transitioned:escalated"}
  ]
}'

# Classifier -- AI associate that classifies incoming emails
indemn role create --data '{
  "name": "classifier",
  "permissions": {
    "read": ["Email", "Submission", "Carrier"],
    "write": ["Email"]
  },
  "watches": [{
    "entity_type": "Email",
    "event": "created",
    "conditions": {"field": "status", "op": "equals", "value": "received"}
  }]
}'

# Linker -- AI associate that links emails to submissions
indemn role create --data '{
  "name": "linker",
  "permissions": {
    "read": ["Email", "Submission", "Carrier"],
    "write": ["Email"]
  },
  "watches": [{
    "entity_type": "Email",
    "event": "transitioned:classified",
    "conditions": {"field": "classification", "op": "equals", "value": "carrier_response"}
  }]
}'

# Assessor -- AI associate that extracts data from carrier responses
indemn role create --data '{
  "name": "assessor",
  "permissions": {
    "read": ["Email", "Submission", "Carrier", "Assessment"],
    "write": ["Assessment"]
  },
  "watches": [{
    "entity_type": "Email",
    "event": "transitioned:linked"
  }]
}'

# Draft Writer -- AI associate that drafts responses
indemn role create --data '{
  "name": "draft_writer",
  "permissions": {
    "read": ["Email", "Submission", "Carrier", "Draft"],
    "write": ["Draft"]
  },
  "watches": [{
    "entity_type": "Email",
    "event": "transitioned:classified",
    "conditions": {"field": "classification", "op": "in", "value": ["agent_inquiry", "policyholder_request"]}
  }]
}'

# Underwriter -- human reviewer for assessments
indemn role create --data '{
  "name": "underwriter",
  "permissions": {
    "read": ["Email", "Submission", "Carrier", "Assessment"],
    "write": ["Assessment", "Submission"]
  },
  "watches": [{
    "entity_type": "Assessment",
    "event": "created",
    "conditions": {"field": "needs_review", "op": "equals", "value": true}
  }]
}'
```

**Coverage check:** New email (received) -> classifier watch fires. Classified as carrier_response -> linker watch fires. Linked to submission -> assessor watch fires. Assessment with needs_review -> underwriter watch fires. Classified as agent_inquiry -> draft_writer watch fires. Escalated -> admin watch fires. Every transition has a watcher.

### Step 4: Rules and Configuration

```bash
# Carrier domain classification rules
indemn rule create \
  --entity Email --capability auto_classify \
  --name known-carrier-usli \
  --when '{"field": "sender_domain", "op": "equals", "value": "usli.com"}' \
  --action set_fields \
  --sets '{"classification": "carrier_response", "classification_confidence": 0.95}' \
  --priority 200

indemn rule create \
  --entity Email --capability auto_classify \
  --name known-carrier-markel \
  --when '{"field": "sender_domain", "op": "equals", "value": "markel.com"}' \
  --action set_fields \
  --sets '{"classification": "carrier_response", "classification_confidence": 0.95}' \
  --priority 200

# Veto: complaints from known carriers still need judgment
indemn rule create \
  --entity Email --capability auto_classify \
  --name carrier-complaint-veto \
  --when '{"all": [
    {"field": "sender_domain", "op": "in", "value": ["usli.com", "markel.com"]},
    {"any": [
      {"field": "subject", "op": "contains", "value": "complaint"},
      {"field": "subject", "op": "contains", "value": "dispute"},
      {"field": "subject", "op": "contains", "value": "legal"}
    ]}
  ]}' \
  --action force_reasoning \
  --forces-reasoning-reason "Complaints, disputes, and legal matters need human judgment" \
  --priority 300

# LOB prefix lookup for submissions
indemn lookup create --name usli-prefix-lob \
  --data '{"MGL": "general_liability", "WC": "workers_comp", "BOP": "business_owners", "CPP": "commercial_package", "PL": "professional_liability"}'
```

### Step 5: Skills

Write the email-classification skill as shown in the Step 5 section above. Write similar skills for linker, assessor, and draft_writer associates.

### Step 6: Integrations

```bash
# Outlook email adapter
indemn integration create --data '{
  "name": "GIC Outlook",
  "system_type": "email",
  "provider": "outlook",
  "owner_type": "org",
  "config": {
    "tenant_id": "gic-tenant-id",
    "client_id": "gic-client-id",
    "mailbox": "inbox@gicinsurance.com"
  }
}'

indemn integration set-credentials <integration_id> \
  --secret-ref indemn/prod/integrations/gic-outlook

indemn integration transition <integration_id> --to connected
indemn integration transition <integration_id> --to active
```

### Step 7: Staging Test

```bash
# Clone prod org as staging
indemn org clone gic --as gic-staging

# Create a test email that should match the USLI rule
indemn email create --data '{
  "subject": "RE: MGL-2026-001 Quote Response",
  "sender": "underwriting@usli.com",
  "sender_domain": "usli.com",
  "body": "Attached is the quote for the above referenced submission.",
  "has_attachment": true,
  "status": "received"
}'

# Verify: classifier watch should fire, message should appear
indemn queue stats
# Expected: 1 pending message for classifier role

# Verify: rule should auto-classify as carrier_response
# (After associate processes, or invoke manually)
indemn trace entity Email <email_id>
# Expected: method_metadata shows matched=true, winning_rule=known-carrier-usli

# Trace the full cascade
indemn trace cascade <correlation_id>
# Expected: Email:created -> classifier processes -> Email:transitioned:classified ->
#           linker processes -> Email:transitioned:linked -> assessor processes
```

### Step 8: Deploy and Tune

Week 1: Go live. Monitor. The classifier handles known carrier domains deterministically. Unknown senders go to LLM. Watch the `needs_reasoning` rate.

Week 2: Study which unknown senders the LLM classified. The top 10 are probably agent domains (allstate.com, statefarm.com). Write rules for each.

Week 4: Rules handle 70% of classifications. Add rules for renewal notice keywords, endorsement patterns, and common agent inquiry formats.

Ongoing: Every new pattern the LLM handles, you decide whether to write a rule for it. The system gets cheaper and faster with every rule added.

---

## Worked Example: Internal CRM (Indemn Customer System)

The same 8-step process, different domain -- zero insurance concepts. This proves the kernel is domain-agnostic.

### Step 1: Understand the Business

> Indemn is a startup selling AI associates to insurance companies. Kyle (CEO) manages the pipeline. George and Craig handle customer relationships. The team needs to know: who are our customers, what stage is each deal at, what have we deployed for them, and what signals indicate health or risk?

### Step 2: Identify Entities

14 entities across 5 groups:

| Entity | Type | Key Purpose |
|---|---|---|
| Company | Domain | Root entity -- any business Indemn has a relationship with |
| Contact | Domain | People at companies |
| Deal | Domain | Business opportunities with pipeline stages |
| Conference | Domain | Events Indemn attends, lead source tracking, ROI |
| Associate Deployment | Domain | What AI associates are deployed per customer |
| Outcome | Domain | Per-customer mapping to the Four Outcomes framework |
| Meeting | Domain | Customer meetings with intelligence extraction |
| Task | Domain | All work items from any source |
| Commitment | Domain | Promises tracked for accountability |
| Signal | Domain | Health, expansion, and risk indicators |
| Decision | Domain | Recorded decisions with rationale |
| Associate Type | Reference | Product catalog of available associates |
| Outcome Type | Reference | The Four Outcomes framework (Revenue, Efficiency, Retention, Control) |
| Stage | Reference | Deal pipeline stages with probability and staleness thresholds |

### Step 3: Roles

```bash
# Account owner -- sees their companies and deals
indemn role create --data '{
  "name": "account_owner",
  "permissions": {
    "read": ["Company", "Contact", "Deal", "Meeting", "Task", "Signal", "Commitment", "Decision"],
    "write": ["Company", "Contact", "Deal", "Task"]
  },
  "watches": [
    {"entity_type": "Deal", "event": "method:stale_check",
     "scope": {"type": "field_path", "path": "company.owner_id"}},
    {"entity_type": "Commitment", "event": "transitioned:missed",
     "scope": {"type": "field_path", "path": "company.owner_id"}},
    {"entity_type": "Signal", "event": "created",
     "conditions": {"field": "type", "op": "in", "value": ["Churn_Risk", "Escalation"]},
     "scope": {"type": "field_path", "path": "company.owner_id"}}
  ]
}'
```

### Step 4: Rules

```bash
# Auto-calculate deal probability from stage
indemn rule create \
  --entity Deal --capability auto_classify \
  --name stage-probability \
  --when '{"field": "stage", "op": "exists", "value": true}' \
  --action set_fields \
  --sets '{"probability": {"lookup": "stage-probability", "from_field": "stage"}}' \
  --priority 100

# Stage probability lookup
indemn lookup create --name stage-probability \
  --data '{"contact": 0.05, "discovery": 0.15, "demo": 0.25, "proposal": 0.40, "negotiation": 0.60, "verbal": 0.80, "signed": 1.00}'
```

### Steps 5-8: Same Process

Write associate skills for meeting intelligence extraction, stale deal detection, and commitment tracking. Set up integrations for Google Calendar and email. Test in staging. Deploy and tune.

The domain is completely different -- sales CRM instead of insurance email processing. The process is identical. The kernel does not care what the entities represent.

---

## Checklist

Use this checklist when modeling a new domain. Every item should have a clear answer before going to production.

### Step 1: Understanding
- [ ] Narrative written in business language, not technical language
- [ ] Workflows mapped with decision points and handoffs
- [ ] People identified with their responsibilities
- [ ] Pain points prioritized
- [ ] Current systems inventoried

### Step 2: Entities
- [ ] Every entity passed the 7-test criteria
- [ ] No entity duplicates a kernel mechanism
- [ ] Reference entities identified and separated
- [ ] State machines cover all lifecycle paths including terminal states
- [ ] Relationships between entities documented
- [ ] Field types chosen (enums over free text)

### Step 3: Roles and Actors
- [ ] Every entity state change has a role whose watch catches it
- [ ] Permissions are minimal per role (least privilege)
- [ ] Watch conditions are specific (not matching everything)
- [ ] Admin role exists for manual operations
- [ ] `indemn role list --show-watches` shows a coherent wiring diagram

### Step 4: Rules
- [ ] Common patterns have deterministic rules
- [ ] Edge cases that look simple but need judgment have `force_reasoning` veto rules
- [ ] Lookups created for any rule that would otherwise need 10+ copies
- [ ] Can trace through the happy path with rules only

### Step 5: Skills
- [ ] Every associate has a skill document
- [ ] Skills name specific CLI commands and enum values
- [ ] Skills handle edge cases explicitly
- [ ] A human reading the skill understands what the associate does

### Step 6: Integrations
- [ ] All external systems have Integration entities
- [ ] Credentials are in AWS Secrets Manager, not MongoDB
- [ ] Adapters exist for all providers
- [ ] Integrations are in `active` state

### Step 7: Testing
- [ ] Staging org created and configured
- [ ] Full lifecycle traced for the primary entity
- [ ] Cascade traces are coherent (no circuit-broken messages)
- [ ] Rules produce expected results for known patterns
- [ ] Permissions prevent unauthorized access

### Step 8: Production
- [ ] Production org created
- [ ] Setup scripts run successfully
- [ ] Monitoring in place for `needs_reasoning` rate
- [ ] Plan for adding rules based on LLM patterns
