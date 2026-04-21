# Getting Started with the Indemn OS

## Install the CLI

```bash
# One-liner (requires gh CLI authenticated)
bash install-cli.sh

# Or manually:
gh release download v0.1.0 --repo craig-indemn/indemn-os --pattern "*.whl"
pip install indemn_os-*.whl
```

## Connect

```bash
export INDEMN_API_URL=https://api.os.indemn.ai

indemn auth login --org _platform --email you@indemn.ai --password <your-password>
```

Ask Craig for your password. He'll set it up for you.

## Verify

```bash
indemn platform health
# → {"status":"healthy","checks":{"mongodb":"ok","temporal":"ok"}}
```

## Your First Commands

```bash
# See what's in the system
indemn company list
indemn deal list
indemn contact list

# Look at a specific deal
indemn deal get <id>

# See entity definitions
indemn entity list

# See auto-generated documentation for any entity
indemn skill get Deal
indemn skill get Company

# See the audit trail for any entity
indemn trace entity Deal <id>

# Update a deal
indemn deal update <id> --data '{"next_step": "Send proposal by Friday"}'

# Transition a deal's stage
indemn deal transition <id> --to proposal
```

## Use the UI

Go to [os.indemn.ai](https://os.indemn.ai) and log in with your Indemn credentials.

- Click any entity in the sidebar to see the list
- Click a row to open the detail panel
- Double-click to go to the full detail page
- Click any field to edit it inline
- Check the **Activity** tab to see everything that's happened

## Use with Claude Code

Clone the repo and open it in Claude Code. CLAUDE.md loads automatically and gives Claude full context.

```bash
git clone https://github.com/craig-indemn/indemn-os.git
cd indemn-os
# Open in Claude Code — CLAUDE.md loads automatically
```

Use the `/domain-modeling` skill for the 8-step process to build any system on the OS.

## The 8-Step Domain Modeling Process

To build any business domain on the OS:

1. **Understand the business** — what workflows, people, systems, pain points
2. **Define entities** — `indemn entity create` with fields, state machines, relationships
3. **Define roles + watches** — `indemn role create` with permissions and watches
4. **Define rules** — `indemn rule create` for deterministic business logic
5. **Write associate skills** — `indemn skill create` with behavioral instructions
6. **Set up integrations** — `indemn integration create` for external systems
7. **Test** — `indemn trace entity/cascade` to verify watches fire correctly
8. **Deploy and tune** — monitor, add rules for patterns the LLM keeps handling

## Services

| Service | URL |
|---------|-----|
| UI | [os.indemn.ai](https://os.indemn.ai) |
| API | [api.os.indemn.ai](https://api.os.indemn.ai) |
| Chat Runtime | wss://indemn-runtime-chat-production.up.railway.app/ws/chat |

## Help

```bash
indemn --help                    # All commands
indemn entity --help             # Entity management
indemn actor --help              # Actor/associate management
indemn trace --help              # Debugging
indemn skill get <EntityName>    # Read auto-generated entity documentation
```
