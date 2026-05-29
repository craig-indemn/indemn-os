"""Tests for LangSmith metadata composition (AI-407 §13 + Task 2.4).

Pins the build_runnable_config helper. Per §13.3:
  - LangSmith metadata.thread_id = correlation_id (cascade-lineage view)
  - LangGraph configurable.thread_id = derive_checkpointer_thread_id(work_ctx)
    For async (this harness):
      - entity_type == "Interaction" → entity_id (handoff continuity)
      - else → message_id (per-invocation isolation)

Implementer note: AgentExecutionInput uses `entity_type` / `entity_id` field
names (not `target_entity_type` / `target_entity_id` as the playbook fixture
example suggests). Tests use actual AgentExecutionInput-shaped SimpleNamespaces.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Add harnesses/_base so the real harness_common package loads — the test needs
# the actual derive_checkpointer_thread_id implementation, not a MagicMock.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_base"))

# langchain_core real — needed for langsmith metadata to be inspectable.
# Import REAL harness_common.thread_id BEFORE stubbing other submodules — this
# registers the real package in sys.modules so main.py's
# `from harness_common.thread_id import derive_checkpointer_thread_id` gets
# the actual implementation (the test validates real §13.3 derivation logic).
from harness_common.thread_id import derive_checkpointer_thread_id  # noqa: E402,F401
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402,F401

for mod in [
    "deepagents",
    "harness",
    "harness.agent",
    "harness.cron_runner",
    "harness.trace_helpers",
    "harness_common.backend",
    "harness_common.cli",
    "harness_common.runtime",
    "indemn_os",
    "indemn_os.types",
    "langchain.agents",
    "langchain.agents.middleware",
    "langchain.agents.middleware.types",
    "langchain.chat_models",
    "langchain_core.tracers",
    "langchain_core.tracers.run_collector",
    "langgraph",
    "langgraph.checkpoint",
    "langgraph.checkpoint.memory",
    "langgraph.checkpoint.mongodb",
    "motor",
    "motor.motor_asyncio",
    "temporalio",
    "temporalio.client",
    "temporalio.contrib",
    "temporalio.contrib.opentelemetry",
    "temporalio.worker",
    "temporalio.activity",
]:
    sys.modules.setdefault(mod, MagicMock())


@pytest.fixture
def fake_input():
    """SimpleNamespace mirroring AgentExecutionInput's field shape."""
    return SimpleNamespace(
        associate_id="actor_abc",
        message_id="msg_def",
        correlation_id="cor_ghi",
        entity_type="Email",
        entity_id="email_jkl",
    )


@pytest.fixture
def fake_associate():
    return {"_id": "actor_abc", "name": "EmailClassifier"}


class TestLangsmithMetadata:
    def test_metadata_thread_id_is_correlation_id(self, fake_input, fake_associate):
        """LangSmith reads metadata.thread_id for UI grouping — must be correlation_id."""
        from main import build_runnable_config

        config = build_runnable_config(
            fake_input, fake_associate, runtime_id="rt_mno"
        )

        assert config["metadata"]["thread_id"] == "cor_ghi"

    def test_configurable_thread_id_uses_message_id_for_async(self, fake_input, fake_associate):
        """For async work targeting a non-Interaction, checkpointer key = message_id."""
        from main import build_runnable_config

        config = build_runnable_config(
            fake_input, fake_associate, runtime_id="rt_mno"
        )
        assert config["configurable"]["thread_id"] == "msg_def"

    def test_configurable_thread_id_uses_entity_id_for_interaction(
        self, fake_input, fake_associate
    ):
        """For async work targeting an Interaction, checkpointer key = entity_id
        (handoff continuity — multi-agent on same conversation)."""
        from main import build_runnable_config

        fake_input.entity_type = "Interaction"
        fake_input.entity_id = "int_xyz"

        config = build_runnable_config(
            fake_input, fake_associate, runtime_id="rt_mno"
        )
        assert config["configurable"]["thread_id"] == "int_xyz"

    def test_metadata_includes_all_ids(self, fake_input, fake_associate):
        """metadata carries IDs for cross-pivot search in LangSmith."""
        from main import build_runnable_config

        config = build_runnable_config(
            fake_input, fake_associate, runtime_id="rt_mno"
        )
        m = config["metadata"]

        assert m["correlation_id"] == "cor_ghi"
        assert m["message_id"] == "msg_def"
        assert m["associate_id"] == "actor_abc"
        assert m["associate_name"] == "EmailClassifier"
        assert m["entity_type"] == "Email"
        assert m["entity_id"] == "email_jkl"
        assert m["runtime_id"] == "rt_mno"

    def test_metadata_interaction_id_only_when_entity_is_interaction(
        self, fake_input, fake_associate
    ):
        """When entity_type=Interaction, metadata.interaction_id = entity_id;
        otherwise None. Lets LangSmith pivot 'all runs on this conversation'."""
        from main import build_runnable_config

        # Default (entity_type=Email) → interaction_id is None
        config_email = build_runnable_config(
            fake_input, fake_associate, runtime_id="rt_mno"
        )
        assert config_email["metadata"]["interaction_id"] is None

        # entity_type=Interaction → interaction_id = entity_id
        fake_input.entity_type = "Interaction"
        fake_input.entity_id = "int_xyz"
        config_interaction = build_runnable_config(
            fake_input, fake_associate, runtime_id="rt_mno"
        )
        assert config_interaction["metadata"]["interaction_id"] == "int_xyz"
