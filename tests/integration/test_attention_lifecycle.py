"""Integration tests for Phase 5 attention lifecycle."""

from datetime import datetime, timedelta, timezone

from bson import ObjectId

from kernel.queue_processor import cleanup_expired_attentions
from kernel_entities.attention import Attention


class TestAttentionTTL:
    """Test TTL cleanup sweep for expired Attentions."""

    async def test_expired_attention_transitions(self, db, org_id, actor):
        """Attention past TTL should be transitioned to expired by sweep."""
        attention = Attention(
            org_id=org_id,
            actor_id=actor.id,
            target_entity={"type": "Interaction", "id": ObjectId()},
            purpose="real_time_session",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            status="active",
        )
        await attention.insert()

        await cleanup_expired_attentions()

        reloaded = await Attention.get(attention.id)
        assert reloaded.status == "expired"

    async def test_active_attention_not_expired(self, db, org_id, actor):
        """Attention within TTL should not be touched."""
        attention = Attention(
            org_id=org_id,
            actor_id=actor.id,
            target_entity={"type": "Interaction", "id": ObjectId()},
            purpose="real_time_session",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            status="active",
        )
        await attention.insert()

        await cleanup_expired_attentions()

        reloaded = await Attention.get(attention.id)
        assert reloaded.status == "active"

    async def test_closed_attention_not_affected(self, db, org_id, actor):
        """Already closed Attention should not be touched by sweep."""
        attention = Attention(
            org_id=org_id,
            actor_id=actor.id,
            target_entity={"type": "Interaction", "id": ObjectId()},
            purpose="review",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            status="closed",
        )
        await attention.insert()

        await cleanup_expired_attentions()

        reloaded = await Attention.get(attention.id)
        assert reloaded.status == "closed"
