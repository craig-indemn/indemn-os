# Indemn OS

The operating system for insurance. Define an entity and it auto-generates its API, CLI, documentation, permissions, and UI.

**Live now:** [os.indemn.ai](https://os.indemn.ai)

## For the Team

### Use the UI

Go to [os.indemn.ai](https://os.indemn.ai) and log in with your Indemn email. Ask Craig for credentials.

### Use the CLI

```bash
# Install (requires GitHub access)
bash install-cli.sh

# Connect
export INDEMN_API_URL=https://api.os.indemn.ai
indemn auth login --org _platform --email you@indemn.ai --password <your-password>

# See your deals
indemn deal list
indemn company list --stage customer

# See what's happened
indemn trace entity Deal <id>
```

See [docs/getting-started.md](docs/getting-started.md) for the full guide.

### Use with Claude Code

Clone this repo and open it in Claude Code. The `CLAUDE.md` and `.claude/skills/` give Claude full context on the OS — how to define entities, set up watches, create associates, and model any business domain.

```bash
git clone https://github.com/craig-indemn/indemn-os.git
cd indemn-os
# Claude Code reads CLAUDE.md automatically
```

The `/domain-modeling` skill walks through the 8-step process for building any system on the OS.

## What's Running

| Service | URL |
|---------|-----|
| UI | [os.indemn.ai](https://os.indemn.ai) |
| API | [api.os.indemn.ai](https://api.os.indemn.ai) |

## Architecture

Six primitives: **Entity, Message, Actor, Role, Organization, Integration.**

```
User / AI Agent
  ↓ (CLI or API)
API Server
  ↓
Entity Framework (auto-generated CRUD, state machines, watches)
  ↓
Message Queue (entity changes → watches fire → messages route to actors)
  ↓
Associates (AI agents using the same CLI as humans)
```

Define entities → the system generates everything. Watches wire entity changes to the right people. Associates process work from the same queue as humans. The system churns.

Read the [white paper](docs/white-paper.md) for the full vision. Read `CLAUDE.md` for the builder's manual.

## Documentation

| Start Here | What It Covers |
|------------|---------------|
| [White Paper](docs/white-paper.md) | Vision, architecture, domain modeling, build sequence |
| [Getting Started](docs/getting-started.md) | CLI installation, first commands, using the UI |
| [CLAUDE.md](CLAUDE.md) | Builder's manual — compact reference for AI sessions |

### Architecture (deep technical docs)

| Document | What It Covers |
|----------|---------------|
| [System Overview](docs/architecture/overview.md) | Trust boundary, dispatch pattern, deployment topology, dependencies |
| [Entity Framework](docs/architecture/entity-framework.md) | Self-evidence, save_tracked(), state machines, computed fields, schema migration |
| [Watches & Wiring](docs/architecture/watches-and-wiring.md) | Watch conditions, scoping, unified queue, message cascade, selective emission |
| [Rules & --auto](docs/architecture/rules-and-auto.md) | Rule engine, lookups, capabilities, the --auto pattern, needs_reasoning metric |
| [Associates](docs/architecture/associates.md) | Actor model, skills, harness pattern, execution lifecycle, gradual rollout |
| [Integrations](docs/architecture/integrations.md) | Adapters, credential resolution, webhooks, content visibility |
| [Authentication](docs/architecture/authentication.md) | JWT + sessions, five auth methods, MFA, platform admin, recovery |
| [Real-Time](docs/architecture/realtime.md) | Attention, Runtime, scoped watches, handoff, voice/chat harnesses |
| [Observability](docs/architecture/observability.md) | Changes collection, message log, OTEL tracing, debugging commands |
| [Infrastructure](docs/architecture/infrastructure.md) | Railway services, local dev, deployment strategies, cost model |
| [Security](docs/architecture/security.md) | Org isolation, credential management, skill integrity, audit trail |

### Guides (step-by-step how-to)

| Guide | What You'll Do |
|-------|---------------|
| [Domain Modeling](docs/guides/domain-modeling.md) | The 8-step process with worked examples (GIC + CRM) |
| [Adding Entities](docs/guides/adding-entities.md) | Define an entity type and see it working end-to-end |
| [Adding Watches](docs/guides/adding-watches.md) | Configure watches that route changes to the right roles |
| [Adding Associates](docs/guides/adding-associates.md) | Create an AI associate that processes work from the queue |
| [Adding Integrations](docs/guides/adding-integrations.md) | Connect an external system, build a new adapter |
| [Development](docs/guides/development.md) | Local setup, testing, deploying, code conventions |

## Repository Structure

```
kernel/          # The platform — entity framework, watches, rules, auth, API
kernel_entities/ # 7 kernel entities (Organization, Actor, Role, etc.)
indemn_os/       # CLI package — the universal interface
ui/              # Base UI — auto-generated from entity definitions
harnesses/       # Associate runtimes (async, chat, voice)
seed/            # Standard entity templates and reference data
tests/           # Unit, integration, end-to-end
docs/            # White paper, architecture docs, developer guides
```
