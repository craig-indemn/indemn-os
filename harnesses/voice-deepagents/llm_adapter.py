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

import json

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


def _drain_event_queue(event_queue: list | None) -> str | None:
    """Drain pending entity-change events from the events-stream queue.

    Returns a one-line system message summarizing the events (suitable for
    prepending to the agent's input messages), or None if the queue is empty.

    Mirrors the chat-deepagents pattern in `ChatSession.handle_message`:
    on each user turn, drain queued events and inject them as a system
    message so the agent has mid-conversation awareness of state changes
    (a supervisor updated the Interaction, a related Touchpoint arrived,
    a related Email got classified, etc.).

    Mutates the queue: pops everything currently in it. Safe across
    concurrent appends from the events-stream subprocess thread because
    Python list pop/append at the head/tail is atomic under the GIL.
    """
    if not event_queue:
        return None
    events = list(event_queue)
    event_queue.clear()
    summaries = []
    for ev in events:
        entity_type = ev.get("entity_type", "unknown")
        entity_id = ev.get("entity_id", "unknown")
        event_type = ev.get("event_type", "change")
        summaries.append(f"{entity_type}/{entity_id}: {event_type}")
    return "[System events since last turn: " + "; ".join(summaries) + "]"


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
        event_queue = self._llm._event_queue  # type: ignore[attr-defined]
        associate = self._llm._associate or {}  # type: ignore[attr-defined]
        runtime_id = self._llm._runtime_id  # type: ignore[attr-defined]

        messages = _livekit_chat_ctx_to_langchain(self._chat_ctx)
        if not messages:
            log.warning("DeepagentsLLMStream._run: no messages in chat_ctx; skipping")
            return

        # AI-407 Phase 4: prepend initial <skill> + <deployment_context>
        # SystemMessages on FIRST turn (set by VoiceSession.start() in Task 2.17;
        # consumed + cleared here). Persists in checkpointer state via
        # add_messages reducer.
        initial_msgs = getattr(self._llm, "_initial_systemmessages", None)
        if initial_msgs:
            messages = list(initial_msgs) + messages
            self._llm._initial_systemmessages = None
            log.info(
                "DeepagentsLLM: prepended %d initial SystemMessages",
                len(initial_msgs),
            )

        # Mid-conversation awareness: drain entity-change events that arrived
        # while the agent was idle and prepend them as a SystemMessage so the
        # agent knows the world has moved while the user was talking.
        # Same pattern as chat-deepagents/session.py::handle_message.
        events_msg = _drain_event_queue(event_queue)
        if events_msg:
            messages = [SystemMessage(content=events_msg)] + messages
            log.info("DeepagentsLLM: prepended event summary (%d chars)", len(events_msg))

        log.info(
            "DeepagentsLLM invoke: %d messages, last=%s",
            len(messages),
            type(messages[-1]).__name__,
        )

        # AI-407 §13.5 voice: build RunnableConfig via VoiceSession.build_runnable_config
        # — configurable.thread_id = interaction_id (state across turns);
        # metadata.thread_id = correlation_id (LangSmith UI grouping); full
        # §13 metadata block + tags + run_name. Replaces the inline config
        # build that was missing metadata.thread_id = correlation_id (§13.5
        # called out this gap explicitly).
        correlation_id = getattr(self._llm, "_correlation_id", None)
        deployment_id = getattr(self._llm, "_deployment_id", None)
        if thread_id and correlation_id:
            # Local import to avoid circular: session.py imports llm_adapter
            from harness.session import VoiceSession

            config = VoiceSession.build_runnable_config(
                interaction_id=thread_id,
                correlation_id=correlation_id,
                associate=associate,
                runtime_id=str(runtime_id) if runtime_id else "",
                deployment_id=deployment_id,
            )
        else:
            # Local-dev fallback (no correlation_id / interaction_id wired in) —
            # preserve minimal LangSmith trace metadata
            associate_name = associate.get("name") or "Voice Agent"
            config = {"metadata": {"associate_name": associate_name}}
            if thread_id:
                config["configurable"] = {"thread_id": thread_id}

        # AI-407 §11.4: tap agent.astream_events for token-level TTS streaming.
        # Replaces agent.ainvoke (wait-for-full-result + emit one ChatChunk).
        # TTS starts synthesizing as soon as the first chunk arrives — biggest
        # single voice latency win per §11.4.
        #
        # Filter strategy: emit ChatChunks only for on_chat_model_stream events
        # with non-empty text content AND no tool_call_chunks. Skips:
        # - on_tool_start/end + on_chain_start/end (no assistant tokens)
        # - on_chat_model_start/end (lifecycle markers)
        # - chunks with tool_call_chunks (those are tool call args, not user-
        #   spoken text — TTS'ing them would speak JSON aloud)
        # - empty-content chunks (Gemini emits these between tool-call setups)
        chunk_count = 0
        try:
            async for event in agent.astream_events(
                {"messages": messages}, config=config, version="v2"
            ):
                if event.get("event") != "on_chat_model_stream":
                    continue
                model_chunk = event.get("data", {}).get("chunk")
                if model_chunk is None:
                    continue
                tool_call_chunks = getattr(model_chunk, "tool_call_chunks", []) or []
                if tool_call_chunks:
                    continue
                text = getattr(model_chunk, "content", "") or ""
                if isinstance(text, list):
                    # Multi-part content — extract text parts only
                    text = " ".join(
                        str(p.get("text", "")) if isinstance(p, dict) else str(p)
                        for p in text
                    ).strip()
                if not text:
                    continue

                self._event_ch.send_nowait(
                    ChatChunk(
                        id=f"deepagents-{chunk_count}",
                        delta=ChoiceDelta(role="assistant", content=text),
                    )
                )
                chunk_count += 1
        except Exception as e:
            log.exception("DeepagentsLLM agent.astream_events failed: %s", e)
            raise

        log.info(
            "DeepagentsLLM emitted %d streaming ChatChunks", chunk_count
        )


