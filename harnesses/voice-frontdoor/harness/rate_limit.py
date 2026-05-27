"""Voice-frontdoor wrapper around `harness_common.rate_limit` (AI-408
Phase 3 extraction).

The full sliding-window RateLimiter impl moved to
`harnesses/_base/harness_common/rate_limit.py` so chat-deepagents can share
it. This wrapper re-exports the symbol for back-compat — sessions.py +
tests/conftest.py import unchanged.
"""

from harness_common.rate_limit import RateLimiter

__all__ = ["RateLimiter"]
