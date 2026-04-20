"""indemn init — scaffold a Claude Code environment for OS development.

Creates .claude/ directory with CLAUDE.md and domain-modeling skill so any
Claude Code session in this project knows how to build on the Indemn OS.
"""

from pathlib import Path

import typer

init_app = typer.Typer(name="init", help="Initialize project for OS development")

# ---------------------------------------------------------------------------
# Template content — embedded so it ships with the CLI package
# ---------------------------------------------------------------------------

CLAUDE_MD = """# Building on the Indemn OS

This project builds on the Indemn Operating System.
The OS auto-generates API, CLI, UI, and documentation from entity definitions.
AI agents are a channel into the platform — they use the CLI like any other client.

## Quick Start

```bash
# Authenticate
indemn auth login --org <org_slug> --email <email>

# See what exists
indemn entity list          # Entity definitions
indemn skill list            # Skills (auto-generated docs)
indemn actor list            # Team members + associates
indemn role list             # Roles with permissions + watches

# Create an entity type
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

# Everything auto-generates: API, CLI, skill, UI
indemn submission list
indemn submission create --data '{"title": "Test"}'
indemn submission transition <id> --to triaging
```

## Field Types

`str`, `int`, `float`, `decimal`, `bool`, `datetime`, `date`,
`objectid`, `list`, `dict`

**Field options:** `required`, `default`, `unique`, `indexed`,
`enum_values`, `is_state_field`, `is_relationship`,
`relationship_target`

**Relationships:** Use `objectid` with `is_relationship: true`
and `relationship_target: "EntityName"`.

## Entity Design — What to Model vs What the Kernel Handles

The kernel provides mechanisms you do NOT rebuild as domain entities:

| Don't model this | The kernel provides |
|------------------|---------------------|
| Activity log | **Changes collection** — every mutation recorded with field-level detail |
| Notifications | **Watches on roles** — entity changes produce messages automatically |
| Team member identity | **Actor** — kernel entity for all participants |
| Account ownership | **Role** assignments with watches |
| Audit trail | **Changes collection** — tamper-evident, append-only |
| Version history | **Changes collection** — every mutation recorded |
| Communication | **Watches + Messages** — the wiring IS the communication system |

Your domain model covers BUSINESS DATA. The kernel covers CONNECTIVE TISSUE.

## Entity Criteria (7 tests)

Apply these to determine what should be its own entity:

1. **Identity** — Does it have a unique identity that matters? Would you refer to it by name or ID?
2. **Lifecycle** — Does it have meaningful states that change over time?
3. **Independence** — Can it exist on its own, not purely as a property of another entity?
4. **Not kernel mechanism** — Is this business data,
   not something the kernel already provides?
5. **CLI test** — Would someone want to `indemn <thing> list/create/get`?
6. **Watchable** — Would changes to this thing need to flow to people via watches?
7. **Multiplicity** — Can there be many of these per parent?

If it passes all 7: make it an entity. If it fails the CLI
test or multiplicity test: it's probably a field on another entity.

## Watches (the wiring mechanism)

Watches live on Roles. When an entity change matches a watch,
a message is created for actors in that role.

```bash
indemn role create --data '{
  "name": "account_lead",
  "permissions": {"read": ["Company", "Deal", "Contact"], "write": ["Company", "Deal"]},
  "watches": [{
    "entity_type": "Deal",
    "event": "transitioned",
    "conditions": {"field": "stage", "op": "equals", "value": "verbal"}
  }]
}'
```

**Events:** created, transitioned, method_invoked, fields_changed, deleted
**Operators:** equals, not_equals, contains, gt, gte, lt, lte,
in, not_in, matches, exists, older_than, within
**Composition:** `{"all": [...]}`, `{"any": [...]}`, `{"not": {...}}`

## Rules (deterministic automation)

Two actions only: `set_fields` (deterministic) and `force_reasoning` (send to LLM).

```bash
indemn rule create --data '{
  "entity_type": "Deal",
  "capability": "auto_classify",
  "name": "high-value-deals",
  "conditions": {"field": "expected_arr", "op": "gte", "value": 100000},
  "action": "set_fields",
  "sets": {"tier": "Enterprise"},
  "priority": 200
}'
```

## Associates (AI agents)

```bash
indemn skill create --name deal-monitor --content-from-file skills/deal-monitor.md
indemn actor create --type associate --name "Deal Monitor" \\
  --mode hybrid --runtime-id <id> --role account_lead --skills deal-monitor
```

## Debugging

```bash
indemn trace entity <Type> <id>        # Unified timeline
indemn trace cascade <correlation_id>  # Full execution tree
indemn queue stats                     # Pending per role
indemn integration health              # Adapter connectivity
indemn platform health                 # System status
```

## The Save Path

ALL entity saves go through `save_tracked()` — one MongoDB transaction:
entity write + changes record + watch evaluation + message creation.

Only creation + state transitions + @exposed methods generate messages. NOT every field change.
"""

