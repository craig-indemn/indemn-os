"""Runtime — execution environment. Where associates actually run."""

from typing import Literal, Optional

from pydantic import Field

from kernel.entity.base import BaseEntity


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

    class Settings:
        name = "runtimes"
        indexes = [[("org_id", 1), ("kind", 1), ("status", 1)]]
