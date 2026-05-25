"""Tests for harnesses/voice-deepagents/llm_adapter.py.

Pins the contract by which a deepagents CompiledStateGraph appears as a
livekit.agents.llm.LLM to LiveKit's AgentSession. The adapter:

- Takes a LiveKit ChatContext, translates it to LangChain BaseMessage
  format that deepagents `agent.ainvoke()` expects
- Invokes the agent (mocked here) and waits for the final state
- Extracts the last AIMessage with non-empty text content
- Emits that text as a ChatChunk via the LLMStream
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# These tests run inside the harness venv (built from the Dockerfile) where
# langchain_core + livekit-agents are installed. The kernel-side `pytest
# tests/unit/` invocation runs in a venv that lacks them, so skip the
# whole module gracefully there.
pytest.importorskip("langchain_core")
pytest.importorskip("livekit.agents.llm")

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402


@pytest.fixture
def patch_livekit_imports():
    """No-op fixture retained for test readability — the module-level
    importorskip already gated us in. Kept so individual tests document
    that they need livekit-agents present."""
    pass


class TestLivekitChatCtxTranslation:
    """The adapter must translate LiveKit's ChatContext into LangChain messages."""

    def test_user_message_becomes_human_message(self, patch_livekit_imports):
        from llm_adapter import (  # noqa: E501
            _livekit_chat_ctx_to_langchain,
        )

        ctx = MagicMock()
        item = MagicMock(role="user", content="hello")
        ctx.items = [item]

        result = _livekit_chat_ctx_to_langchain(ctx)

        assert len(result) == 1
        assert isinstance(result[0], HumanMessage)
        assert result[0].content == "hello"

    def test_assistant_message_becomes_ai_message(self, patch_livekit_imports):
        from llm_adapter import (
            _livekit_chat_ctx_to_langchain,
        )

        ctx = MagicMock()
        ctx.items = [MagicMock(role="assistant", content="prior reply")]

        result = _livekit_chat_ctx_to_langchain(ctx)

        assert len(result) == 1
        assert isinstance(result[0], AIMessage)
        assert result[0].content == "prior reply"

    def test_system_message_becomes_system_message(self, patch_livekit_imports):
        from llm_adapter import (
            _livekit_chat_ctx_to_langchain,
        )

        ctx = MagicMock()
        ctx.items = [MagicMock(role="system", content="instructions")]

        result = _livekit_chat_ctx_to_langchain(ctx)

        assert len(result) == 1
        assert isinstance(result[0], SystemMessage)

    def test_empty_content_skipped(self, patch_livekit_imports):
        """Items with no content (or whitespace-only) don't appear in the result —
        deepagents would treat them as malformed turns."""
        from llm_adapter import (
            _livekit_chat_ctx_to_langchain,
        )

        ctx = MagicMock()
        ctx.items = [
            MagicMock(role="user", content="   "),
            MagicMock(role="user", content="real message"),
        ]

        result = _livekit_chat_ctx_to_langchain(ctx)

        assert len(result) == 1
        assert result[0].content == "real message"

    def test_unknown_role_skipped(self, patch_livekit_imports):
        """Tool/function roles aren't surfaced — deepagents owns its own
        tool history; LiveKit-side tool calls (none, in our case) aren't
        translated back."""
        from llm_adapter import (
            _livekit_chat_ctx_to_langchain,
        )

        ctx = MagicMock()
        ctx.items = [
            MagicMock(role="user", content="hi"),
            MagicMock(role="tool", content="tool result"),
            MagicMock(role="function", content="fn output"),
        ]

        result = _livekit_chat_ctx_to_langchain(ctx)

        assert len(result) == 1  # only the user message survives
        assert isinstance(result[0], HumanMessage)

    def test_multipart_content_concatenates_text(self, patch_livekit_imports):
        """LiveKit content can be a list of parts (e.g., text + image). For
        voice we only carry text; non-text parts get stringified harmlessly."""
        from llm_adapter import (
            _livekit_chat_ctx_to_langchain,
        )

        ctx = MagicMock()
        part1 = MagicMock(text="hello")
        part2 = MagicMock(text="world")
        item = MagicMock(role="user", content=[part1, part2])
        ctx.items = [item]

        result = _livekit_chat_ctx_to_langchain(ctx)

        assert len(result) == 1
        assert "hello" in result[0].content
        assert "world" in result[0].content


