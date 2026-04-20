"""Tests for adapter base class, registry, and error hierarchy."""

import pytest

from kernel.integration.adapter import (
    Adapter,
    AdapterAuthError,
    AdapterError,
    AdapterNotFoundError,
    AdapterRateLimitError,
    AdapterTimeoutError,
    AdapterValidationError,
)
from kernel.integration.registry import (
    ADAPTER_REGISTRY,
    get_adapter_class,
    register_adapter,
)


class TestAdapterErrorHierarchy:
    def test_all_errors_inherit_adapter_error(self):
        assert issubclass(AdapterAuthError, AdapterError)
        assert issubclass(AdapterRateLimitError, AdapterError)
        assert issubclass(AdapterTimeoutError, AdapterError)
        assert issubclass(AdapterNotFoundError, AdapterError)
        assert issubclass(AdapterValidationError, AdapterError)

    def test_rate_limit_has_retry_after(self):
        err = AdapterRateLimitError("too many requests", retry_after=60)
        assert err.retry_after == 60

    def test_rate_limit_retry_after_defaults_none(self):
        err = AdapterRateLimitError("too many requests")
        assert err.retry_after is None


class TestAdapterBase:
    def test_not_implemented_methods(self):
        class DummyAdapter(Adapter):
            pass

        adapter = DummyAdapter(config={}, credentials={})
        with pytest.raises(NotImplementedError):
            import asyncio

            asyncio.get_event_loop().run_until_complete(adapter.fetch())

    def test_needs_token_refresh_default_false(self):
        class DummyAdapter(Adapter):
            pass

        adapter = DummyAdapter(config={}, credentials={})
        assert adapter.needs_token_refresh() is False


class TestAdapterRegistry:
    def test_register_and_get(self):
        class TestAdapter(Adapter):
            pass

        register_adapter("test_provider", "v1", TestAdapter)
        assert get_adapter_class("test_provider", "v1") is TestAdapter
        # Cleanup
        ADAPTER_REGISTRY.pop("test_provider:v1", None)

    def test_get_unknown_raises(self):
        with pytest.raises(AdapterNotFoundError):
            get_adapter_class("nonexistent", "v1")
