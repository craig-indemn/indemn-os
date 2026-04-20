"""Integration tests: save_tracked() atomicity guarantee.

Protects the #1 architectural invariant from regression:
  Entity write + changes record + watch evaluation + message creation
  must be one atomic MongoDB transaction.

If any part fails, NONE of it commits.

A regression here was found during the 2026-04-15 audit (introduced by
shakeout commit 23fcf06 — accidental indentation shift moved changes
record + message emission outside the transaction). These tests prevent
that class of regression from happening silently.
"""

from unittest.mock import patch

import pytest

from kernel.changes.collection import ChangeRecord
from kernel.message.schema import Message
from kernel.watch.cache import load_watch_cache
from kernel_entities import Actor, Role
from kernel_entities.role import WatchDefinition


@pytest.mark.asyncio
async def test_atomicity_on_change_write_failure(db, org_id, actor):
    """If write_change_record() fails, the entity write must roll back.

    This is the canonical atomicity test. We mock write_change_record to raise,
    then verify NO Actor was inserted and NO change record exists for it.
    """
    # Snapshot: how many actors and change records exist before
    actors_before = await Actor.find({"org_id": org_id}).count()
    changes_before = await ChangeRecord.find({"org_id": org_id}).count()

    # Mock write_change_record to raise an exception
    with patch(
        "kernel.entity.save.write_change_record",
        side_effect=RuntimeError("Simulated change record failure"),
    ):
        new_actor = Actor(
            org_id=org_id,
            name="Should Not Persist",
            email="atomicity-test@example.com",
            type="human",
            status="provisioned",
        )

        with pytest.raises(RuntimeError, match="Simulated change record failure"):
            await new_actor.save_tracked(actor_id=str(actor.id), method="create")

    # Atomicity check: the actor must NOT have been inserted
    actors_after = await Actor.find({"org_id": org_id}).count()
    assert actors_after == actors_before, (
        f"ATOMICITY VIOLATED: Actor was inserted despite change record failure. "
        f"Before: {actors_before}, After: {actors_after}"
    )

    # The actor with that email must not exist
    leaked = await Actor.find_one({"email": "atomicity-test@example.com"})
    assert leaked is None, "ATOMICITY VIOLATED: Actor leaked into the database"

    # No new change records either
    changes_after = await ChangeRecord.find({"org_id": org_id}).count()
    assert changes_after == changes_before, (
        f"Change records were written despite the failure. "
        f"Before: {changes_before}, After: {changes_after}"
    )


@pytest.mark.asyncio
async def test_atomicity_on_message_emission_failure(db, org_id, actor):
    """If evaluate_watches_and_emit() fails, entity write AND change record must roll back.

    Set up a watch so emission actually runs, then mock it to raise.
    Verify entity, change record, and messages are all absent.
    """
    # Set up a role with a watch on Actor creation so emission runs
    watch_role = Role(
        org_id=org_id,
        name="atomicity_watcher",
        permissions={"read": ["Actor"], "write": []},
        watches=[WatchDefinition(entity_type="Actor", event="created")],
    )
    await watch_role.insert()
    await load_watch_cache()

    actors_before = await Actor.find({"org_id": org_id}).count()
    changes_before = await ChangeRecord.find({"org_id": org_id}).count()
    messages_before = await Message.find({"org_id": org_id}).count()

    # Mock evaluate_watches_and_emit to raise
    with patch(
        "kernel.entity.save.evaluate_watches_and_emit",
        side_effect=RuntimeError("Simulated emission failure"),
    ):
        new_actor = Actor(
            org_id=org_id,
            name="Emission Fail Test",
            email="emission-fail@example.com",
            type="human",
            status="provisioned",
        )

        with pytest.raises(RuntimeError, match="Simulated emission failure"):
            await new_actor.save_tracked(actor_id=str(actor.id), method="create")

    # ALL three must be absent: entity, change record, messages
    actors_after = await Actor.find({"org_id": org_id}).count()
    assert actors_after == actors_before, (
        "ATOMICITY VIOLATED: Actor was inserted despite emission failure"
    )

    leaked = await Actor.find_one({"email": "emission-fail@example.com"})
    assert leaked is None, "ATOMICITY VIOLATED: Actor leaked into the database"

    changes_after = await ChangeRecord.find({"org_id": org_id}).count()
    assert changes_after == changes_before, (
        "ATOMICITY VIOLATED: Change record was committed despite emission failure"
    )

    messages_after = await Message.find({"org_id": org_id}).count()
    assert messages_after == messages_before, (
        "ATOMICITY VIOLATED: Messages were committed despite emission failure"
    )


@pytest.mark.asyncio
async def test_version_restored_on_failure(db, org_id, actor):
    """Version must be restored when save_tracked fails.

    This protects retries — if version isn't restored, the next attempt
    would have the wrong expected_version and fail with VersionConflictError
    instead of succeeding.
    """
    new_actor = Actor(
        org_id=org_id,
        name="Version Restore Test",
        email="version-restore@example.com",
        type="human",
        status="provisioned",
    )

    initial_version = new_actor.version

    with patch(
        "kernel.entity.save.write_change_record",
        side_effect=RuntimeError("Forced failure"),
    ):
        with pytest.raises(RuntimeError):
            await new_actor.save_tracked(actor_id=str(actor.id), method="create")

    # Version must be restored to its pre-save value
    assert new_actor.version == initial_version, (
        f"Version not restored after failure. Expected {initial_version}, got {new_actor.version}"
    )