class TestExtractFinalAssistantText:
    """The adapter extracts the last AIMessage with non-empty text from the
    deepagents agent state and returns it for TTS to speak."""

    def test_returns_last_ai_message_text(self, patch_livekit_imports):
        from llm_adapter import (
            _extract_final_assistant_text,
        )

        result = {
            "messages": [
                HumanMessage(content="hi"),
                AIMessage(content="thinking..."),
                HumanMessage(content="status?"),
                AIMessage(content="All good — done."),
            ]
        }
        assert _extract_final_assistant_text(result) == "All good — done."

    def test_skips_ai_messages_with_only_tool_calls(self, patch_livekit_imports):
        """Intermediate AIMessages with no text content (just tool calls) are
        the agent's internal reasoning — TTS shouldn't speak them."""
        from llm_adapter import (
            _extract_final_assistant_text,
        )

        result = {
            "messages": [
                HumanMessage(content="run fetch"),
                AIMessage(content=""),  # just tool calls in the original
                AIMessage(content="Fetched 5 messages, all good."),
            ]
        }
        assert _extract_final_assistant_text(result) == "Fetched 5 messages, all good."

    def test_returns_empty_when_no_ai_messages(self, patch_livekit_imports):
        from llm_adapter import (
            _extract_final_assistant_text,
        )

        result = {"messages": [HumanMessage(content="hi")]}
        assert _extract_final_assistant_text(result) == ""

    def test_handles_multipart_ai_content(self, patch_livekit_imports):
        """Some LLMs emit multi-part AIMessage content (text + thought_signature
        for Gemini). Extract just the text parts."""
        from llm_adapter import (
            _extract_final_assistant_text,
        )

        result = {
            "messages": [
                AIMessage(
                    content=[
                        {"type": "text", "text": "Hello"},
                        {"type": "thought_signature", "value": "abc"},
                        {"type": "text", "text": "world."},
                    ]
                )
            ]
        }
        text = _extract_final_assistant_text(result)
        assert "Hello" in text
        assert "world." in text
        assert "abc" not in text


class TestDeepagentsLLMShape:
    """DeepagentsLLM exposes the right LiveKit interface — model, provider,
    chat() returns an LLMStream that runs the wrapped agent."""

    def test_model_and_provider_identifiers(self, patch_livekit_imports):
        from llm_adapter import DeepagentsLLM

        llm = DeepagentsLLM(agent=MagicMock(), thread_id="i-1")
        assert llm.model == "deepagents"
        assert llm.provider == "indemn-deepagents"

    @pytest.mark.asyncio
    async def test_chat_returns_stream_that_runs_agent(self, patch_livekit_imports):
        """End-to-end smoke: chat() returns an LLMStream; iterating it invokes
        the wrapped agent via astream_events and yields ChatChunks with the
        streamed assistant tokens. Post-AI-407 Task 2.20: astream_events
        replaces ainvoke for token-level TTS streaming."""
        from livekit.agents.llm import ChatContext

        from llm_adapter import DeepagentsLLM

        astream_calls = []

        async def fake_astream_events(input_dict, config=None, **kwargs):
            astream_calls.append(input_dict)

            class _Chunk:
                content = "All good."
                tool_call_chunks = []

            yield {"event": "on_chat_model_stream", "data": {"chunk": _Chunk()}}
            yield {"event": "on_chat_model_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_astream_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-2")

        # Build a real ChatContext with one user message
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="status")

        stream = llm.chat(chat_ctx=chat_ctx)

        # Drain the stream — should yield one chunk with our final text
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)

        assert len(chunks) >= 1
        assert any(
            c.delta and c.delta.content and "All good." in c.delta.content
            for c in chunks
        )
        assert len(astream_calls) == 1  # astream_events called once


