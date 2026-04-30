"""Voice agent — Assistant class wrapping the LiveKit Agents framework.

The Assistant is the LLM-backed agent that handles a single voice conversation.
It owns the system prompt, the tool surface (one tool: `execute`), and any
per-turn behavior (e.g., loading the operating skill before acting).

Architecture:
- Assistant is constructed once per LiveKit room (one per call).
- The LLM is initialized once at startup (via livekit.plugins.google for Gemini).
- Tools are bound at construction. The `execute` tool runs `indemn` CLI commands.
- System prompt directs the agent to (1) load operating skill + entity skills via
  `execute('indemn skill get <name>')` on turn 1, (2) plan with todo, (3) execute
  per the skill instructions. Symmetric with async-deepagents DEFAULT_PROMPT.

For the operating skill currently wired (`log-touchpoint`):
- The agent's job is to help the user log a manually-recorded interaction.
- It collects: customer Company, participants (Contacts/Employees), date, scope,
  free-text summary. Resolves via entity_resolve. Creates Touchpoint via the CLI.
- The skill itself (uploaded to dev OS) tells the agent the procedure verbatim.
"""

import logging
import os
from typing import Optional

from livekit.agents import Agent, ChatContext

from tools import execute

log = logging.getLogger(__name__)


# This system prompt directs the agent how to operate for this voice session.
# It mirrors async-deepagents DEFAULT_PROMPT (commit `7281b83` on indemn-os main):
# load skill via CLI on turn 1, then act per the skill content.
SYSTEM_PROMPT = (
    "You are the Indemn OS voice assistant. You're talking to a team member who wants "
    "to interact with the Indemn OS by voice. You help them log touchpoints (manually-"
    "recorded calls, in-person meetings, push-to-talk updates) and look up customer "
    "information.\n\n"
    "## How to operate\n\n"
    "On EVERY conversation, your first action is:\n\n"
    "  execute('indemn skill get log-touchpoint')\n\n"
    "That returns the full procedure for logging a touchpoint — read it carefully and "
    "follow it verbatim. The skill tells you what to collect, how to resolve "
    "participants and Company via entity-resolve, and the exact CLI shape for creating "
    "the Touchpoint.\n\n"
    "If the user asks for something other than logging a touchpoint (e.g. 'what's up "
    "with Alliance', 'how many emails from Cam this week'), use `indemn` CLI commands "
    "to look up the answer. Common patterns:\n"
    "  - `indemn company list --search '<name>'` — find a Company\n"
    "  - `indemn touchpoint list --limit 5` — recent Touchpoints\n"
    "  - `indemn skill get <EntityName>` — entity field schema + CLI examples\n\n"
    "## Voice-specific guidance\n\n"
    "- Be CONCISE. The user is listening, not reading. 1-2 sentences per turn unless "
    "they ask for detail.\n"
    "- ASK clarifying questions one at a time, not three at once. The user's answers "
    "are short — match their energy.\n"
    "- After running a CLI command, summarize what happened — don't recite raw JSON.\n"
    "- For entity-resolve ambiguity: ask 'Did you mean X or Y?' Don't guess.\n"
    "- For CRUD operations: confirm before creating. 'Logging a touchpoint with "
    "Walker at GR Little, scope external, dated today, summary <repeat-back>. Sound "
    "right?'\n\n"
    "## Hard rules\n\n"
    "- NEVER fabricate Contact/Company data. Resolve via `entity-resolve` first.\n"
    "- NEVER create entities without explicit user confirmation.\n"
    "- NEVER read raw JSON to the user — summarize.\n"
    "- If something fails, tell the user what failed and ask how to proceed. Don't "
    "retry silently.\n"
)


class IndemnVoiceAssistant(Agent):
    """The LiveKit voice agent for Indemn OS internal team use.

    Wraps a Gemini LLM (configured at session level — see agent.py / main.py)
    with a single `execute` tool that subprocess-shells `indemn` CLI commands.
    """

    def __init__(self, *, instructions: Optional[str] = None) -> None:
        super().__init__(
            instructions=instructions or SYSTEM_PROMPT,
            tools=[execute],
        )
        log.info("IndemnVoiceAssistant constructed with %d tool(s)", 1)
