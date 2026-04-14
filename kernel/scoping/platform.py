"""PlatformCollection — bypasses org scoping for platform admin operations.

Used by platform admin sessions working across orgs (build, debug, incident).
Every operation is audited. Application code cannot accidentally instantiate this —
it requires an explicit acting_actor_id parameter.
"""

import logging

from motor.motor_asyncio import AsyncIOMotorCollection

logger = logging.getLogger(__name__)


class PlatformCollection:
    """Bypasses org scoping for platform admin operations.
    Requires explicit acting_actor_id. Every operation is audited."""

    def __init__(self, collection: AsyncIOMotorCollection, acting_actor_id: str):
        self._collection = collection
        self._acting_actor_id = acting_actor_id

    def _audit(self, operation: str, filter_doc: dict = None):
        logger.info(
            "Platform access: actor=%s op=%s filter=%s collection=%s",
            self._acting_actor_id,
            operation,
            filter_doc,
            self._collection.name,
        )

    async def find_one(self, filter_doc=None, *args, **kwargs):
        self._audit("find_one", filter_doc)
        return await self._collection.find_one(filter_doc or {}, *args, **kwargs)

    def find(self, filter_doc=None, *args, **kwargs):
        self._audit("find", filter_doc)
        return self._collection.find(filter_doc or {}, *args, **kwargs)

    async def insert_one(self, doc, *args, **kwargs):
        self._audit("insert_one")
        return await self._collection.insert_one(doc, *args, **kwargs)

    async def update_one(self, filter_doc, update, *args, **kwargs):
        self._audit("update_one", filter_doc)
        return await self._collection.update_one(filter_doc, update, *args, **kwargs)

    async def update_many(self, filter_doc, update, *args, **kwargs):
        self._audit("update_many", filter_doc)
        return await self._collection.update_many(filter_doc, update, *args, **kwargs)

    async def delete_one(self, filter_doc, *args, **kwargs):
        self._audit("delete_one", filter_doc)
        return await self._collection.delete_one(filter_doc, *args, **kwargs)

    async def count_documents(self, filter_doc=None, *args, **kwargs):
        self._audit("count_documents", filter_doc)
        return await self._collection.count_documents(filter_doc or {}, *args, **kwargs)

    async def aggregate(self, pipeline, *args, **kwargs):
        self._audit("aggregate")
        return self._collection.aggregate(pipeline, *args, **kwargs)
