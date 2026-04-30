"""Auto-import all adapters so they register via register_adapter()."""

from kernel.integration.adapters import (
    google_workspace,  # noqa: F401
    outlook,  # noqa: F401
    slack,  # noqa: F401
    stripe_adapter,  # noqa: F401
)
