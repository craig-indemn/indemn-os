"""MongoDB implementation of the MessageBus interface.

Messages live in message_queue (hot). Completed messages move to message_log (cold).
Claiming is atomic via findOneAndUpdate. Completion is transactional (insert log + delete queue).
"""

from datetime import datetime, timedelta, timezone

from bson import ObjectId

from kernel.message.schema import Message, MessageLog


class MongoDBMessageBus:
    """MongoDB implementation of the MessageBus interface."""

    async def publish(self, message: Message, session=None) -> None:
        """Write a message to the queue within a transaction."""
        await message.insert(session=session)

    async def claim_by_id(self, message_id: ObjectId, actor_id: ObjectId) -> Message:
        """Claim a specific message by ID. Used by Temporal activities."""
        now = datetime.now(timezone.utc)
        result = await Message.get_motor_collection().find_one_and_update(
            {
                "_id": message_id,
                "$or": [
                    {"status": "pending"},
                    {"status": "processing", "visibility_timeout": {"$lt": now}},
                ],
            },
            {
                "$set": {
                    "status": "processing",
                    "claimed_by": actor_id,
                    "claimed_at": now,
                    "visibility_timeout": now + timedelta(minutes=5),
                },
                "$inc": {"attempt_count": 1},
            },
            return_document=True,
        )
        return Message(**result) if result else None

    async def claim(self, role: str, org_id: ObjectId, actor_id: ObjectId) -> Message:
        """Atomic claim via findOneAndUpdate. Highest priority, oldest first."""
        now = datetime.now(timezone.utc)
        result = await Message.get_motor_collection().find_one_and_update(
            {
                "org_id": org_id,
                "target_role": role,
                "$and": [
                    {
                        "$or": [
                            {"status": "pending"},
                            {"status": "processing", "visibility_timeout": {"$lt": now}},
                        ]
                    },
                    {
                        "$or": [
                            {"target_actor_id": None},
                            {"target_actor_id": actor_id},
                        ]
                    },
                ],
            },
            {
                "$set": {
                    "status": "processing",
                    "claimed_by": actor_id,
                    "claimed_at": now,
                    "visibility_timeout": now + timedelta(minutes=5),
                },
                "$inc": {"attempt_count": 1},
            },
            sort=[("priority", -1), ("created_at", 1)],
            return_document=True,
        )
        return Message(**result) if result else None

    async def complete(self, message_id: ObjectId, result: dict) -> None:
        """Move message from queue to log. In a transaction."""
        message = await Message.get(message_id)
        if not message:
            return

        client = Message.get_motor_collection().database.client
        async with await client.start_session() as session:
            async with session.start_transaction():
                # Insert into log
                log_entry = MessageLog(
                    **message.model_dump(exclude={"id", "revision_id"}),
                    result=result,
                    completed_at=datetime.now(timezone.utc),
                )
                await log_entry.insert(session=session)
                # Delete from queue
                await Message.get_motor_collection().delete_one(
                    {"_id": message_id}, session=session
                )

    async def fail(self, message_id: ObjectId, error: str) -> None:
        """Return message to queue or move to dead_letter."""
        message = await Message.get(message_id)
        if not message:
            return
        if message.attempt_count >= message.max_attempts:
            new_status = "dead_letter"
        else:
            new_status = "pending"
        update = {
            "$set": {
                "status": new_status,
                "last_error": error,
            }
        }
        if new_status == "pending":
            update["$set"]["claimed_by"] = None
            update["$set"]["visibility_timeout"] = None
        await Message.get_motor_collection().update_one(
            {"_id": message_id}, update
        )
