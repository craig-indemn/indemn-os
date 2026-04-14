"""Request-scoped context variables.

Set by auth middleware on each request.
Read by OrgScopedCollection, save_tracked(), and entity operations.
"""

from contextvars import ContextVar
from typing import Optional

from bson import ObjectId

current_org_id: ContextVar[Optional[ObjectId]] = ContextVar("current_org_id", default=None)
current_actor_id: ContextVar[Optional[str]] = ContextVar("current_actor_id", default=None)
current_correlation_id: ContextVar[Optional[str]] = ContextVar(
    "current_correlation_id", default=None
)
current_depth: ContextVar[int] = ContextVar("current_depth", default=0)
