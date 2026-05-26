"""Voice events-queue drain on user turns (AI-407 §11 + playbook Task 2.21).

Mid-conversation entity changes (a new Touchpoint arrives, a Document gets
classified, a Deal transitions stage) flow into VoiceSession._event_queue
via the `indemn events stream` subprocess. Chat-deepagents drains this
queue at the start of each user turn in session.py::handle_message. Voice
matches the pattern but wraps the summary in <entity_events>...</entity_events>
to align with <skill> + <deployment_context> SystemMessage XML conventions.

Playbook Task 2.21 format:
  <entity_events>
  The following entity changes happened since the last turn:
  - {event_type} id={entity_id} company={X} actor={Y} summary={Z}
  - ...
  </entity_events>

Module path imports + heavy-dep stubs come from `tests/conftest.py`.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def patch_livekit_imports():
    pytest.importorskip("livekit.agents.llm")


def _make_chunk(content: str, tool_call_chunks=None):
    chunk = MagicMock()
    chunk.content = content
    chunk.tool_call_chunks = tool_call_chunks or []
    return chunk


class TestVoiceEventsDrain:
    @pytest.mark.asyncio
    async def test_events_queue_drained_into_entity_events_systemmessage(
        self, patch_livekit_imports
    ):
        """Events queued before a user turn are drained into a SystemMessage
        prepended to the agent input on _run()."""
        from langchain_core.messages import SystemMessage
        from livekit.agents.llm import ChatContext
        from llm_adapter import DeepagentsLLM

        captured_input = {}

        async def fake_astream_events(input_dict, config=None, **kwargs):
            captured_input["messages"] = input_dict.get("messages", [])
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk("ack")},
            }

        agent = MagicMock()
        agent.astream_events = fake_astream_events

        # Pre-populate the queue with two events
        event_queue = [
            {
                "event_type": "Touchpoint:created",
                "entity_id": "tp_abc",
                "company": "Branch Insurance",
            },
            {
                "event_type": "Document:classified",
                "entity_id": "doc_xyz",
                "company": "Branch Insurance",
            },
        ]

        llm = DeepagentsLLM(
            agent=agent, thread_id="i-events-1", event_queue=event_queue
        )
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="tell me about Branch")
        stream = llm.chat(chat_ctx=chat_ctx)

        async for _ in stream:
            pass

        # The queue is drained
        assert len(event_queue) == 0

        # The agent invocation received the events as a prepended SystemMessage
        messages = captured_input["messages"]
        sys_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        event_summary_msg = next(
            (m for m in sys_msgs if "<entity_events>" in m.content), None
        )
        assert event_summary_msg is not None
        assert "</entity_events>" in event_summary_msg.content
        assert "Touchpoint:created" in event_summary_msg.content
        assert "tp_abc" in event_summary_msg.content
        assert "Document:classified" in event_summary_msg.content
        # Company context surfaced for the agent's mid-conversation awareness
        assert "Branch Insurance" in event_summary_msg.content

    @pytest.mark.asyncio
    async def test_empty_event_queue_does_not_prepend(self, patch_livekit_imports):
        """No events queued → no <entity_events> SystemMessage prepended."""
        from langchain_core.messages import SystemMessage
        from livekit.agents.llm import ChatContext
        from llm_adapter import DeepagentsLLM

        captured_input = {}

        async def fake_astream_events(input_dict, config=None, **kwargs):
            captured_input["messages"] = input_dict.get("messages", [])
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _make_chunk("hi")},
            }

        agent = MagicMock()
        agent.astream_events = fake_astream_events

        llm = DeepagentsLLM(agent=agent, thread_id="i-events-2", event_queue=[])
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content="hello")
        stream = llm.chat(chat_ctx=chat_ctx)

        async for _ in stream:
            pass

        messages = captured_input["messages"]
        assert not any(
            isinstance(m, SystemMessage) and "<entity_events>" in m.content
            for m in messages
        )

    def test_format_event_includes_known_fields(self):
        """The _format_event helper renders type + id + company/actor/summary."""
        from llm_adapter import _format_event

        ev = {
            "event_type": "Touchpoint:created",
            "entity_id": "tp_abc",
            "company": "Branch Insurance",
            "actor": "kyle@indemn.ai",
            "summary": "logged a discovery call",
        }
        line = _format_event(ev)
        assert "Touchpoint:created" in line
        assert "id=tp_abc" in line
        assert "company=Branch Insurance" in line
        assert "actor=kyle@indemn.ai" in line
        assert "summary=logged a discovery call" in line
        assert line.startswith("- ")  # bulleted line

    def test_format_event_handles_minimal_event(self):
        """Malformed events (missing optional fields) render the available bits."""
        from llm_adapter import _format_event

        line = _format_event({})
        # Returns a sentinel-like line — falls back to "?" event_type
        assert "?" in line

        line2 = _format_event({"event_type": "Email:received"})
        assert "Email:received" in line2