DOMAIN_MODELING_SKILL = """---
name: domain-modeling
description: >-
  Build a domain on the Indemn OS — entity design, roles,
  watches, rules, skills, integrations. Use when creating a
  new business domain, defining entities, or onboarding.
---

# Domain Modeling on the Indemn OS

## The Process

### 1. Understand the Business
Talk to the people who do the work. Understand the narrative,
workflows, pain points, and what systems they use today.
Capture the objectives — what the system needs to DO should
inform what entities you need.

### 2. Identify Entities
Apply the 7-test criteria to every candidate:

| Test | Question | If No |
|------|----------|-------|
| Identity | Does it have a name/ID that matters? | It's a field |
| Lifecycle | Does it have meaningful state changes? | Consider making it a field with enum |
| Independence | Can it exist on its own? | It's a child field or embedded doc |
| Not kernel | Is it business data, not connective tissue? | The kernel handles it |
| CLI test | Would someone `indemn <thing> list`? | It's a field |
| Watchable | Should changes notify people? | Maybe a field is fine |
| Multiplicity | Can there be many per parent? | Might be a section of the parent |

**Design principles:**
- Entities are cheap — the OS auto-generates everything from definitions
- AI populates everything — design for extraction, not manual entry
- Enums over free text — AI classifies more reliably into defined categories
- If it passes the criteria, make it an entity — don't cram it into fields

### 3. Identify Roles and Actors
Who participates? What do they need to see? What do they need to be notified about?

- Start with one role (full access). Differentiate later.
- Associates (AI agents) get their own roles with specific watches.
- Watches answer: "what does this role care about?"

### 4. Define Rules and Configuration
Per-org business logic that determines behavior without code:

- Rules: condition → `set_fields` or `force_reasoning`
- Lookups: mapping tables, bulk-importable from CSV
- Capability activation: `indemn entity enable <Type> auto_classify --config '{...}'`

### 5. Write Skills
Markdown behavioral instructions for associates:

- Entity skills auto-generate (fields, lifecycle, commands)
- Associate skills: hand-written, describe HOW the associate should behave
- Skills reference entities by name — the CLI commands are in the auto-generated entity skill

### 6. Set Up Integrations
External system connections:

```bash
indemn integration create --name "Outlook" --system-type email --provider outlook --owner-type org
indemn integration set-credentials <id> --secret-ref indemn/prod/integrations/outlook
```

Credentials NEVER in MongoDB — always AWS Secrets Manager.

### 7. Test in Staging
Create a staging org (`indemn org clone`). Load realistic data. Validate end-to-end:
- Entity creation → watches fire → messages routed
- State transitions trigger the right downstream actions
- Associates process messages correctly
- Rules produce expected results

### 8. Deploy and Tune
Production org. Monitor. Add rules for patterns the LLM keeps handling deterministically.

The `--auto` pattern: try rules first, LLM fallback if no
match. Over time, rules replace LLM for repeated patterns —
cost goes down, speed goes up.

## Setup Script Pattern

Number your scripts so they run in dependency order:

```
data/setup/
  01-bootstrap.sh    # Org + first admin (skip if org exists)
  02-actors.sh       # Team members
  03-roles.sh        # Roles with permissions + watches
  04-entities.sh     # Entity definitions (reference entities first)
  05-seed.sh         # Reference data + bulk import
```

## Field Type Reference

| Design Type | OS Type | Example |
|-------------|---------|---------|
| string/text | `str` | `{"type": "str"}` |
| integer | `int` | `{"type": "int"}` |
| decimal/money | `decimal` | `{"type": "decimal"}` |
| boolean | `bool` | `{"type": "bool"}` |
| date | `date` | `{"type": "date"}` |
| datetime | `datetime` | `{"type": "datetime"}` |
| reference to entity | `objectid` | `{"type": "objectid", "is_relationship": true, ...}` |
| reference to actor | `objectid` | `{"type": "objectid", "is_relationship": true, ...}` |
| list of strings | `list` | `{"type": "list"}` |
| enum | `str` | `{"type": "str", "enum_values": ["A", "B", "C"]}` |
| free-form JSON | `dict` | `{"type": "dict"}` |

## State Machine Definition

```json
{
  "field_name": {"type": "str", "default": "initial_state", "is_state_field": true},
  ...
}
// state_machine on EntityDefinition:
{
  "initial_state": ["next_state_1", "next_state_2"],
  "next_state_1": ["final_state"],
  ...
}
```
"""


@init_app.callback(invoke_without_command=True)
def init_project(
    path: str = typer.Argument(".", help="Project directory (default: current)"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
):
    """Initialize a project for building on the Indemn OS.

    Creates .claude/ directory with CLAUDE.md and domain-modeling skill
    so Claude Code sessions know how to build on the OS.
    """
    project_dir = Path(path).resolve()

    if not project_dir.exists():
        typer.echo(f"Directory does not exist: {project_dir}", err=True)
        raise typer.Exit(1)

    claude_dir = project_dir / ".claude"
    claude_md = claude_dir / "CLAUDE.md"
    skill_dir = claude_dir / "skills" / "domain-modeling"
    skill_file = skill_dir / "SKILL.md"

    # Check for existing files
    if claude_md.exists() and not force:
        typer.echo(f"Already initialized: {claude_md}")
        typer.echo("Use --force to overwrite")
        raise typer.Exit(1)

    # Create directories
    claude_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Write files
    claude_md.write_text(CLAUDE_MD.lstrip())
    skill_file.write_text(DOMAIN_MODELING_SKILL.lstrip())

    typer.echo(f"Initialized Indemn OS project at {project_dir}")
    typer.echo(f"  {claude_md.relative_to(project_dir)}")
    typer.echo(f"  {skill_file.relative_to(project_dir)}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo("  indemn auth login --org <org> --email <email>")
    typer.echo("  # Start a Claude Code session — it will load the OS context automatically")
