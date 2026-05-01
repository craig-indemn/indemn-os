"""Tests for SlackAdapter — TD-1 sub-piece 4 (NEW build).

Direct Slack Web API integration: conversations.list + conversations.history,
paginated via cursor, dedup by composite (channel_id, slack_ts) external_ref.
File attachments → linked Document entities.
"""

from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from kernel.integration.adapter import AdapterValidationError


def _make_adapter(token="xoxb-test"):
    from kernel.integration.adapters.slack import SlackAdapter
    return SlackAdapter(config={}, credentials={"bot_token": token})


class TestSlackAdapterRegistration:
    """Pin the (provider, version) registration key — Bug #45b regression guard."""

    def test_registers_under_slack_v1(self):
        # Importing the module triggers register_adapter() at import time.
        import kernel.integration.adapters.slack  # noqa: F401
        from kernel.integration.adapters.slack import SlackAdapter
        from kernel.integration.registry import get_adapter_class

        assert get_adapter_class("slack", "v1") is SlackAdapter


class TestSlackAdapterContract:
    """fetch_new wire-shape: returns SlackMessage-shaped dicts ready for
    kernel.capability.fetch_new."""

    @pytest.mark.asyncio
    async def test_rejects_unknown_kwarg(self):
        """Bug #36 discipline — strict params, no silent absorption."""
        adapter = _make_adapter()
        with pytest.raises(AdapterValidationError) as exc:
            await adapter.fetch(folder="random")
        assert "folder" in str(exc.value)
        assert "since, until, channels, limit" in str(exc.value)

    @pytest.mark.asyncio
    async def test_requires_bot_token_in_credentials(self):
        """Constructor rejects missing bot_token explicitly (not a runtime
        AttributeError later)."""
        from kernel.integration.adapters.slack import SlackAdapter
        with pytest.raises(AdapterValidationError) as exc:
            SlackAdapter(config={}, credentials={})
        assert "bot_token" in str(exc.value)

    @pytest.mark.asyncio
    async def test_returns_dicts_with_slackmessage_fields(self):
        """Each dict must have SlackMessage entity's required fields plus
        external_ref (= "{channel_id}:{slack_ts}") for fetch_new dedup."""
        adapter = _make_adapter()

        # Mock the two API calls: conversations.list + conversations.history
        async def fake_request(method, params=None):
            if method == "conversations.list":
                return {"ok": True, "channels": [{"id": "C01", "name": "general", "is_archived": False}]}
            if method == "conversations.history":
                return {
                    "ok": True,
                    "messages": [
                        {
                            "ts": "1745000000.001000",
                            "user": "U999",
                            "text": "Hello team",
                            "type": "message",
                        }
                    ],
                }
            return {"ok": True}

        with patch.object(adapter, "_api_call", side_effect=fake_request):
            results = await adapter.fetch()

        assert len(results) == 1
        m = results[0]
        assert m["slack_ts"] == "1745000000.001000"
        assert m["channel_id"] == "C01"
        assert m["channel_name"] == "general"
        assert m["user_id"] == "U999"
        assert m["text"] == "Hello team"
        assert m["external_ref"] == "C01:1745000000.001000"
        # posted_at parsed from slack_ts
        assert "posted_at" in m

    @pytest.mark.asyncio
    async def test_thread_ts_carried_when_present(self):
        """If a message has thread_ts, surface it on the SlackMessage dict."""
        adapter = _make_adapter()

        async def fake_request(method, params=None):
            if method == "conversations.list":
                return {"ok": True, "channels": [{"id": "C01", "name": "general", "is_archived": False}]}
            if method == "conversations.history":
                return {
                    "ok": True,
                    "messages": [
                        {
                            "ts": "1745000099.123456",
                            "user": "U1",
                            "text": "thread reply",
                            "thread_ts": "1745000000.001000",
                        }
                    ],
                }
            return {"ok": True}

        with patch.object(adapter, "_api_call", side_effect=fake_request):
            results = await adapter.fetch()

        assert results[0]["thread_ts"] == "1745000000.001000"

    @pytest.mark.asyncio
    async def test_filters_by_since_via_oldest_param(self):
        """`since` (ISO datetime) translates to Slack's `oldest` (unix epoch)."""
        adapter = _make_adapter()
        captured = []

        async def fake_request(method, params=None):
            captured.append((method, params or {}))
            if method == "conversations.list":
                return {"ok": True, "channels": [{"id": "C01", "name": "general", "is_archived": False}]}
            if method == "conversations.history":
                return {"ok": True, "messages": []}
            return {"ok": True}

        with patch.object(adapter, "_api_call", side_effect=fake_request):
            await adapter.fetch(since="2026-04-01T00:00:00Z")

        # Find the conversations.history call
        history_calls = [(m, p) for m, p in captured if m == "conversations.history"]
        assert history_calls, "conversations.history was never called"
        _, params = history_calls[0]
        # 2026-04-01T00:00:00Z = 1775001600 unix
        assert params.get("oldest") == "1775001600"
        assert params.get("channel") == "C01"

    @pytest.mark.asyncio
    async def test_filters_by_until_via_latest_param(self):
        """`until` (ISO datetime) translates to Slack's `latest` (unix epoch)."""
        adapter = _make_adapter()
        captured = []

        async def fake_request(method, params=None):
            captured.append((method, params or {}))
            if method == "conversations.list":
                return {"ok": True, "channels": [{"id": "C01", "name": "general", "is_archived": False}]}
            if method == "conversations.history":
                return {"ok": True, "messages": []}
            return {"ok": True}

        with patch.object(adapter, "_api_call", side_effect=fake_request):
            await adapter.fetch(
                since="2026-04-01T00:00:00Z",
                until="2026-04-30T00:00:00Z",
            )

        history_calls = [(m, p) for m, p in captured if m == "conversations.history"]
        _, params = history_calls[0]
        assert params.get("oldest") == "1775001600"  # 2026-04-01T00:00:00Z
        assert params.get("latest") == "1777507200"  # 2026-04-30T00:00:00Z

    @pytest.mark.asyncio
    async def test_paginates_via_cursor(self):
        """conversations.history with response_metadata.next_cursor → fetch again."""
        adapter = _make_adapter()
        history_call_count = {"n": 0}

        async def fake_request(method, params=None):
            if method == "conversations.list":
                return {"ok": True, "channels": [{"id": "C01", "name": "general", "is_archived": False}]}
            if method == "conversations.history":
                history_call_count["n"] += 1
                if history_call_count["n"] == 1:
                    return {
                        "ok": True,
                        "messages": [{"ts": "1745000000.001000", "user": "U1", "text": "msg1"}],
                        "response_metadata": {"next_cursor": "tok-abc"},
                    }
                return {
                    "ok": True,
                    "messages": [{"ts": "1745000099.001000", "user": "U2", "text": "msg2"}],
                }
            return {"ok": True}

        with patch.object(adapter, "_api_call", side_effect=fake_request):
            results = await adapter.fetch()

        assert len(results) == 2
        assert {m["slack_ts"] for m in results} == {"1745000000.001000", "1745000099.001000"}
        assert history_call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_excludes_dm_channels(self):
        """conversations.list called with types=public_channel,private_channel
        — DMs (im, mpim) explicitly excluded."""
        adapter = _make_adapter()
        captured_params = {}

        async def fake_request(method, params=None):
            if method == "conversations.list":
                captured_params.update(params or {})
                return {"ok": True, "channels": []}
            return {"ok": True, "messages": []}

        with patch.object(adapter, "_api_call", side_effect=fake_request):
            await adapter.fetch()

        types = captured_params.get("types", "")
        assert "public_channel" in types
        assert "private_channel" in types
        assert "im" not in types
        assert "mpim" not in types

    @pytest.mark.asyncio
    async def test_skips_archived_channels(self):
        """Archived channels are skipped — no history fetch for them."""
        adapter = _make_adapter()
        history_calls = []

        async def fake_request(method, params=None):
            if method == "conversations.list":
                return {
                    "ok": True,
                    "channels": [
                        {"id": "C01", "name": "active", "is_archived": False},
                        {"id": "C02", "name": "old", "is_archived": True},
                    ],
                }
            if method == "conversations.history":
                history_calls.append(params.get("channel"))
                return {"ok": True, "messages": []}
            return {"ok": True}

        with patch.object(adapter, "_api_call", side_effect=fake_request):
            await adapter.fetch()

        assert history_calls == ["C01"]
