"""Auto-import all adapters so they register via register_adapter()."""

from kernel.integration.adapters import (
    outlook,  # noqa: F401
    stripe_adapter,  # noqa: F401
)
