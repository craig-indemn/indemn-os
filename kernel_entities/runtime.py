"""Runtime — execution environment. Where associates actually run."""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import Field

from kernel.entity.base import BaseEntity
from kernel.entity.exposed import exposed


class Runtime(BaseEntity):
    """Execution environment — where associates actually run.

    One Runtime hosts many Associates. The Associate carries per-session config
    (skills, model, mode). The Runtime provides the environment.
    """

    name: str
    kind: Literal["realtime_chat", "realtime_voice", "realtime_sms", "async_worker"]
    framework: str  # deepagents, langchain, custom
    framework_version: str
    transport: Optional[str] = None
    transport_config: dict = Field(default_factory=dict)
    transport_secret_ref: Optional[str] = None
    llm_config: dict = Field(default_factory=dict)
    sandbox_config: dict = Field(default_factory=dict)
    deployment_image: str = ""
    deployment_platform: str = "railway"
    deployment_ref: Optional[str] = None
    capacity: dict = Field(
        default_factory=lambda: {"max_concurrent_sessions": None, "max_memory_mb": None}
    )
    status: Literal[
        "configured", "deploying", "active", "draining", "stopped", "error"
    ] = "configured"
    instances: list[dict] = Field(default_factory=list)

    _state_field_name = "status"
    _state_machine = {
        "configured": ["deploying"],
        "deploying": ["active", "error"],
        "active": ["draining", "error"],
        "draining": ["stopped"],
        "stopped": ["deploying"],
        "error": ["configured", "stopped"],
    }
    _is_kernel_entity = True

    @exposed
    async def register_instance(self):
        """Register a harness instance. Called at harness startup.

        Adds instance to the tracking list, transitions to active if
        this is the first instance (configured/deploying → active).
        """
        import uuid

        instance_id = str(uuid.uuid4())[:8]
        self.instances.append({
            "instance_id": instance_id,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        })
        # Auto-transition through lifecycle on first instance
        if self.status == "configured":
            self.transition_to("deploying")
            self.transition_to("active")
        elif self.status == "deploying":
            self.transition_to("active")
        await self.save_tracked(
            actor_id=f"system:runtime:{self.id}",
            method="register_instance",
        )
        return {"instance_id": instance_id, "runtime_id": str(self.id), "status": self.status}

    @exposed
    async def heartbeat(self):
        """Update heartbeat for the most recent instance.

        Heartbeat updates bypass audit logging (same pattern as Attention.heartbeat)
        to avoid noise in the changes collection.
        """
        if self.instances:
            self.instances[-1]["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
        await self.save_tracked(
            actor_id=f"system:heartbeat:{self.id}",
            method="heartbeat",
        )
        return {"status": "ok", "runtime_id": str(self.id)}

    class Settings:
        name = "runtimes"
        indexes = [[("org_id", 1), ("kind", 1), ("status", 1)]]
