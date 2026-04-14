"""Base class for all entities — kernel and domain.

Every entity in the system inherits from BaseEntity. It provides:
- org_id scoping (multi-tenancy)
- version field (optimistic concurrency)
- created_at / updated_at timestamps
- State tracking for change detection (after_find captures loaded state)
- transition_to() for state machine enforcement
- save_tracked() — the ONLY save path
- find_scoped() / get_scoped() — org-aware queries
"""

from typing import Any, ClassVar, Optional

from beanie import Document
from bson import ObjectId
from datetime import datetime, timezone
from pydantic import Field


class BaseEntity(Document):
    """Base class for all entities — kernel and domain.
    Provides common fields and the save_tracked() method."""

    org_id: ObjectId
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: Optional[str] = None
    version: int = 1

    # Subclass configuration — set by kernel entity classes or factory.py
    _state_machine: ClassVar[Optional[dict[str, list[str]]]] = None
    _computed_fields: ClassVar[Optional[dict]] = None
    _activated_capabilities: ClassVar[list] = []
    _is_kernel_entity: ClassVar[bool] = False
    _state_field_name: ClassVar[Optional[str]] = None

    # Captured on load for change tracking
    _loaded_state: dict = {}

    model_config = {"arbitrary_types_allowed": True}

    class Settings:
        use_state_management = True
        is_root = True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    async def after_find(self):
        """Beanie hook: capture state on load for change tracking."""
        self._loaded_state = self.model_dump(by_alias=True)

    def transition_to(self, target_state: str, reason: Optional[str] = None):
        """Validate and set state transition. Does NOT save — caller must call save_tracked()."""
        from kernel.entity.state_machine import validate_and_apply_transition

        validate_and_apply_transition(self, target_state, reason)

    async def save_tracked(self, actor_id: str = None, **kwargs):
        """The ONLY save path. See kernel/entity/save.py for full implementation.
        Returns list of created messages (for optimistic dispatch in Phase 2)."""
        from kernel.entity.save import save_tracked_impl
        from kernel.context import current_actor_id

        _actor_id = actor_id or current_actor_id.get()
        return await save_tracked_impl(self, _actor_id, **kwargs)

    def _validate_pre_transition(self, target_state: str):
        """Override in kernel entity subclasses for business validation.
        Domain entities use capability-based validation instead."""
        pass

    @classmethod
    async def find_scoped(cls, filter_doc: dict = None, **kwargs):
        """Find with automatic org_id injection from context."""
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
