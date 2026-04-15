"""Tests for Outlook adapter — field mapping and token refresh detection."""

from datetime import datetime, timedelta, timezone

from kernel.integration.adapters.outlook import OutlookAdapter


class TestOutlookFieldMapping:
    def setup_method(self):
        self.adapter = OutlookAdapter(
            config={"tenant_id": "test", "client_id": "test"},
            credentials={"access_token": "fake", "client_secret": "s", "refresh_token": "r"},
        )

    def test_map_to_os(self):
        outlook_msg = {
            "id": "msg-1",
            "from": {"emailAddress": {"address": "sender@example.com"}},
            "toRecipients": [
                {"emailAddress": {"address": "to1@example.com"}},
                {"emailAddress": {"address": "to2@example.com"}},
            ],
            "subject": "Test Subject",
            "body": {"content": "<p>Hello</p>"},
            "receivedDateTime": "2026-01-15T10:00:00Z",
            "conversationId": "conv-123",
            "hasAttachments": True,
        }
        result = self.adapter._map_to_os(outlook_msg)
        assert result["external_id"] == "msg-1"
        assert result["from_address"] == "sender@example.com"
        assert len(result["to_addresses"]) == 2
        assert result["subject"] == "Test Subject"
        assert result["has_attachments"] is True

    def test_map_from_os(self):
        email_data = {
            "to_addresses": ["to@example.com"],
            "subject": "Reply",
            "body": "<p>Thanks</p>",
        }
        result = self.adapter._map_from_os(email_data)
        assert len(result["toRecipients"]) == 1
        assert result["toRecipients"][0]["emailAddress"]["address"] == "to@example.com"
        assert result["body"]["contentType"] == "HTML"


class TestOutlookTokenRefresh:
    def test_needs_refresh_when_expired(self):
        adapter = OutlookAdapter(
            config={"tenant_id": "t", "client_id": "c"},
            credentials={
                "access_token": "tok",
                "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                "client_secret": "s",
                "refresh_token": "r",
            },
        )
        assert adapter.needs_token_refresh() is True

    def test_no_refresh_when_valid(self):
        adapter = OutlookAdapter(
            config={"tenant_id": "t", "client_id": "c"},
            credentials={
                "access_token": "tok",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "client_secret": "s",
                "refresh_token": "r",
            },
        )
        assert adapter.needs_token_refresh() is False

    def test_no_refresh_when_no_expires_at(self):
        adapter = OutlookAdapter(
            config={"tenant_id": "t", "client_id": "c"},
            credentials={"access_token": "tok", "client_secret": "s", "refresh_token": "r"},
        )
        assert adapter.needs_token_refresh() is False
