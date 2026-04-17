# Getting Started with the Indemn OS

## Install the CLI

**From the repo (recommended for developers):**
```bash
git clone https://github.com/craig-indemn/indemn-os.git
cd indemn-os
uv sync
uv run indemn --help
```

**As a standalone package:**
```bash
pip install "indemn-os @ git+https://github.com/craig-indemn/indemn-os.git#subdirectory=indemn_os"
indemn --help
```

## Connect to the OS

```bash
# Set the API URL
export INDEMN_API_URL=https://indemn-api-production.up.railway.app

# Authenticate (get a token from your admin)
export INDEMN_SERVICE_TOKEN=indemn_xxx

# Or use the setup script:
source scripts/setup-cli.sh dev
```

## Verify Connection

```bash
indemn platform health
# → {"status":"healthy","checks":{"mongodb":"ok","temporal":"ok"}}
```

## Your First Commands

```bash
# See what's in the system
indemn queue stats
indemn actor list --type associate --status active
indemn runtime list

# Create a test entity
indemn entity create --data '{
  "name": "Task",
  "collection_name": "tasks",
  "fields": {
    "title": {"type": "str", "required": true},
    "status": {"type": "str", "default": "open", "is_state_field": true}
  },
  "state_machine": {"open": ["closed"], "closed": []}
}'

# Use the auto-generated commands
indemn task list
indemn task create --data '{"title": "My first task"}'
indemn task get <id>
indemn task transition <id> --to closed

# See what happened
indemn trace entity Task <id>

# Read the auto-generated skill (documentation)
indemn skill get Task
```

## Using from Claude Code

When working on the OS from Claude Code:

1. The `indemn` CLI is available via `uv run indemn` from the repo
2. Set `INDEMN_API_URL` and `INDEMN_SERVICE_TOKEN` in your session
3. Use the `/domain-modeling` skill for the 8-step process
4. Read `CLAUDE.md` at repo root for conventions

## The 8-Step Process

To build a business domain on the OS, follow the domain modeling process:

1. Understand the business
2. Define entities (`indemn entity create`)
3. Define roles + watches (`indemn role create`)
4. Define rules (`indemn rule create`)
5. Write associate skills (`indemn skill create`)
6. Set up integrations (`indemn integration create`)
7. Test in staging (`indemn trace entity/cascade`)
8. Deploy and tune

See `/domain-modeling` skill for full details with examples.

## Architecture

```
User/Agent
  ↓ (CLI or API)
Kernel API Server
  ↓
Entity Framework (auto-generated CRUD, state machines, watches)
  ↓
Message Queue (watches fire → messages → associates process)
  ↓
Harnesses (async/chat/voice — run agents outside kernel)
  ↓
CLI subprocess (agents use same CLI as humans)
```

## Services

| Service | What | URL |
|---|---|---|
| API | The gateway — all CLI/UI/harness calls | https://indemn-api-production.up.railway.app |
| UI | Base UI — entity views, queue, assistant | https://indemn-ui-production.up.railway.app |
| Chat Harness | WebSocket server for real-time conversations | wss://indemn-runtime-chat-production.up.railway.app/ws/chat |

## Help

```bash
indemn --help                    # All commands
indemn entity --help             # Entity management
indemn actor --help              # Actor/associate management
indemn trace --help              # Debugging
indemn skill get <EntityName>    # Read entity documentation
```
