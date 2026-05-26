"""VoiceSession reads deployment_id from room.metadata (AI-407 §10.3.2).

Per design §10.3.2, the worker reads `deployment_id` + `dynamic_params` +
`interaction_id` + `correlation_id` from `ctx.room.metadata` — set by the
frontdoor service at session-create time. Replaces the Phase 3 pattern of
reading associate.deployment_id (the 1:1 association is dropped — one
Associate can be deployed in many Deployments).

Per §10.6: NO auth tokens in room metadata (visible to all participants per
LiveKit protocol). The worker authenticates via its own INDEMN_SERVICE_TOKEN
env var.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# session.py imports from `harness.agent` + `harness.llm_adapter` (the Docker
# /app/harness/ package path); locally these modules live flat in the
# directory above. Stub the `harness` package so the import resolves.
for mod in [
    "harness",
    "harness.agent",
    "harness.llm_adapter",
    "deepagents",
    "harness_common",
    "harness_common.attention",
    "harness_common.backend",
    "harness_common.cli",
    "harness_common.interaction",
    "harness_common.runtime",
    "langchain",
    "langchain.chat_models",
    "langchain_core",
    "langchain_core.messages",
    "livekit",
    "livekit.agents",
    "livekit.agents.llm",
    "livekit.agents.llm.tool_context",
    "livekit.agents.types",
]:
    sys.modules.setdefault(mod, MagicMock())

# Make `harness_common.cli.CLIError` a real Exception subclass so `except CLIError`
# in session.py doesn't raise TypeError when MagicMock pretends to be it.
class _StubCLIError(Exception):
    pass


sys.modules["harness_common.cli"].CLIError = _StubCLIError

from session import VoiceSession  # noqa: E402


class FakeRoom:
    """Mimics livekit.rtc.Room for tests — only the metadata attribute matters
    for parse_room_metadata."""

    def __init__(self, metadata):
        if isinstance(metadata, dict):
            self.metadata = json.dumps(metadata)
        else:
            self.metadata = metadata  # raw str for invalid-JSON tests
        self.sid = "test-room-sid"
        self.name = "test-room-name"


class TestRoomMetadataLoad:
    def test_extracts_deployment_id_from_metadata(self):
        room = FakeRoom({"deployment_id": "dep_abc", "interaction_id": "int_xyz"})
        meta = VoiceSession.parse_room_metadata(room)

        assert meta["deployment_id"] == "dep_abc"
        assert meta["interaction_id"] == "int_xyz"

    def test_extracts_dynamic_params(self):
        room = FakeRoom(
            {
                "deployment_id": "dep_abc",
                "interaction_id": "int_xyz",
                "dynamic_params": {"actor_id": "act_user", "current_route": "/proposal/new"},
                "correlation_id": "cor_lineage",
            }
        )
        meta = VoiceSession.parse_room_metadata(room)

        assert meta["dynamic_params"]["actor_id"] == "act_user"
        assert meta["dynamic_params"]["current_route"] == "/proposal/new"
        assert meta["correlation_id"] == "cor_lineage"

    def test_missing_deployment_id_raises(self):
        room = FakeRoom({"interaction_id": "int_xyz"})  # no deployment_id
        with pytest.raises(ValueError, match="deployment_id"):
            VoiceSession.parse_room_metadata(room)

    def test_invalid_json_raises(self):
        room = FakeRoom({})
        room.metadata = "not-valid-json"
        with pytest.raises(ValueError, match="metadata"):
            VoiceSession.parse_room_metadata(room)

    def test_empty_metadata_raises(self):
        """Empty string metadata (LiveKit's default for no metadata) is a hard error —
        the worker REQUIRES the frontdoor to have set metadata."""
        room = FakeRoom({})
        room.metadata = ""
        with pytest.raises(ValueError, match="metadata"):
            VoiceSession.parse_room_metadata(room)

    def test_dynamic_params_defaults_to_empty_dict(self):
        """If frontdoor didn't pass dynamic_params (e.g., a deployment with no
        parameter_schema), the parsed result has an empty dict — not None — so
        callers can `**dynamic_params` cleanly."""
        room = FakeRoom({"deployment_id": "dep_abc"})
        meta = VoiceSession.parse_room_metadata(room)
        assert meta["dynamic_params"] == {}

    def test_interaction_and_correlation_optional(self):
        """interaction_id + correlation_id are optional in parse_room_metadata —
        the worker should accept None and let downstream code error if it
        needs them (e.g., correlation_id is required to build RunnableConfig)."""
        room = FakeRoom({"deployment_id": "dep_abc"})
        meta = VoiceSession.parse_room_metadata(room)
        assert meta["interaction_id"] is None
        assert meta["correlation_id"] is None
