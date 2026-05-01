"""Tests for fetch_new watermark fallback chain — Bug #46.

The kernel's fetch_new capability computes an incremental `since` watermark
from the latest existing entity's source-system timestamp. Originally this
was hardcoded to a `date` field — which works for Email and Meeting but
NOT for SlackMessage (uses `posted_at`) or Document (uses `created_date`).

Effect of the bug: every cron tick re-fetched ALL recent items from the
source, kernel deduplicated against external_ref, created=0 silently. The
adapter still made the API calls + the agent burned LLM tokens for nothing.
Surfaced 2026-05-01 Session 15 after backfilling 860 SlackMessages and
seeing identical drain pattern (50 fetched / 0 created / 50 skipped) on
every subsequent fetch — and confirming Drive-Fetcher had been doing the
same thing hourly for ~17h since Session 14 activation.

Fix: try a candidate-field list `("date", "posted_at", "created_date")`,
first one that exists on the entity AND has a non-null value wins. Falls
through to "no since" if none match (preserves "fetch all" semantic).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernel.capability.fetch_new import WATERMARK_FIELD_CANDIDATES, fetch_new


def _entity_cls_with_latest(field_name: str | None, value):
    """Build a mock entity_cls whose find_scoped(...).sort(...).limit(1).to_list()
    returns one mock entity with the given attribute, or [] if value is None."""
    entity_cls = MagicMock()
    entity_cls.__name__ = "FakeEntity"

    if value is None:
        latest_records = []
    else:
        record = MagicMock()
        # Only set the named field; getattr for others returns Mock (truthy by default)
        # so we explicitly clear non-target fields to None to mimic a real entity.
        for f in WATERMARK_FIELD_CANDIDATES:
            setattr(record, f, value if f == field_name else None)
        latest_records = [record]

    # Mock the chain: find_scoped({}) -> .sort(f) -> .limit(1) -> .to_list()
    chain = MagicMock()
    chain.sort.return_value = chain
    chain.limit.return_value = chain
    chain.to_list = AsyncMock(return_value=latest_records)
    entity_cls.find_scoped = MagicMock(return_value=chain)
    return entity_cls, chain


@pytest.fixture
def patch_dispatch():
    """Mock get_adapter + execute_with_retry so fetch_new can run end-to-end
    without an adapter. Returns the captured fetch_params for assertion."""
    captured = {"params": None}

    async def fake_execute(adapter, method, **kwargs):
        captured["params"] = kwargs
        return []  # No raw_results — fetch_new just returns counts of zero

    fake_adapter = MagicMock()
    with patch(
        "kernel.integration.dispatch.get_adapter",
        new=AsyncMock(return_value=fake_adapter),
    ), patch(
        "kernel.integration.dispatch.execute_with_retry",
        new=fake_execute,
    ), patch(
        "kernel.capability.fetch_new.current_actor_id",
        new=MagicMock(get=MagicMock(return_value="actor-fake")),
    ):
        yield captured


class TestWatermarkFallbackChain:
    @pytest.mark.asyncio
    async def test_uses_date_when_present(self, patch_dispatch):
        """Email/Meeting case: entity has `date` field → since pulled from date."""
        ts = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        entity_cls, chain = _entity_cls_with_latest("date", ts)

        await fetch_new(entity_cls, {"system_type": "x"}, org_id="org1", params={})

        assert patch_dispatch["params"]["since"] == ts.isoformat()
        # First sort attempted should be `-date`
        chain.sort.assert_called_with("-date")

    @pytest.mark.asyncio
    async def test_uses_posted_at_when_date_missing(self, patch_dispatch):
        """SlackMessage case: no `date`, but `posted_at` exists → falls through."""
        ts = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)

        # Build entity_cls where -date sort fails (raises) but -posted_at works.
        entity_cls = MagicMock()
        entity_cls.__name__ = "SlackMessage"

        date_chain = MagicMock()
        date_chain.sort.return_value = date_chain
        date_chain.limit.return_value = date_chain
        date_chain.to_list = AsyncMock(side_effect=Exception("no such field: date"))

        posted_chain = MagicMock()
        posted_chain.sort.return_value = posted_chain
        posted_chain.limit.return_value = posted_chain
        record = MagicMock(date=None, posted_at=ts, created_date=None)
        posted_chain.to_list = AsyncMock(return_value=[record])

        # find_scoped({}) is called once per candidate field; return a fresh chain each time.
        chains_iter = iter([date_chain, posted_chain])
        entity_cls.find_scoped = MagicMock(side_effect=lambda *a, **k: next(chains_iter))

        await fetch_new(entity_cls, {"system_type": "messaging"}, org_id="org1", params={})

        assert patch_dispatch["params"]["since"] == ts.isoformat()

    @pytest.mark.asyncio
    async def test_uses_created_date_when_others_missing(self, patch_dispatch):
        """Document case: no `date` or `posted_at`, but `created_date` exists."""
        ts = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        entity_cls = MagicMock()
        entity_cls.__name__ = "Document"

        # Two failed sorts (date, posted_at) followed by a successful one (created_date)
        fail = MagicMock()
        fail.sort.return_value = fail
        fail.limit.return_value = fail
        fail.to_list = AsyncMock(side_effect=Exception("no such field"))

        success = MagicMock()
        success.sort.return_value = success
        success.limit.return_value = success
        record = MagicMock(date=None, posted_at=None, created_date=ts)
        success.to_list = AsyncMock(return_value=[record])

        chains_iter = iter([fail, fail, success])
        entity_cls.find_scoped = MagicMock(side_effect=lambda *a, **k: next(chains_iter))

        await fetch_new(entity_cls, {"system_type": "drive"}, org_id="org1", params={})

        assert patch_dispatch["params"]["since"] == ts.isoformat()

    @pytest.mark.asyncio
    async def test_no_watermark_when_all_fields_missing(self, patch_dispatch):
        """Entity without any candidate field: since stays unset (fetch all)."""
        entity_cls = MagicMock()
        entity_cls.__name__ = "Bare"
        fail = MagicMock()
        fail.sort.return_value = fail
        fail.limit.return_value = fail
        fail.to_list = AsyncMock(side_effect=Exception("no such field"))
        entity_cls.find_scoped = MagicMock(return_value=fail)

        await fetch_new(entity_cls, {"system_type": "x"}, org_id="org1", params={})

        assert "since" not in patch_dispatch["params"]

    @pytest.mark.asyncio
    async def test_no_watermark_when_no_existing_entities(self, patch_dispatch):
        """Empty collection: latest is [], no value, since stays unset."""
        entity_cls, chain = _entity_cls_with_latest("date", None)
        await fetch_new(entity_cls, {"system_type": "x"}, org_id="org1", params={})
        assert "since" not in patch_dispatch["params"]

    @pytest.mark.asyncio
    async def test_no_watermark_when_field_value_is_null(self, patch_dispatch):
        """Latest entity exists but the candidate field is None → continue to next candidate."""
        ts = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        entity_cls = MagicMock()
        entity_cls.__name__ = "PartiallyPopulated"

        # `-date` sort returns a record where date is None (so we skip it)
        # `-posted_at` sort returns a record where posted_at is set
        date_chain = MagicMock()
        date_chain.sort.return_value = date_chain
        date_chain.limit.return_value = date_chain
        record_no_date = MagicMock(date=None, posted_at=ts, created_date=None)
        date_chain.to_list = AsyncMock(return_value=[record_no_date])

        posted_chain = MagicMock()
        posted_chain.sort.return_value = posted_chain
        posted_chain.limit.return_value = posted_chain
        record_with_posted = MagicMock(date=None, posted_at=ts, created_date=None)
        posted_chain.to_list = AsyncMock(return_value=[record_with_posted])

        chains_iter = iter([date_chain, posted_chain])
        entity_cls.find_scoped = MagicMock(side_effect=lambda *a, **k: next(chains_iter))

        await fetch_new(entity_cls, {"system_type": "messaging"}, org_id="org1", params={})

        assert patch_dispatch["params"]["since"] == ts.isoformat()

    @pytest.mark.asyncio
    async def test_explicit_since_takes_precedence(self, patch_dispatch):
        """Caller-provided `since` is never overridden by the watermark logic."""
        ts = datetime(2026, 4, 15, tzinfo=timezone.utc)
        entity_cls, chain = _entity_cls_with_latest("date", ts)

        await fetch_new(
            entity_cls, {"system_type": "x"}, org_id="org1",
            params={"since": "2025-01-01T00:00:00+00:00"},
        )

        # The caller's value wins; no sort query attempted at all
        assert patch_dispatch["params"]["since"] == "2025-01-01T00:00:00+00:00"
        # find_scoped should NOT have been called when since is provided
        entity_cls.find_scoped.assert_not_called()


class TestWatermarkFieldCandidatesShape:
    """Pin the constant against drift — covers Email/Meeting (date),
    SlackMessage (posted_at), Document (created_date)."""

    def test_candidates_in_documented_order(self):
        assert WATERMARK_FIELD_CANDIDATES == ("date", "posted_at", "created_date")
