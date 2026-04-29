"""Base classes for entities.

Two base classes:
- KernelBaseEntity(Document): for the 7 kernel entities. Uses Beanie for ODM.
- DomainBaseEntity(BaseModel): for dynamic domain entities. Uses Pydantic + Motor directly.

Both share the same interface: org_id, version, save_tracked(), find_scoped(), get_scoped(),
transition_to(). The API/CLI layer doesn't care which base class is used.
"""

import logging
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

from beanie import Document
from bson import ObjectId
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)

# --- Shared mixin for common behavior ---


class _EntityMixin:
    """Shared behavior for both kernel and domain entities."""

    # Subclass configuration — set by kernel entity classes or factory.py
    _state_machine: ClassVar[Optional[dict[str, list[str]]]] = None
    _computed_fields: ClassVar[Optional[dict]] = None
    _activated_capabilities: ClassVar[list] = []
    _is_kernel_entity: ClassVar[bool] = False
    _state_field_name: ClassVar[Optional[str]] = None

    # Captured on load for change tracking
    _loaded_state: dict = {}

    def transition_to(self, target_state: str, reason: Optional[str] = None):
        """Validate and set state transition. Does NOT save."""
        from kernel.entity.state_machine import validate_and_apply_transition

        validate_and_apply_transition(self, target_state, reason)

    async def save_tracked(self, actor_id: str = None, **kwargs):
        """The ONLY save path. Returns list of created messages."""
        from kernel.context import current_actor_id
        from kernel.entity.save import save_tracked_impl

        _actor_id = actor_id or current_actor_id.get()
        return await save_tracked_impl(self, _actor_id, **kwargs)

    def _validate_pre_transition(self, target_state: str):
        """Override in kernel entity subclasses for business validation."""
        pass


# --- Kernel entities: Beanie Document ---


class KernelBaseEntity(_EntityMixin, Document):
    """Base for the 7 kernel entities. Uses Beanie for full ODM support."""

    org_id: ObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: Optional[str] = None
    version: int = 1

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        use_state_management = True

    async def after_find(self):
        """Beanie hook: capture state on load for change tracking."""
        self._loaded_state = self.model_dump(by_alias=True)

    @classmethod
    def find_scoped(cls, filter_doc: dict = None, **kwargs):
        """Find with automatic org_id injection. Returns Beanie query (not async)."""
        from kernel.context import current_org_id

        filter_doc = filter_doc or {}
        org_id = current_org_id.get()
        if org_id:
            filter_doc["org_id"] = org_id
        return cls.find(filter_doc, **kwargs)

    @classmethod
    async def get_scoped(cls, entity_id, **kwargs):
        """Get by ID with org_id verification."""
        from kernel.context import current_org_id

        entity = await cls.get(entity_id, **kwargs)
        if entity and current_org_id.get():
            if entity.org_id != current_org_id.get():
                raise PermissionError("Cross-org access denied")
        return entity

    # get_motor_collection() is provided by Beanie's Document class


# --- Domain entities: Pydantic BaseModel + Motor ---


