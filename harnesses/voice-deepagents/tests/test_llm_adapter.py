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
        from harnesses.voice_deepagents.llm_adapter import (  # noqa: E501
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
        from harnesses.voice_deepagents.llm_adapter import (
            _livekit_chat_ctx_to_langchain,
        )

        ctx = MagicMock()
        ctx.items = [MagicMock(role="assistant", content="prior reply")]

        result = _livekit_chat_ctx_to_langchain(ctx)

        assert len(result) == 1
        assert isinstance(result[0], AIMessage)
        assert result[0].content == "prior reply"

    def test_system_message_becomes_system_message(self, patch_livekit_imports):
        from harnesses.voice_deepagents.llm_adapter import (
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
        from harnesses.voice_deepagents.llm_adapter import (
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
        from harnesses.voice_deepagents.llm_adapter import (
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
        from harnesses.voice_deepagents.llm_adapter import (
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
        from harnesses.voice_deepagents.llm_adapter import (
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
        from harnesses.voice_deepagents.llm_adapter import (
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
        from harnesses.voice_deepagents.llm_adapter import (
            _extract_final_assistant_text,
        )

        result = {"messages": [HumanMessage(content="hi")]}
        assert _extract_final_assistant_text(result) == ""

    def test_handles_multipart_ai_content(self, patch_livekit_imports):
        """Some LLMs emit multi-part AIMessage content (text + thought_signature
        for Gemini). Extract just the text parts."""
        from harnesses.voice_deepagents.llm_adapter import (
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
        from harnesses.voice_deepagents.llm_adapter import DeepagentsLLM

        llm = DeepagentsLLM(agent=MagicMock(), thread_id="i-1")
        assert llm.model == "deepagents"
        assert llm.provider == "indemn-deepagents"

    @pytest.mark.asyncio
    async def test_chat_returns_stream_that_runs_agent(self, patch_livekit_imports):
        """End-to-end smoke: chat() returns an LLMStream; iterating it invokes
        the wrapped agent and yields a ChatChunk with the final assistant text."""
        from livekit.agents.llm import ChatContext

        from harnesses.voice_deepagents.llm_adapter import DeepagentsLLM

        agent = MagicMock()
        agent.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    HumanMessage(content="status"),
                    AIMessage(content="All good."),
                ]
            }
        )

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
        agent.ainvoke.assert_called_once()
