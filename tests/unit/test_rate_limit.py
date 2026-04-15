"""Unit tests for rate limit key generation."""

from kernel.auth.rate_limit import _make_key


class TestRateLimitKey:
    def test_key_is_deterministic(self):
        k1 = _make_key("1.2.3.4", "test@example.com")
        k2 = _make_key("1.2.3.4", "test@example.com")
        assert k1 == k2

    def test_different_ip_different_key(self):
        k1 = _make_key("1.2.3.4", "test@example.com")
        k2 = _make_key("5.6.7.8", "test@example.com")
        assert k1 != k2

    def test_different_email_different_key(self):
        k1 = _make_key("1.2.3.4", "a@example.com")
        k2 = _make_key("1.2.3.4", "b@example.com")
        assert k1 != k2

    def test_key_is_hex_string(self):
        key = _make_key("1.2.3.4", "test@example.com")
        assert len(key) == 64  # SHA-256 hex digest
        int(key, 16)  # Should be valid hex
