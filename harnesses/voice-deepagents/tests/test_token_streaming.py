"""Token-level TTS streaming in DeepagentsLLM (AI-407 §11.4).

Phase 4 voice: replace `agent.ainvoke()` (wait-for-full-result) with
`agent.astream_events()` (emit ChatChunks per-token as the FINAL assistant
message tokens generate). TTS starts synthesizing as soon as the first
chunk arrives — biggest single voice latency win per §11.4.

Filter strategy: only emit `on_chat_model_stream` events with non-empty
text content AND no tool_call_chunks. This skips intermediate reasoning
(tool calls; their args go via tool_call_chunks not content) and lets only
the agent's final spoken response flow to TTS.

Module path imports + heavy-dep stubs come from `tests/conftest.py`.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def patch_livekit_imports():
    """Skip cleanly if livekit-agents isn't installed in this venv."""
    pytest.importorskip("livekit.agents.llm")


def _make_chunk(content: str, tool_call_chunks=None):
    """Build a MagicMock that mimics a langchain_core AIMessageChunk."""
    chunk = MagicMock()
    chunk.content = content
    chunk.tool_call_chunks = tool_call_chunks or []
    return chunk


class TestTokenStreaming:
    @pytest.mark.asyncio
    async def test_astream_events_yields_chatchunk_per_token(
        self, patch_livekit_imports
    ):
        """Each on_chat_model_stream event with text content → one ChatChunk."""
        from livekit.agents.llm import ChatContext
        from llm_adapter import DeepagentsLLM

        async def fake_events(*args, **kwargs):
            yield {"event": "on_chat_model_start", "data": {}}
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk("Hi")},
            }
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk(", how")},
            }
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk(" can I help?")},
            }
            yield {"event": "on_chat_model_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-stream-1")
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="hi")
        stream = llm.chat(chat_ctx=chat_ctx)

        chunks = [c async for c in stream]

        # Three text-bearing on_chat_model_stream events → three ChatChunks
        text_chunks = [
            c
            for c in chunks
            if c.delta and c.delta.content
        ]
        assert len(text_chunks) >= 3
        concatenated = "".join(c.delta.content for c in text_chunks)
        assert "Hi" in concatenated
        assert "how" in concatenated
        assert "help?" in concatenated

    @pytest.mark.asyncio
    async def test_tool_call_chunks_filtered_out(self, patch_livekit_imports):
        """Chunks emitting tool_call args (tool_call_chunks present) must
        NOT be TTS'd — that would speak JSON tool args aloud to the user."""
        from livekit.agents.llm import ChatContext
        from llm_adapter import DeepagentsLLM

        async def fake_events(*args, **kwargs):
            # Intermediate reasoning chunk emitting a tool call — content empty,
            # tool_call_chunks present. Should be SKIPPED.
            yield {
                "event": "on_chat_model_stream",
                "data": {
                    "chunk": _make_chunk(
                        "",
                        tool_call_chunks=[{"name": "execute", "args": "{...}"}],
                    )
                },
            }
            # Final response text — emitted.
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk("Done.")},
            }
            yield {"event": "on_chat_model_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-stream-2")
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="run something")
        stream = llm.chat(chat_ctx=chat_ctx)

        chunks = [c async for c in stream]
        text = "".join(
            c.delta.content for c in chunks if c.delta and c.delta.content
        )
        # No JSON args, no tool name leaked
        assert "execute" not in text
        assert "Done." in text

    @pytest.mark.asyncio
    async def test_empty_content_chunks_skipped(self, patch_livekit_imports):
        """Some chunks have empty content (Gemini emits empty stream events
        between tool-call setups). These should not produce empty ChatChunks."""
        from livekit.agents.llm import ChatContext
        from llm_adapter import DeepagentsLLM

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk("")},
            }
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk("Hello.")},
            }
            yield {"event": "on_chat_model_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-stream-3")
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="hi")
        stream = llm.chat(chat_ctx=chat_ctx)

        chunks = [c async for c in stream]
        text_chunks = [
            c
            for c in chunks
            if c.delta and c.delta.content
        ]
        # Only one chunk with "Hello." — the empty content chunk was skipped
        assert len(text_chunks) == 1
        assert text_chunks[0].delta.content == "Hello."

    @pytest.mark.asyncio
    async def test_other_event_types_ignored(self, patch_livekit_imports):
        """on_chain_start, on_tool_start/end, on_chat_model_start/end events
        don't carry assistant tokens — they should not emit ChatChunks."""
        from livekit.agents.llm import ChatContext
        from llm_adapter import DeepagentsLLM

        async def fake_events(*args, **kwargs):
            yield {"event": "on_chain_start", "data": {}}
            yield {"event": "on_chat_model_start", "data": {}}
            yield {"event": "on_tool_start", "data": {}}
            yield {"event": "on_tool_end", "data": {}}
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk("Result.")},
            }
            yield {"event": "on_chat_model_end", "data": {}}
            yield {"event": "on_chain_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-stream-4")
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="x")
        stream = llm.chat(chat_ctx=chat_ctx)

        chunks = [c async for c in stream]
        text_chunks = [
            c for c in chunks if c.delta and c.delta.content
        ]
        assert len(text_chunks) == 1
        assert text_chunks[0].delta.content == "Result."
