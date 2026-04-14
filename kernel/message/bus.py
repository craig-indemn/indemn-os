"""MessageBus abstraction interface.

MongoDB implementation now, swappable to RabbitMQ later (additive, not replacing).
Application code publishes/claims through this interface.
"""

from typing import Optional, Protocol

from bson import ObjectId


class MessageBus(Protocol):
    """Abstract interface for message publishing and claiming."""

    async def publish(self, message, session=None) -> None: ...
    async def claim_by_id(self, message_id: ObjectId, actor_id: ObjectId) -> Optional[object]: ...
    async def claim(self, role: str, org_id: ObjectId, actor_id: ObjectId) -> Optional[object]: ...
    async def complete(self, message_id: ObjectId, result: dict) -> None: ...
    async def fail(self, message_id: ObjectId, error: str) -> None: ...
