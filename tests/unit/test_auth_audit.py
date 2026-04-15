"""Unit tests for auth audit event types."""

from kernel.auth.audit import AUTH_EVENT_TYPES


class TestAuthEventTypes:
    def test_all_event_types_have_auth_prefix(self):
        for event_type in AUTH_EVENT_TYPES:
            assert event_type.startswith("auth."), f"{event_type} missing auth. prefix"

    def test_expected_events_present(self):
        expected = {
            "auth.login_attempt",
            "auth.login_success",
            "auth.login_failure",
            "auth.mfa_verified",
            "auth.mfa_reset",
            "auth.password_changed",
            "auth.platform_admin_access",
            "auth.brute_force_lockout",
            "auth.session_revoked",
            "auth.role_granted",
            "auth.role_revoked",
        }
        actual = set(AUTH_EVENT_TYPES)
        assert expected.issubset(actual), f"Missing: {expected - actual}"

    def test_no_duplicate_events(self):
        assert len(AUTH_EVENT_TYPES) == len(set(AUTH_EVENT_TYPES))