class TestEventQueueDrain:
    """The adapter drains VoiceSession._event_queue on each user turn and
    prepends a SystemMessage describing entity changes that happened while
    the user was talking — same mid-conversation awareness pattern as
    ChatSession.handle_message."""

    def test_drain_returns_none_for_empty_queue(self, patch_livekit_imports):
        from llm_adapter import _drain_event_queue

        assert _drain_event_queue([]) is None
        assert _drain_event_queue(None) is None

    def test_drain_summarizes_events(self, patch_livekit_imports):
        from llm_adapter import _drain_event_queue

        queue = [
            {"entity_type": "Touchpoint", "entity_id": "tp-1", "event_type": "created"},
            {"entity_type": "Email", "entity_id": "e-9", "event_type": "transitioned"},
        ]
        msg = _drain_event_queue(queue)
        assert msg is not None
        assert "Touchpoint/tp-1: created" in msg
        assert "Email/e-9: transitioned" in msg
        assert msg.startswith("[System events since last turn:")
        # Drain mutates — queue should be empty after.
        assert queue == []

    def test_drain_handles_missing_fields(self, patch_livekit_imports):
        """Robust against malformed events from the stream subprocess."""
        from llm_adapter import _drain_event_queue

        msg = _drain_event_queue([{}])
        assert "unknown/unknown: change" in msg

    @pytest.mark.asyncio
    async def test_stream_prepends_system_message_when_events_present(
        self, patch_livekit_imports
    ):
        """The full path: events queued → adapter drains → SystemMessage
        prepended to the agent's input messages. Post-AI-407 Task 2.20:
        astream_events captures the input via the first call argument."""
        from livekit.agents.llm import ChatContext

        from llm_adapter import DeepagentsLLM

        captured_messages: list = []

        async def fake_astream_events(input_dict, config=None, **kwargs):
            captured_messages.extend(input_dict.get("messages", []))

            class _Chunk:
                content = "ack"
                tool_call_chunks = []

            yield {"event": "on_chat_model_stream", "data": {"chunk": _Chunk()}}
            yield {"event": "on_chat_model_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_astream_events

        # Pre-populate the queue (as if the events subprocess pushed events)
        event_queue = [
            {"entity_type": "Touchpoint", "entity_id": "tp-99", "event_type": "created"}
        ]

        llm = DeepagentsLLM(agent=agent, thread_id="i-3", event_queue=event_queue)

        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="status?")

        stream = llm.chat(chat_ctx=chat_ctx)
        async for _ in stream:
            pass

        # Should have prepended a SystemMessage describing the queued event.
        assert len(captured_messages) == 2
        assert isinstance(captured_messages[0], SystemMessage)
        assert "Touchpoint/tp-99: created" in captured_messages[0].content
        assert isinstance(captured_messages[1], HumanMessage)
        assert event_queue == []  # drained

    @pytest.mark.asyncio
    async def test_stream_skips_system_message_when_queue_empty(
        self, patch_livekit_imports
    ):
        """No drain message when no events queued. Post-AI-407 Task 2.20:
        astream_events replaces ainvoke."""
        from livekit.agents.llm import ChatContext

        from llm_adapter import DeepagentsLLM

        captured_messages: list = []

        async def fake_astream_events(input_dict, config=None, **kwargs):
            captured_messages.extend(input_dict.get("messages", []))

            class _Chunk:
                content = "ack"
                tool_call_chunks = []

            yield {"event": "on_chat_model_stream", "data": {"chunk": _Chunk()}}
            yield {"event": "on_chat_model_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_astream_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-4", event_queue=[])

        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="hi")

        stream = llm.chat(chat_ctx=chat_ctx)
        async for _ in stream:
            pass

        # Only the user message — no SystemMessage prepended.
        assert len(captured_messages) == 1
        assert isinstance(captured_messages[0], HumanMessage)
