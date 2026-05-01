"""DeepagentsLLM — adapt a deepagents CompiledStateGraph to livekit.agents.llm.LLM.

LiveKit's AgentSession orchestrates audio I/O (STT -> LLM -> TTS). The "LLM"
slot is an interface that takes a ChatContext (the conversation so far in
LiveKit's format) and returns an LLMStream of ChatChunks (text deltas to
speak via TTS).

The deepagents agent already does ALL of the reasoning + tool-calling
internally (it loads skills via `indemn skill get`, plans with todos,
executes CLI commands). We don't want LiveKit to know about any of that.

So this adapter exposes the deepagents agent as an LLM that:
1. Takes the LiveKit ChatContext (which is the conversation history)
2. Translates it to the deepagents/LangChain message format
3. Invokes the agent (which may run many internal turns: load skill,
   write_todos, execute CLI, summarize) and waits for the final output
4. Extracts the final assistant text response from the agent state
5. Streams it back to LiveKit as ChatChunks for TTS to speak

Tools passed in by the AgentSession at the LiveKit layer are IGNORED —
deepagents has its own tool surface (execute, write_todos, read_file,
etc.). The agent's tools are bound at agent build time, not per-call.
This is intentional: voice is a thin transport over the same agent
that runs in chat + async.

Streaming behavior (v1): the adapter waits for the agent to fully
complete, then emits the final assistant text in chunks (split on
sentence boundaries for snappier TTS). Token-level streaming via
agent.astream_events is a future enhancement once we validate the
basic shape works end-to-end.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from livekit.agents import APIConnectOptions
from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    LLMStream,
)
from livekit.agents.llm.tool_context import (
    FunctionTool,
    ProviderTool,
    RawFunctionTool,
    ToolChoice,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr

log = logging.getLogger(__name__)


def _livekit_chat_ctx_to_langchain(chat_ctx: ChatContext) -> list:
    """Translate a LiveKit ChatContext into LangChain BaseMessage list.

    LiveKit's ChatContext.items is an ordered list of ChatMessage objects
    with `role` (system/user/assistant) and `content` (str or list of
    text/image parts). For voice, content is text — STT emits text and
    TTS speaks text. Image/audio parts are not relevant.

    deepagents expects the conversation as a list of LangChain BaseMessage
    instances passed in via {"messages": [...]} on agent.ainvoke().
    """
    messages: list = []
    for item in chat_ctx.items:
        # LiveKit's items can be ChatMessage or other types (function calls etc).
        # We only care about text-bearing messages here.
        role = getattr(item, "role", None)
        content = getattr(item, "content", None)
        if not role or content is None:
            continue
        # Content can be a str or a list of content parts.
        if isinstance(content, list):
            text = " ".join(
                str(getattr(part, "text", part)) for part in content if part is not None
            )
        else:
            text = str(content)
        if not text.strip():
            continue
        if role == "user":
            messages.append(HumanMessage(content=text))
        elif role == "assistant":
            messages.append(AIMessage(content=text))
        elif role == "system":
            messages.append(SystemMessage(content=text))
        # Other roles (function, tool) — skip; deepagents owns its own tool history
    return messages


def _extract_final_assistant_text(agent_result: dict) -> str:
    """Pull the agent's final assistant message out of a deepagents result.

    deepagents `agent.ainvoke()` returns a state dict containing
    "messages": [BaseMessage, ...]. The last AIMessage is the assistant's
    final spoken response. Tool calls and intermediate AIMessages with
    only tool_calls (no text content) are skipped — those are the
    agent's internal reasoning, not what we speak to the user.
    """
    messages = agent_result.get("messages", [])
    # Walk backward — find the last AIMessage with non-empty text content.
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, list):
                # Multi-part AIMessage — concatenate text parts only.
                text_parts = [
                    str(part.get("text", "")) if isinstance(part, dict) else str(part)
                    for part in content
                ]
                text = " ".join(p for p in text_parts if p).strip()
            else:
                text = (content or "").strip() if isinstance(content, str) else ""
            if text:
                return text
    return ""


class DeepagentsLLMStream(LLMStream):
    """LLMStream that runs the deepagents agent and emits its final text."""

    async def _run(self) -> None:  # type: ignore[override]
        """Invoke the deepagents agent; emit its final assistant text as chunks."""
        agent = self._llm._agent  # type: ignore[attr-defined]
        thread_id = self._llm._thread_id  # type: ignore[attr-defined]

        messages = _livekit_chat_ctx_to_langchain(self._chat_ctx)
        if not messages:
            log.warning("DeepagentsLLMStream._run: no messages in chat_ctx; skipping")
            return

        log.info(
            "DeepagentsLLM invoke: %d messages, last=%s",
            len(messages),
            type(messages[-1]).__name__,
        )
        config = {"configurable": {"thread_id": thread_id}} if thread_id else None
        try:
            result = await agent.ainvoke({"messages": messages}, config=config)
        except Exception as e:
            log.exception("DeepagentsLLM agent.ainvoke failed: %s", e)
            raise

        text = _extract_final_assistant_text(result)
        if not text:
            log.warning(
                "DeepagentsLLM: agent returned no final assistant text "
                "(result keys: %s)",
                list(result.keys()) if isinstance(result, dict) else "?",
            )
            return

        log.info("DeepagentsLLM final assistant text: %d chars", len(text))

        # Emit the text as a single ChatChunk. Sentence-boundary chunking
        # would let TTS start sooner; for v1 we keep it simple — TTS will
        # see one chunk and start synthesizing immediately.
        chunk = ChatChunk(
            id=f"deepagents-{id(result)}",
            delta=ChoiceDelta(role="assistant", content=text),
        )
        self._event_ch.send_nowait(chunk)


class DeepagentsLLM(LLM):
    """LiveKit-compatible LLM that runs a deepagents agent under the hood.

    Constructed once per VoiceSession with a built deepagents agent and a
    thread_id (the Interaction.id) for LangGraph checkpointing. AgentSession
    plugs this into the STT -> LLM -> TTS pipeline; on each user turn, the
    AgentSession calls `chat()` and waits for the LLMStream to emit chunks.
    """

    def __init__(self, agent, thread_id: str | None = None) -> None:
        super().__init__()
        self._agent = agent
        self._thread_id = thread_id

    @property
    def model(self) -> str:
        return "deepagents"

    @property
    def provider(self) -> str:
        return "indemn-deepagents"

    def chat(  # type: ignore[override]
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[FunctionTool | RawFunctionTool | ProviderTool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> LLMStream:
        # `tools` from LiveKit are intentionally IGNORED — deepagents owns
        # its own tool surface (execute, write_todos, read_file, etc.) bound
        # at agent build time.
        return DeepagentsLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )
