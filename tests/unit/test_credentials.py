"""Tests for credential caching logic (without hitting AWS)."""

import time

from kernel.integration.credentials import CACHE_TTL, _cache, invalidate_cached_credentials


class TestCredentialCache:
    def setup_method(self):
        _cache.clear()

    def test_invalidate_removes_entry(self):
        _cache["test/secret"] = ({"key": "value"}, time.time())
        invalidate_cached_credentials("test/secret")
        assert "test/secret" not in _cache

    def test_invalidate_nonexistent_is_noop(self):
        invalidate_cached_credentials("does/not/exist")
        # Should not raise

    def test_cache_structure(self):
        now = time.time()
        _cache["test/ref"] = ({"api_key": "abc"}, now)
        creds, cached_at = _cache["test/ref"]
        assert creds == {"api_key": "abc"}
        assert cached_at == now

    def test_ttl_constant(self):
        assert CACHE_TTL == 300  # 5 minutes
