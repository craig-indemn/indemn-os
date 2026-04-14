"""Capability registration and dispatch.

Kernel capabilities are reusable operations that any entity can activate.
Registered at import time. Dispatched by name from entity methods.
"""

from typing import Callable

CAPABILITY_REGISTRY: dict[str, Callable] = {}


def register_capability(name: str, func: Callable):
    """Register a capability function by name."""
    CAPABILITY_REGISTRY[name] = func


def get_capability(name: str) -> Callable:
    """Get a registered capability by name. Raises if not found."""
    func = CAPABILITY_REGISTRY.get(name)
    if not func:
        raise ValueError(f"Unknown capability: {name}")
    return func
