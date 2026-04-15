"""Adapter registry — maps provider:version to adapter class."""

from kernel.integration.adapter import Adapter, AdapterNotFoundError

ADAPTER_REGISTRY: dict[str, type[Adapter]] = {}


def register_adapter(provider: str, version: str, adapter_cls: type[Adapter]):
    """Register an adapter class for a provider:version key."""
    key = f"{provider}:{version}"
    ADAPTER_REGISTRY[key] = adapter_cls


def get_adapter_class(provider: str, version: str) -> type[Adapter]:
    """Get adapter class by provider:version. Raises AdapterNotFoundError."""
    key = f"{provider}:{version}"
    cls = ADAPTER_REGISTRY.get(key)
    if not cls:
        raise AdapterNotFoundError(f"No adapter for {key}")
    return cls
