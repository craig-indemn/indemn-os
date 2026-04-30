"""Slack adapter — Web API for workspace messages + file attachments.

Polling mode: conversations.list (public+private channels, no DMs initially)
+ conversations.history per channel, paginated via cursor. Returns
SlackMessage-shaped dicts ready for kernel.capability.fetch_new (dedups by
external_ref = "{channel_id}:{slack_ts}").

Architectural decisions (Session 13/14, see customer-system roadmap.md TD-1
+ artifacts/2026-04-30-slack-adapter-design.md):
- Direct Slack Web API, not third-party libs (matches outlook.py style).
- All channels via Slack admin permission; no DMs initially.
- Per-message granularity; threading is metadata via thread_ts, not entity.
- Polling 5min cadence; Events API push deferred to post-TD-2.
- Bug #36 discipline: strict params, no silent absorption.
"""

import logging
from datetime import datetime, timezone

import httpx

from kernel.integration.adapter import (
    Adapter,
    AdapterAuthError,
    AdapterError,
    AdapterRateLimitError,
    AdapterValidationError,
)
from kernel.integration.registry import register_adapter

logger = logging.getLogger(__name__)


SLACK_API_BASE = "https://slack.com/api"


def _iso_to_unix_epoch(s: str) -> str:
    """Convert ISO 8601 datetime to Slack's unix epoch string format."""
    if not s:
        return None
    if "T" not in s:
        s = s + "T00:00:00Z"
    if not (s.endswith("Z") or "+" in s[10:]):
        s = s + "Z"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return str(int(dt.timestamp()))


def _slack_ts_to_iso(ts: str) -> str:
    """Convert Slack timestamp string (e.g. '1745000000.001000') to ISO datetime."""
    if not ts:
        return None
    epoch = float(ts)
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class SlackAdapter(Adapter):
    """Slack workspace adapter. fetch() pulls SlackMessages incrementally
    across all non-archived public/private channels."""

    def __init__(self, config: dict, credentials: dict):
        super().__init__(config, credentials)
        self._token = credentials.get("bot_token")
        if not self._token:
            raise AdapterValidationError(
                "SlackAdapter requires bot_token in credentials. "
                "Configure via `indemn integration set-credentials <id> "
                "--secret-ref indemn/dev/integrations/slack-oauth` "
                "with secret containing {bot_token: 'xoxb-...'}"
            )
        self._workspace_id = config.get("workspace_id")  # optional

    async def _api_call(self, method: str, params: dict = None) -> dict:
        """Slack Web API call with bearer auth, JSON response. Raises on
        non-ok responses; surfaces rate-limit and auth errors as typed."""
        url = f"{SLACK_API_BASE}/{method}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                url,
                data=params or {},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if r.status_code == 429:
                retry = r.headers.get("Retry-After", "1")
                raise AdapterRateLimitError(f"Slack rate limit; retry after {retry}s")
            if r.status_code != 200:
                raise AdapterError(f"Slack API {method} HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if not data.get("ok"):
                err = data.get("error", "unknown")
                if err in ("invalid_auth", "not_authed", "token_expired"):
                    raise AdapterAuthError(f"Slack {method}: {err}")
                raise AdapterError(f"Slack {method} not ok: {err}")
            return data

    async def fetch(
        self,
        since: str = None,
        until: str = None,
        channels: list = None,
        limit: int = 1000,
        **unknown_params,
    ) -> list[dict]:
        """Fetch SlackMessage-shaped dicts from workspace channels.

        `since` / `until` are ISO 8601; converted to Slack unix epochs for
        oldest/latest filters on conversations.history.

        Returns list of dicts ready for kernel.capability.fetch_new — dedup
        happens via external_ref = '{channel_id}:{slack_ts}'.
        """
        # Bug #36 discipline.
        if unknown_params:
            raise AdapterValidationError(
                f"Unknown params for SlackAdapter.fetch: "
                f"{sorted(unknown_params.keys())}. "
                f"Supported: since, until, channels, limit."
            )

        # 1. Channel discovery (unless caller provided explicit list)
        if channels is None:
            channel_records = await self._list_channels()
        else:
            # When channels passed explicitly, we still want their names.
            channel_records = []
            for cid in channels:
                channel_records.append({"id": cid, "name": "", "is_archived": False})

        # Filter out archived; we don't ingest from them
        active_channels = [c for c in channel_records if not c.get("is_archived")]

        oldest = _iso_to_unix_epoch(since) if since else None
        latest = _iso_to_unix_epoch(until) if until else None

        all_messages: list[dict] = []
        seen_keys: set[str] = set()
        for channel in active_channels:
            if len(all_messages) >= limit:
                break
            try:
                msgs = await self._fetch_channel_history(
                    channel["id"], oldest=oldest, latest=latest,
                    remaining=limit - len(all_messages),
                )
                for msg in msgs:
                    ext_ref = f"{channel['id']}:{msg['ts']}"
                    if ext_ref in seen_keys:
                        continue
                    seen_keys.add(ext_ref)
                    all_messages.append(self._format_message(channel, msg))
                    if len(all_messages) >= limit:
                        break
            except Exception as e:
                logger.warning(
                    "SlackAdapter.fetch: skipping channel %s (%s): %s",
                    channel.get("name") or channel["id"], channel["id"], e,
                )
                continue

        logger.info(
            "SlackAdapter.fetch: returning %d messages across %d channels",
            len(all_messages), len(active_channels),
        )
        return all_messages

    async def _list_channels(self) -> list[dict]:
        """conversations.list — public + private channels only. Paginated by
        cursor. Returns raw channel records."""
        out = []
        cursor = None
        while True:
            params = {
                "types": "public_channel,private_channel",
                "exclude_archived": "false",  # we filter ourselves to log archived count
                "limit": "200",
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._api_call("conversations.list", params)
            out.extend(data.get("channels", []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return out

    async def _fetch_channel_history(
        self,
        channel_id: str,
        oldest: str = None,
        latest: str = None,
        remaining: int = 1000,
    ) -> list[dict]:
        """conversations.history paginated by cursor."""
        out = []
        cursor = None
        while True:
            params = {
                "channel": channel_id,
                "limit": str(min(200, max(1, remaining - len(out)))),
            }
            if oldest:
                params["oldest"] = oldest
            if latest:
                params["latest"] = latest
            if cursor:
                params["cursor"] = cursor
            data = await self._api_call("conversations.history", params)
            out.extend(data.get("messages", []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor or len(out) >= remaining:
                break
        return out

    def _format_message(self, channel: dict, msg: dict) -> dict:
        """Format a Slack message dict as a SlackMessage-shaped dict.

        external_ref = '{channel_id}:{slack_ts}' for fetch_new dedup.
        File attachments referenced by ID; Document creation deferred to
        a follow-on enrichment step (out of fetch path).
        """
        ts = msg.get("ts")
        return {
            "external_ref": f"{channel['id']}:{ts}",
            "slack_ts": ts,
            "channel_id": channel["id"],
            "channel_name": channel.get("name", ""),
            "thread_ts": msg.get("thread_ts"),
            "user_id": msg.get("user", ""),
            "text": msg.get("text", ""),
            "posted_at": _slack_ts_to_iso(ts),
            # files list: store IDs for now; Document materialization is a
            # separate enrichment step (avoids file-download blocking the fetch).
            "files": [f.get("id") for f in (msg.get("files") or []) if f.get("id")],
        }


register_adapter("messaging", "slack", SlackAdapter)
