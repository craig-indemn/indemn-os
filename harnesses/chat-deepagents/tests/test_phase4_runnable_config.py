"""ChatSession.build_runnable_config (AI-407 §13.5 chat).

Per §13.3 the chat harness is real-time → configurable.thread_id = interaction_id
(state continuity across turns). Per §13.2 metadata.thread_id = correlation_id
(LangSmith UI grouping by cascade lineage — separate concept from checkpointer
state key).

Step 0 audit finding (per §15.3 pre-migration check): the current chat code
already uses `configurable.thread_id = self.interaction_id` at the agent
astream_events call site — so Phase 4 is a no-op continuation for the
checkpointer key (no existing checkpoints get invalidated). The behavioral
change in this task: explicitly set metadata.thread_id = correlation_id +
populate the full §13.5 metadata block.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# harnesses/_base for real harness_common.thread_id
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_base"))

# Real langchain_core (Phase 4 uses real SystemMessage type)
# Real harness_common.thread_id (build_runnable_config uses derive_checkpointer_thread_id)
from harness_common.thread_id import derive_checkpointer_thread_id  # noqa: E402,F401
from langchain_core.messages import SystemMessage  # noqa: E402,F401

for mod in [
    "deepagents",
    "harness",
    "harness.agent",
    "harness_common.backend",
    "harness_common.cli",
    "harness_common.runtime",
    "harness_common.attention",
    "harness_common.interaction",
    "langchain",
    "langchain.chat_models",
    "starlette",
    "starlette.websockets",
    "langgraph",
    "langgraph.checkpoint",
    "langgraph.checkpoint.memory",
    "langgraph.checkpoint.mongodb",
    "motor",
    "motor.motor_asyncio",
]:
    sys.modules.setdefault(mod, MagicMock())

from session import ChatSession  # noqa: E402


class TestChatRunnableConfig:
    def test_metadata_thread_id_is_correlation_id(self):
        """LangSmith reads metadata.thread_id for UI grouping — must be correlation_id."""
        config = ChatSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "OS Assistant"},
            runtime_id="rt_ghi",
            deployment_id="dep_jkl",
        )
        assert config["metadata"]["thread_id"] == "cor_xyz"

    def test_configurable_thread_id_is_interaction_id(self):
        """Chat is real-time → checkpointer key = interaction_id (state across turns)."""
        config = ChatSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "OS Assistant"},
            runtime_id="rt_ghi",
            deployment_id="dep_jkl",
        )
        assert config["configurable"]["thread_id"] == "int_abc"

    def test_metadata_contains_required_fields(self):
        """metadata carries IDs for cross-pivot search in LangSmith."""
        config = ChatSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "OS Assistant"},
            runtime_id="rt_ghi",
            deployment_id="dep_jkl",
        )
        m = config["metadata"]

        assert m["interaction_id"] == "int_abc"
        assert m["correlation_id"] == "cor_xyz"
        assert m["associate_id"] == "act_def"
        assert m["associate_name"] == "OS Assistant"
        assert m["runtime_id"] == "rt_ghi"
        assert m["deployment_id"] == "dep_jkl"
        assert m["entity_type"] == "Interaction"
        assert m["entity_id"] == "int_abc"

    def test_tags_include_channel_chat_and_runtime(self):
        """Tags allow LangSmith filter-by-channel / filter-by-runtime."""
        config = ChatSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "OS Assistant"},
            runtime_id="rt_ghi",
            deployment_id="dep_jkl",
        )
        tags = config["tags"]
        assert "channel:chat" in tags
        assert "associate:OS Assistant" in tags

    def test_run_name_includes_associate_and_interaction_short_id(self):
        """Run name format: '{associate_name} → Interaction {short_id}'."""
        config = ChatSession.build_runnable_config(
            interaction_id="int_abcdefghijklmnop",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "OS Assistant"},
            runtime_id="rt_ghi",
            deployment_id="dep_jkl",
        )
        assert "OS Assistant" in config["run_name"]
        assert "int_abcd" in config["run_name"]  # first 8 chars

    def test_handles_missing_deployment_id(self):
        """Sales-UI's first connect (pre-AI-408) may have no deployment_id —
        config should still build cleanly without crashing."""
        config = ChatSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "OS Assistant"},
            runtime_id="rt_ghi",
            deployment_id=None,
        )
        # deployment_id = None is acceptable (gets recorded in metadata as None)
        assert config["metadata"]["deployment_id"] is None
        assert config["configurable"]["thread_id"] == "int_abc"
