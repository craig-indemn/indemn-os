"""Unit tests for pipeline aggregation logic."""

from kernel.capability.aggregations import queue_depth, state_distribution


class TestAggregationFunctions:
    def test_state_distribution_is_async(self):
        """state_distribution is an async function."""
        import asyncio
        assert asyncio.iscoroutinefunction(state_distribution)

    def test_queue_depth_is_async(self):
        """queue_depth is an async function."""
        import asyncio
        assert asyncio.iscoroutinefunction(queue_depth)
