"""Request-scoped context variables.

Set by auth middleware on each request.
Read by OrgScopedCollection, save_tracked(), and entity operations.
"""

from contextvars import ContextVar
from typing import Optional

from bson import ObjectId

current_org_id: ContextVar[Optional[ObjectId]] = ContextVar("current_org_id", default=None)
current_actor_id: ContextVar[Optional[str]] = ContextVar("current_actor_id", default=None)
# The associate (or other actor) on whose behalf the authenticated session is
# acting. Set when an inside-trust-boundary caller (typically the runtime
# harness) asserts via X-Effective-Actor-Id header that this request runs for
# a specific associate, even though the auth token belongs to the runtime /
# Platform Admin. Without this, every associate's mutations look identical
# in the changes collection (Bug #22 forensics gap).
current_effective_actor_id: ContextVar[Optional[str]] = ContextVar(
    "current_effective_actor_id", default=None
)
current_correlation_id: ContextVar[Optional[str]] = ContextVar(
    "current_correlation_id", default=None
)
current_depth: ContextVar[int] = ContextVar("current_depth", default=0)
current_causation_message_id: ContextVar[Optional[str]] = ContextVar(
    "current_causation_message_id", default=None
)
