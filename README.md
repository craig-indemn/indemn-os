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

## Repository Structure

```
kernel/          # The platform — entity framework, watches, rules, auth, API
kernel_entities/ # 7 kernel entities (Organization, Actor, Role, etc.)
indemn_os/       # CLI package — the universal interface
ui/              # Base UI — auto-generated from entity definitions
harnesses/       # Associate runtimes (async, chat, voice)
seed/            # Standard entity templates and reference data
tests/           # Unit, integration, end-to-end
docs/            # Getting started, architecture
```