class DomainBaseEntity(_EntityMixin, BaseModel):
    """Base for dynamic domain entities. Uses Pydantic + Motor directly.

    No Beanie lazy model, no ExpressionField interference.
    Motor collection operations are explicit.
    """

    id: Optional[ObjectId] = Field(default=None, alias="_id")
    org_id: ObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: Optional[str] = None
    version: int = 1

    # Set by factory.py — the MongoDB collection name
    _collection_name: ClassVar[str] = ""
    _db_ref: ClassVar[Any] = None  # Set during init_database

    model_config = {"arbitrary_types_allowed": True, "populate_by_name": True}

    @classmethod
    def get_motor_collection(cls):
        """Get the Motor collection for this entity type."""
        return cls._db_ref[cls._collection_name]

    @classmethod
    def find_scoped(cls, filter_doc: dict = None, **kwargs):
        """Find with automatic org_id injection. Returns a MotorCursor-like wrapper."""
        from kernel.context import current_org_id

        filter_doc = filter_doc or {}
        org_id = current_org_id.get()
        if org_id:
            filter_doc["org_id"] = org_id
        return _DomainQuery(cls, filter_doc)

    @classmethod
    async def find_one(cls, filter_doc: dict, **kwargs):
        """Find a single document matching the filter."""
        doc = await cls._db_ref[cls._collection_name].find_one(filter_doc)
        if doc is None:
            return None
        entity = cls(**doc)
        entity._loaded_state = entity.model_dump(by_alias=True)
        return entity

    @classmethod
    async def get(cls, entity_id, **kwargs):
        """Get by ID from MongoDB."""
        if entity_id is None:
            return None
        doc = await cls._db_ref[cls._collection_name].find_one({"_id": ObjectId(str(entity_id))})
        if doc is None:
            return None
        entity = cls(**doc)
        entity._loaded_state = entity.model_dump(by_alias=True)
        return entity

    @classmethod
    async def get_scoped(cls, entity_id, **kwargs):
        """Get by ID with org_id verification."""
        from kernel.context import current_org_id

        entity = await cls.get(entity_id, **kwargs)
        if entity and current_org_id.get():
            if entity.org_id != current_org_id.get():
                raise PermissionError("Cross-org access denied")
        return entity

    async def insert(self, session=None):
        """Insert into MongoDB."""
        if self.id is None:
            self.id = ObjectId()
        data = self.model_dump(by_alias=True)
        coll = self.__class__._db_ref[self.__class__._collection_name]
        await coll.insert_one(data, session=session)
        return self


class _DomainQuery:
    """Simple query builder for domain entities (replaces Beanie's query interface)."""

    def __init__(self, cls, filter_doc: dict):
        self._cls = cls
        self._filter = filter_doc
        self._sort_key = None
        self._skip_n = 0
        self._limit_n = 0

    def sort(self, key: str):
        self._sort_key = key
        return self

    def skip(self, n: int):
        self._skip_n = n
        return self

    def limit(self, n: int):
        self._limit_n = n
        return self

    async def to_list(self, length: int = None, skip_invalid: bool = False):
        """Materialize the query into a list of entity instances.

        Args:
            length: cap on number of docs to fetch (Motor passthrough). Usually
                bounded already via `.limit()`; defaults to 1000 if neither is set.
            skip_invalid: when True, individual docs that fail Pydantic
                construction are skipped with a warning log naming the entity
                type and bad doc `_id`. When False (default — the historical
                strict behavior), one bad doc raises and the entire list call
                fails with a Pydantic ValidationError.

        Bug #37 (2026-04-29 Armadillo trace): a single Email document with
        `company={"name":"Oneleet"}` (a stringified dict left over from a
        pre-Bug-#9-fix associate run) made `_DomainQuery.to_list()` raise
        `Input should be an instance of ObjectId`, which propagated through
        the user-facing list endpoint and returned HTTP 400 for every caller.
        One bad row poisoned the whole endpoint. The list route now opts in
        to `skip_invalid=True` so the API stays usable while the malformed
        rows are surfaced via logs for cleanup. Migrations / audit code that
        need to know about every doc keep the strict default.
        """
        coll = self._cls._db_ref[self._cls._collection_name]
        cursor = coll.find(self._filter)
        if self._sort_key:
            direction = -1 if self._sort_key.startswith("-") else 1
            field = self._sort_key.lstrip("-")
            cursor = cursor.sort(field, direction)
        if self._skip_n:
            cursor = cursor.skip(self._skip_n)
        limit = self._limit_n or length or 0
        if limit:
            cursor = cursor.limit(limit)
        docs = await cursor.to_list(length=limit or 1000)
        entities = []
        for doc in docs:
            try:
                entity = self._cls(**doc)
            except Exception as e:
                if skip_invalid:
                    _logger.warning(
                        "Skipping malformed %s document _id=%s in to_list "
                        "(skip_invalid=True): %s",
                        self._cls.__name__,
                        doc.get("_id"),
                        e,
                    )
                    continue
                raise
            entity._loaded_state = entity.model_dump(by_alias=True)
            entities.append(entity)
        return entities

    async def count(self):
        coll = self._cls._db_ref[self._cls._collection_name]
        return await coll.count_documents(self._filter)


# Backward compat alias — kernel entities import this
BaseEntity = KernelBaseEntity