class DeepagentsLLM(LLM):
    """LiveKit-compatible LLM that runs a deepagents agent under the hood.

    Constructed once per VoiceSession with a built deepagents agent and a
    thread_id (the Interaction.id) for LangGraph checkpointing. AgentSession
    plugs this into the STT -> LLM -> TTS pipeline; on each user turn, the
    AgentSession calls `chat()` and waits for the LLMStream to emit chunks.
    """

    def __init__(
        self,
        agent,
        thread_id: str | None = None,
        event_queue: list | None = None,
        associate: dict | None = None,
        runtime_id: str | None = None,
        correlation_id: str | None = None,
        deployment_id: str | None = None,
        initial_systemmessages: list | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        # thread_id retained for back-compat naming (existing tests pass it);
        # semantically it's the interaction_id per AI-407 §13.3 (voice is a
        # real-time session → checkpointer key = interaction_id).
        self._thread_id = thread_id
        # event_queue is shared with VoiceSession._run_events_stream — appended
        # by the events-stream subprocess reader thread, drained here on each
        # user turn. Pass `None` to skip mid-conversation event injection.
        self._event_queue = event_queue
        # associate + runtime_id power LangSmith metadata + tags so voice
        # traces appear in the indemn-os-associates project queryable by
        # associate_id / entity_id / runtime_id (CLAUDE.md § 8).
        self._associate = associate
        self._runtime_id = runtime_id
        # AI-407 Phase 4 additions:
        # - correlation_id: §13.5 voice — metadata.thread_id (LangSmith UI
        #   grouping; cascade lineage). Distinct from thread_id (configurable
        #   checkpointer key per §13.2).
        # - deployment_id: §13.5 metadata field (cross-pivot search).
        # - initial_systemmessages: composed by VoiceSession.start() (Task 2.17);
        #   prepended to messages on the first agent.ainvoke() call.
        self._correlation_id = correlation_id
        self._deployment_id = deployment_id
        self._initial_systemmessages = initial_systemmessages

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
