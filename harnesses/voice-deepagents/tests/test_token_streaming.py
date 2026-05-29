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

from unittest.mock import MagicMock

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


class TestTextAndToolInSameChunk:
    """AI-407 Task 2.38 follow-up: when a model streams text content + a
    tool_call in the SAME chunk (some models do — preamble "I'll look
    that up..." alongside the function call), the text MUST still reach
    TTS. The old filter `if tool_call_chunks: continue` was too aggressive
    and dropped these chunks entirely. Commit a0cd710 relaxed the filter
    to skip only when text is empty, regardless of tool_call_chunks.

    Pins the relaxed-filter contract so a future tightening doesn't
    silently regress voice latency / coherence."""

    @pytest.mark.asyncio
    async def test_text_with_tool_call_in_same_chunk_emits_text(
        self, patch_livekit_imports
    ):
        """Chunk with content='I'll check that' AND tool_call_chunks
        present → text IS emitted (downstream TTS picks it up)."""
        from livekit.agents.llm import ChatContext
        from llm_adapter import DeepagentsLLM

        async def fake_events(*args, **kwargs):
            # Text preamble + tool call in the SAME chunk
            yield {
                "event": "on_chat_model_stream",
                "data": {
                    "chunk": _make_chunk(
                        "I'll check that",
                        tool_call_chunks=[{"name": "execute", "args": "{...}"}],
                    )
                },
            }
            yield {"event": "on_chat_model_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-text-and-tool")
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="look that up")
        stream = llm.chat(chat_ctx=chat_ctx)

        chunks = [c async for c in stream]
        text = "".join(
            c.delta.content for c in chunks if c.delta and c.delta.content
        )
        # Text reached TTS even though tool_call_chunks was present
        assert "I'll check that" in text
        # Tool call args did NOT reach TTS (only the text content,
        # not the function_call args)
        assert "execute" not in text


class TestZeroTextFallback:
    """AI-407 Task 2.38 follow-up: when the agent emits ONLY tool-call
    chunks throughout an entire stream (observed live: Gemini Flash
    enters tool-exploration mode → empty content + only tool calls
    → 0 ChatChunks emitted → user hears silence → interrupts →
    agent cancelled). Commit a0cd710 added a post-stream fallback
    chunk `"One moment."` when chunk_count == 0 so the user always
    hears SOMETHING. Silence breaks the voice loop; a brief
    acknowledgement preserves it.

    Pins the fallback contract so future filter changes don't
    accidentally remove the safety net."""

    @pytest.mark.asyncio
    async def test_zero_text_chunks_triggers_fallback_emit(
        self, patch_livekit_imports
    ):
        """Stream that yields ONLY tool-call chunks (no text content)
        → post-stream fallback emits exactly one ChatChunk with
        'One moment.' content."""
        from livekit.agents.llm import ChatContext
        from llm_adapter import DeepagentsLLM

        async def fake_events(*args, **kwargs):
            # Three tool-call chunks; no text content anywhere
            for cmd in (
                "indemn --help",
                "indemn entity --help",
                "indemn company list",
            ):
                yield {
                    "event": "on_chat_model_stream",
                    "data": {
                        "chunk": _make_chunk(
                            "",
                            tool_call_chunks=[
                                {"name": "execute", "args": f'{{"command":"{cmd}"}}'}
                            ],
                        )
                    },
                }
            yield {"event": "on_chat_model_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-zero-text-fallback")
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="look that up")
        stream = llm.chat(chat_ctx=chat_ctx)

        chunks = [c async for c in stream]
        text_chunks = [
            c for c in chunks if c.delta and c.delta.content
        ]
        # Exactly ONE chunk emitted (the fallback)
        assert len(text_chunks) == 1
        assert text_chunks[0].delta.content == "One moment."

    @pytest.mark.asyncio
    async def test_nonzero_text_chunks_skips_fallback(
        self, patch_livekit_imports
    ):
        """Stream with any text chunk → fallback does NOT fire (the
        normal text path already covered the user's hearing)."""
        from livekit.agents.llm import ChatContext
        from llm_adapter import DeepagentsLLM

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk("Hi there.")},
            }
            yield {"event": "on_chat_model_end", "data": {}}

        agent = MagicMock()
        agent.astream_events = fake_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-nonzero-text")
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="hi")
        stream = llm.chat(chat_ctx=chat_ctx)

        chunks = [c async for c in stream]
        text = "".join(
            c.delta.content for c in chunks if c.delta and c.delta.content
        )
        # The normal text reached TTS
        assert "Hi there." in text
        # Fallback did NOT fire (no extra "One moment." appended)
        assert "One moment." not in text
