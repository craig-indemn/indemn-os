"""Tests for Outlook adapter — field mapping, token refresh detection, and
the Bug #36 propagated fix (until + unknown-param rejection in fetch)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernel.integration.adapter import AdapterValidationError
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


# --- Bug #36 propagated fix: until plumbing + unknown-param rejection ---


def _make_adapter():
    return OutlookAdapter(
        config={"tenant_id": "t", "client_id": "c"},
        credentials={"access_token": "tok", "client_secret": "s", "refresh_token": "r"},
    )


class TestFetchUnknownParamsRejection:
    """The Outlook adapter must reject unknown kwargs explicitly. Same pattern
    as GoogleWorkspaceAdapter — silent **params absorption was the enabling
    failure for Bug #36 and the same defect existed here."""

    @pytest.mark.asyncio
    async def test_fetch_rejects_unknown_kwarg(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterValidationError) as exc:
            await adapter.fetch(query="foo")
        assert "query" in str(exc.value)
        assert "since, until, folder, limit" in str(exc.value)


class TestFetchFilterConstruction:
    """Microsoft Graph $filter clauses combined with `and`. Datetime is
    full-precision (not date-precision like Gmail), so no client-side filter
    is needed."""

    async def _capture_filter(self, since, until):
        adapter = _make_adapter()
        captured_params = {}

        # Mock httpx.AsyncClient context manager + .get
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"value": []}
        response.raise_for_status = MagicMock()

        client = MagicMock()
        client.get = AsyncMock(
            side_effect=lambda url, headers, params, timeout: (
                captured_params.update(params),
                response,
            )[1],
        )
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("kernel.integration.adapters.outlook.httpx.AsyncClient", return_value=client):
            await adapter.fetch(since=since, until=until)
        return captured_params.get("$filter", "")

    @pytest.mark.asyncio
    async def test_no_bounds(self):
        f = await self._capture_filter(None, None)
        assert f == ""

    @pytest.mark.asyncio
    async def test_since_only_unchanged(self):
        f = await self._capture_filter("2026-04-29T00:00:00Z", None)
        # Pre-Bug-#36 behavior preserved
        assert f == "receivedDateTime ge 2026-04-29T00:00:00Z"

    @pytest.mark.asyncio
    async def test_until_only(self):
        f = await self._capture_filter(None, "2026-04-29T22:00:00Z")
        assert f == "receivedDateTime le 2026-04-29T22:00:00Z"

    @pytest.mark.asyncio
    async def test_since_and_until_anded(self):
        f = await self._capture_filter("2026-04-29T18:00:00Z", "2026-04-29T22:00:00Z")
        assert "receivedDateTime ge 2026-04-29T18:00:00Z" in f
        assert "receivedDateTime le 2026-04-29T22:00:00Z" in f
        assert " and " in f
