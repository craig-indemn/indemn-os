"""Integration tests: Message flow — claim, complete, fail, dead-letter.

Acceptance tests:
  #4 CHANGES COLLECTION (hash chain intact)
  #13 CASCADE DEPTH (circuit breaker)
  #19 KERNEL ENTITY CASCADE GUARD
"""

import pytest
from bson import ObjectId

from kernel.changes.collection import ChangeRecord
from kernel.changes.hash_chain import compute_hash
from kernel.message.mongodb_bus import MongoDBMessageBus
from kernel.message.schema import Message, MessageLog
from kernel_entities import Actor, Role
from kernel.watch.cache import load_watch_cache
from kernel_entities.role import WatchDefinition


@pytest.mark.asyncio
async def test_changes_collection_hash_chain(db, org_id, actor):
    """#4: Every mutation recorded, hash chain intact."""
    # Create two actors to generate change records
    a1 = Actor(
        org_id=org_id, name="Actor 1", type="human", status="provisioned",
    )
    await a1.save_tracked(actor_id=str(actor.id), method="create")

    a2 = Actor(
        org_id=org_id, name="Actor 2", type="human", status="provisioned",
    )
    await a2.save_tracked(actor_id=str(actor.id), method="create")

    # Verify chain
    records = await ChangeRecord.find(
        {"org_id": org_id}
    ).sort("timestamp").to_list()
    assert len(records) >= 2

    # Verify hash chain links (each record points to the previous)
    for i in range(1, len(records)):
        assert records[i].previous_hash == records[i - 1].current_hash

    # Verify hashes are non-empty
    for record in records:
        assert record.current_hash is not None
        assert len(record.current_hash) == 64  # SHA-256 hex


@pytest.mark.asyncio
async def test_message_claim_and_complete(db, org_id, actor):
    """Message claim (atomic) and complete (queue→log transfer)."""
    # Insert a message directly
    msg = Message(
        org_id=org_id,
        entity_type="TestEntity",
        entity_id=ObjectId(),
        event_type="created",
        target_role="test_role",
        correlation_id="test-corr-1",
        status="pending",
    )
    await msg.insert()

    bus = MongoDBMessageBus()

    # Claim
    claimed = await bus.claim_by_id(msg.id, actor.id)
    assert claimed is not None
    assert claimed.status == "processing"
    assert claimed.claimed_by == actor.id

    # Can't claim again
    claimed2 = await bus.claim_by_id(msg.id, ObjectId())
    assert claimed2 is None  # Already claimed

    # Complete — moves to log
    await bus.complete(msg.id, {"result": "success"})

    # Queue should be empty
    remaining = await Message.get(msg.id)
    assert remaining is None

    # Log should have it
    log_entries = await MessageLog.find(
        {"correlation_id": "test-corr-1"}
    ).to_list()
    assert len(log_entries) == 1
    assert log_entries[0].result == {"result": "success"}


@pytest.mark.asyncio
async def test_message_fail_and_dead_letter(db, org_id, actor):
    """Message fail → retry or dead-letter after max attempts."""
    msg = Message(
        org_id=org_id,
        entity_type="TestEntity",
        entity_id=ObjectId(),
        event_type="created",
        target_role="test_role",
        correlation_id="test-corr-2",
        status="pending",
        max_attempts=2,
    )
    await msg.insert()

    bus = MongoDBMessageBus()

    # Claim and fail — should go back to pending
    claimed = await bus.claim_by_id(msg.id, actor.id)
    await bus.fail(msg.id, "temporary error")
    msg_after = await Message.get(msg.id)
    assert msg_after.status == "pending"

    # Claim and fail again — should become dead_letter (attempt_count=2, max=2)
    claimed2 = await bus.claim_by_id(msg.id, actor.id)
    await bus.fail(msg.id, "another error")
    msg_dead = await Message.get(msg.id)
    assert msg_dead.status == "dead_letter"
