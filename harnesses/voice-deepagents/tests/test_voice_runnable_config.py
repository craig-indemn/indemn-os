"""VoiceSession + DeepagentsLLMStream build RunnableConfig per §13.5 voice
spec. AI-407 §13 ID semantics.

§13.5 voice mapping:
- `configurable.thread_id = interaction_id` (state continuity across turns —
  MongoDB checkpointer key per §13.3)
- `metadata.thread_id = correlation_id` (LangSmith UI grouping; cascade lineage)
- metadata also carries: correlation_id, interaction_id, associate_id,
  associate_name, entity_type=Interaction, entity_id=interaction_id,
  runtime_id, deployment_id

Pre-fix (Phase 3 voice llm_adapter.py): the config was missing
`metadata.thread_id = correlation_id` explicitly — LangSmith inferred thread
from `configurable.thread_id` as a fallback, which conflates the two
concepts. This task makes the wiring deliberate.

Module path imports + heavy-dep stubs come from `tests/conftest.py`.
"""


class TestVoiceRunnableConfig:
    def test_metadata_thread_id_is_correlation_id(self):
        """LangSmith UI grouping key = correlation_id (lineage)."""
        from session import VoiceSession

        config = VoiceSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "Sales Assistant"},
            runtime_id="rt_voice",
            deployment_id="dep_sales_voice",
        )

        assert config["metadata"]["thread_id"] == "cor_xyz"

    def test_configurable_thread_id_is_interaction_id(self):
        """Voice is real-time → checkpointer key = interaction_id (state across turns)."""
        from session import VoiceSession

        config = VoiceSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "Sales Assistant"},
            runtime_id="rt_voice",
            deployment_id="dep_sales_voice",
        )

        assert config["configurable"]["thread_id"] == "int_abc"

    def test_metadata_carries_required_fields(self):
        """metadata fields per §13.5 voice example: correlation_id,
        interaction_id, associate_id, associate_name, entity_type=Interaction,
        entity_id=interaction_id, runtime_id, deployment_id."""
        from session import VoiceSession

        config = VoiceSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "Sales Assistant"},
            runtime_id="rt_voice",
            deployment_id="dep_sales_voice",
        )

        m = config["metadata"]
        assert m["correlation_id"] == "cor_xyz"
        assert m["interaction_id"] == "int_abc"
        assert m["associate_id"] == "act_def"
        assert m["associate_name"] == "Sales Assistant"
        assert m["entity_type"] == "Interaction"
        assert m["entity_id"] == "int_abc"
        assert m["runtime_id"] == "rt_voice"
        assert m["deployment_id"] == "dep_sales_voice"

    def test_tags_include_channel_and_runtime(self):
        """Tags allow LangSmith filter-by-channel / filter-by-deployment."""
        from session import VoiceSession

        config = VoiceSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "Sales Assistant"},
            runtime_id="rt_voice",
            deployment_id="dep_sales_voice",
        )

        tags = config["tags"]
        assert "channel:voice" in tags
        assert "deployment:dep_sales_voice" in tags
        assert "associate:Sales Assistant" in tags

    def test_run_name_format(self):
        """Run name format: '{associate_name} → Interaction {short_id}'."""
        from session import VoiceSession

        config = VoiceSession.build_runnable_config(
            interaction_id="int_abcdefghijklmnop",
            correlation_id="cor_xyz",
            associate={"_id": "act_def", "name": "Sales Assistant"},
            runtime_id="rt_voice",
            deployment_id="dep_sales_voice",
        )

        assert "Sales Assistant" in config["run_name"]
        assert "int_abcd" in config["run_name"]  # first 8 chars

    def test_thread_ids_differ_so_cascade_isnt_conflated_with_checkpointer(self):
        """The key insight of §13.2 — these are TWO different fields that
        happen to share a name in LangChain's APIs. They MUST be set to
        different values: correlation_id (lineage) vs interaction_id (state).
        Pre-fix the voice harness set ONLY configurable.thread_id and let
        LangSmith infer — this test pins that we now set both explicitly
        + distinctly."""
        from session import VoiceSession

        config = VoiceSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "a", "name": "X"},
            runtime_id="rt",
            deployment_id="dep",
        )
        assert (
            config["configurable"]["thread_id"]
            != config["metadata"]["thread_id"]
        )

    def test_deployment_id_optional_falls_back_to_none_string(self):
        """If a session has no deployment_id (local-dev edge case), the tag
        should still serialize cleanly without breaking LangSmith."""
        from session import VoiceSession

        config = VoiceSession.build_runnable_config(
            interaction_id="int_abc",
            correlation_id="cor_xyz",
            associate={"_id": "a", "name": "X"},
            runtime_id="rt",
            deployment_id=None,
        )
        # metadata still carries it (as None)
        assert "deployment_id" in config["metadata"]
        # tags handle None gracefully
        assert any("deployment:" in t for t in config["tags"])
