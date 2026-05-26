"""Tests for derive_checkpointer_thread_id (AI-407 §13.3).

Design §13 splits the overloaded `thread_id` concept:
  - LangSmith metadata.thread_id (cascade-lineage view) → always correlation_id
  - LangGraph configurable.thread_id (checkpointer key) → derived per the rule

This module covers the second — the checkpointer-key derivation. The rule
tracks the SUBJECT of the work:
  - Real-time session → interaction_id (state across turns)
  - Async targeting an Interaction → target_entity_id (handoff continuity)
  - Async other → message_id (per-invocation isolation in cascades)
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

HARNESS_BASE = Path(__file__).resolve().parents[2]
if str(HARNESS_BASE) not in sys.path:
    sys.path.insert(0, str(HARNESS_BASE))


@dataclass
class FakeContext:
    """Stand-in for the WorkContext Protocol that the real harnesses pass.

    Mirrors both shapes (real-time + async) since the utility branches on
    is_real_time_session at the top.
    """

    is_real_time_session: bool = False
    interaction_id: str | None = None
    target_entity_type: str | None = None
    target_entity_id: str | None = None
    message_id: str | None = None


class TestDeriveCheckpointerThreadId:
    def test_real_time_uses_interaction_id(self):
        from harness_common.thread_id import derive_checkpointer_thread_id

        ctx = FakeContext(is_real_time_session=True, interaction_id="int_abc")
        assert derive_checkpointer_thread_id(ctx) == "int_abc"

    def test_async_targeting_interaction_uses_target_id(self):
        from harness_common.thread_id import derive_checkpointer_thread_id

        ctx = FakeContext(
            is_real_time_session=False,
            target_entity_type="Interaction",
            target_entity_id="int_xyz",
            message_id="msg_qrs",
        )
        assert derive_checkpointer_thread_id(ctx) == "int_xyz"

    def test_async_other_entity_uses_message_id(self):
        from harness_common.thread_id import derive_checkpointer_thread_id

        ctx = FakeContext(
            is_real_time_session=False,
            target_entity_type="Email",
            target_entity_id="email_def",
            message_id="msg_ghi",
        )
        assert derive_checkpointer_thread_id(ctx) == "msg_ghi"

    def test_real_time_without_interaction_id_raises(self):
        from harness_common.thread_id import derive_checkpointer_thread_id

        ctx = FakeContext(is_real_time_session=True, interaction_id=None)
        with pytest.raises(ValueError, match="interaction_id"):
            derive_checkpointer_thread_id(ctx)

    def test_async_without_message_id_raises(self):
        from harness_common.thread_id import derive_checkpointer_thread_id

        ctx = FakeContext(
            is_real_time_session=False,
            target_entity_type="Email",
            target_entity_id="email_def",
            message_id=None,
        )
        with pytest.raises(ValueError, match="message_id"):
            derive_checkpointer_thread_id(ctx)
