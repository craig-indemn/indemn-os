"""Integration tests for associate processing — the Phase 2 core loop.

Tests the full message lifecycle: message creation → claim → process → complete/fail.
Tests credential resolution priority chain.
Tests scheduled task creation.
"""

import pytest
import pytest_asyncio
from bson import ObjectId
from datetime import datetime, timezone

from kernel.context import current_org_id, current_actor_id
from kernel.message.mongodb_bus import MongoDBMessageBus
from kernel.message.schema import Message, MessageLog
from kernel.skill.integrity import compute_content_hash
from kernel.skill.schema import Skill
from kernel.temporal.activities import (
    PermanentProcessingError,
    SkillTamperError,
    _extract_data_from_args,
    _load_skills,
    _parse_skill_steps,
    claim_message,
    complete_message,
    fail_message,
    load_entity_context,
)
from kernel_entities.actor import Actor
from kernel_entities.integration import Integration
from kernel_entities.role import Role


class TestMessageLifecycle:
    """Test claim → complete → log flow against Atlas."""

    @pytest.mark.asyncio
    async def test_claim_message(self, db, org_id, actor):
        """Claiming a pending message sets it to processing."""
        msg = Message(
            org_id=org_id,
            entity_type="TestEntity",
            entity_id=ObjectId(),
            event_type="created",
            target_role="admin",
            correlation_id="test-corr-1",
            status="pending",
        )
        await msg.insert()

        bus = MongoDBMessageBus()
        claimed = await bus.claim_by_id(msg.id, actor.id)
        assert claimed is not None
        assert claimed.status == "processing"
        assert claimed.claimed_by == actor.id

    @pytest.mark.asyncio
    async def test_claim_already_claimed(self, db, org_id, actor):
        """Double-claim returns None."""
        msg = Message(
            org_id=org_id,
            entity_type="TestEntity",
            entity_id=ObjectId(),
            event_type="created",
            target_role="admin",
            correlation_id="test-corr-2",
            status="processing",
            claimed_by=ObjectId(),  # Claimed by someone else
        )
        await msg.insert()

        bus = MongoDBMessageBus()
        result = await bus.claim_by_id(msg.id, actor.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_complete_moves_to_log(self, db, org_id, actor):
        """Completing a message moves it from queue to log."""
        msg = Message(
            org_id=org_id,
            entity_type="TestEntity",
            entity_id=ObjectId(),
            event_type="created",
            target_role="admin",
            correlation_id="test-corr-3",
            status="processing",
            claimed_by=actor.id,
        )
        await msg.insert()

        bus = MongoDBMessageBus()
        await bus.complete(msg.id, {"status": "done"})

        # Should be gone from queue
        in_queue = await Message.get(msg.id)
        assert in_queue is None

        # Should be in log
        log = await MessageLog.find_one({"correlation_id": "test-corr-3"})
        assert log is not None
        assert log.result == {"status": "done"}

    @pytest.mark.asyncio
    async def test_fail_returns_to_pending(self, db, org_id, actor):
        """Failing a message under max_attempts returns to pending."""
        msg = Message(
            org_id=org_id,
            entity_type="TestEntity",
            entity_id=ObjectId(),
            event_type="created",
            target_role="admin",
            correlation_id="test-corr-4",
            status="processing",
            claimed_by=actor.id,
            attempt_count=1,
            max_attempts=3,
        )
        await msg.insert()

        bus = MongoDBMessageBus()
        await bus.fail(msg.id, "transient error")

        updated = await Message.get(msg.id)
        assert updated.status == "pending"
        assert updated.claimed_by is None
        assert updated.last_error == "transient error"

    @pytest.mark.asyncio
    async def test_fail_dead_letters(self, db, org_id, actor):
        """Failing a message at max_attempts moves to dead_letter."""
        msg = Message(
            org_id=org_id,
            entity_type="TestEntity",
            entity_id=ObjectId(),
            event_type="created",
            target_role="admin",
            correlation_id="test-corr-5",
            status="processing",
            claimed_by=actor.id,
            attempt_count=3,
            max_attempts=3,
        )
        await msg.insert()

        bus = MongoDBMessageBus()
        await bus.fail(msg.id, "permanent error")

        updated = await Message.get(msg.id)
        assert updated.status == "dead_letter"
        assert updated.last_error == "permanent error"


class TestSkillLoading:
    """Test skill loading with integrity verification."""

    @pytest.mark.asyncio
    async def test_load_active_skill(self, db, org_id):
        """Loading an active skill with valid hash succeeds."""
        content = "# My Skill\n1. `indemn test run`"
        skill = Skill(
            name="test-skill",
            type="associate",
            content=content,
            content_hash=compute_content_hash(content),
            status="active",
        )
        await skill.insert()

        result = await _load_skills(["test-skill"])
        assert "indemn test run" in result

    @pytest.mark.asyncio
    async def test_tampered_skill_raises(self, db, org_id):
        """Loading a skill with mismatched hash raises SkillTamperError."""
        content = "# My Skill\n1. `indemn test run`"
        skill = Skill(
            name="tampered-skill",
            type="associate",
            content=content,
            content_hash="badhash",
            status="active",
        )
        await skill.insert()

        with pytest.raises(SkillTamperError, match="failed integrity check"):
            await _load_skills(["tampered-skill"])

    @pytest.mark.asyncio
    async def test_missing_skill_skipped(self, db, org_id):
        """Missing skills are silently skipped."""
        result = await _load_skills(["nonexistent-skill"])
        assert result == ""

    @pytest.mark.asyncio
    async def test_multiple_skills_concatenated(self, db, org_id):
        """Multiple skills are joined with dividers."""
        for i in range(2):
            content = f"# Skill {i}"
            skill = Skill(
                name=f"multi-skill-{i}",
                type="associate",
                content=content,
                content_hash=compute_content_hash(content),
                status="active",
            )
            await skill.insert()

        result = await _load_skills(["multi-skill-0", "multi-skill-1"])
        assert "# Skill 0" in result
        assert "# Skill 1" in result
        assert "---" in result


class TestCredentialResolution:
    """Test the actor → owner → org resolution chain."""

    @pytest.mark.asyncio
    async def test_actor_level_wins(self, db, org_id, actor):
        """Actor's own integration takes priority."""
        # Create actor-level integration
        integration = Integration(
            org_id=org_id,
            name="actor-email",
            owner_type="actor",
            owner_id=actor.id,
            system_type="email",
            provider="outlook",
            provider_version="v2",
            status="active",
            secret_ref="test/actor/email",
        )
        await integration.insert()

        from kernel.integration.resolver import resolve_integration

        result = await resolve_integration("email", actor_id=actor.id, org_id=org_id)
        assert result.id == integration.id
        assert result.owner_type == "actor"

    @pytest.mark.asyncio
    async def test_org_level_with_role_check(self, db, org_id, actor):
        """Org-level integration requires matching role."""
        # Create org-level integration accessible to 'admin' role
        integration = Integration(
            org_id=org_id,
            name="org-payment",
            owner_type="org",
            owner_id=org_id,
            system_type="payment",
            provider="stripe",
            provider_version="v1",
            status="active",
            secret_ref="test/org/payment",
            access={"roles": ["admin"]},
        )
        await integration.insert()

        from kernel.integration.resolver import resolve_integration

        result = await resolve_integration("payment", actor_id=actor.id, org_id=org_id)
        assert result.id == integration.id

    @pytest.mark.asyncio
    async def test_no_integration_raises(self, db, org_id, actor):
        """Missing integration raises AdapterNotFoundError."""
        from kernel.integration.adapter import AdapterNotFoundError
        from kernel.integration.resolver import resolve_integration

        with pytest.raises(AdapterNotFoundError):
            await resolve_integration("nonexistent_system", actor_id=actor.id, org_id=org_id)

    @pytest.mark.asyncio
    async def test_owner_level_fallback(self, db, org_id, actor):
        """Associate falls back to owner's integration."""
        # Create an owner actor
        owner = Actor(
            org_id=org_id,
            name="Owner",
            type="human",
            status="active",
            role_ids=[],
        )
        await owner.insert()

        # Create associate owned by the owner
        associate = Actor(
            org_id=org_id,
            name="Associate",
            type="associate",
            status="active",
            role_ids=[],
            owner_actor_id=owner.id,
        )
        await associate.insert()

        # Create owner's integration
        integration = Integration(
            org_id=org_id,
            name="owner-email",
            owner_type="actor",
            owner_id=owner.id,
            system_type="email",
            provider="outlook",
            provider_version="v2",
            status="active",
            secret_ref="test/owner/email",
        )
        await integration.insert()

        from kernel.integration.resolver import resolve_integration

        result = await resolve_integration("email", actor_id=associate.id, org_id=org_id)
        assert result.owner_id == owner.id


class TestArgParsing:
    def test_extract_key_value_pairs(self):
        args = ["EMAIL-001", "--to", "admin", "--auto", "--priority", "high"]
        data = _extract_data_from_args(args)
        assert data["to"] == "admin"
        assert data["priority"] == "high"
        assert "auto" not in data  # --auto is excluded

    def test_flag_without_value(self):
        args = ["ID-1", "--force"]
        data = _extract_data_from_args(args)
        assert data["force"] is True

    def test_empty_args(self):
        data = _extract_data_from_args([])
        assert data == {}
