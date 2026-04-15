"""Pre-auth rate limiting interface.

Phase 1: interface definition only.
Phase 4: full implementation with sliding window counters.
"""

from typing import Protocol


class RateLimiter(Protocol):
    """Rate limiter interface. Implementations check request rate before auth."""

    async def check(self, key: str, limit: int, window_seconds: int) -> bool:
        """Check if the request is within rate limits.

        Returns True if allowed, False if rate limited.
        """
        ...

    async def record(self, key: str) -> None:
        """Record a request for rate tracking."""
        ...
