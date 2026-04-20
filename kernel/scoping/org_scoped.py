"""OrgScopedCollection — wraps Motor collection to always inject org_id.

Used for any raw MongoDB queries that bypass Beanie (aggregations, raw updates,
bulk operations). Application code should never use raw Motor collections directly.

Layer 1 of org isolation. Layer 2 is BaseEntity.find_scoped() / get_scoped().
"""

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorCollection

from kernel.context import current_org_id


class OrgScopedCollection:
    """Wraps a Motor collection to always inject org_id."""

    def __init__(self, collection: AsyncIOMotorCollection, org_id: ObjectId = None):
        self._collection = collection
        self._org_id = org_id or current_org_id.get()

    def _inject(self, filter_doc: dict = None) -> dict:
        f = dict(filter_doc or {})
        f["org_id"] = self._org_id
        return f

    async def find_one(self, filter_doc=None, *args, **kwargs):
        return await self._collection.find_one(self._inject(filter_doc), *args, **kwargs)

    def find(self, filter_doc=None, *args, **kwargs):
        return self._collection.find(self._inject(filter_doc), *args, **kwargs)

    async def insert_one(self, doc, *args, **kwargs):
        doc["org_id"] = self._org_id
        return await self._collection.insert_one(doc, *args, **kwargs)

    async def update_one(self, filter_doc, update, *args, **kwargs):
        return await self._collection.update_one(self._inject(filter_doc), update, *args, **kwargs)

    async def update_many(self, filter_doc, update, *args, **kwargs):
        return await self._collection.update_many(self._inject(filter_doc), update, *args, **kwargs)

    async def delete_one(self, filter_doc, *args, **kwargs):
        return await self._collection.delete_one(self._inject(filter_doc), *args, **kwargs)

    async def count_documents(self, filter_doc=None, *args, **kwargs):
        return await self._collection.count_documents(self._inject(filter_doc), *args, **kwargs)

    async def aggregate(self, pipeline, *args, **kwargs):
        pipeline = [{"$match": {"org_id": self._org_id}}] + list(pipeline)
        return self._collection.aggregate(pipeline, *args, **kwargs)
