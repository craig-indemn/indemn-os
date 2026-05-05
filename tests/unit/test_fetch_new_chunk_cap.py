"""Tests for fetch_new per-call chunk cap + oldest-first sort — Bug #50 follow-on.

Bug #50 fixed the visibility-recovery race + attempt_count cap. But the root
chronic slowness in Email Fetcher (subprocess >5min on backed-up watermark)
came from `fetch_new`'s sequential per-entity `save_tracked()` loop. With
~150-300ms per save × N new entities × 11-mailbox fan-out, accumulated
backlog made each cron tick chronically exceed even the visibility-extend
window.

Bridging fix: `params["limit"]` caps saves per call. Subsequent ticks pick
up the rest. Subprocess time stays bounded.

Critical correctness requirement: the cap must apply to the OLDEST-first
slice. Otherwise (e.g., Gmail's API returns newest-first by default), saving
the 100 newest items would advance the watermark past unsaved older items,
leaving them stranded forever.

Tests pin: (1) sort-ascending-then-cap behavior produces a stable drain;
(2) cap is opt-in (no limit = old unbounded behavior, preserves manual
backfill semantics); (3) dedup happens BEFORE the cap (don't waste a save
slot on a duplicate).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def patch_dispatch_with_results():
    """Mock get_adapter + execute_with_retry to return a controllable
    raw_results list. Returns a setter so each test can configure its
    own raw_results."""
    state = {"raw_results": [], "saved_items": []}

    async def fake_execute(adapter, method, **kwargs):
        return list(state["raw_results"])

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
        yield state


def _entity_cls_with_no_existing():
    """Mock entity_cls with no existing entities (empty watermark + no dedup)."""
    entity_cls = MagicMock()
    entity_cls.__name__ = "FakeEntity"

    chain = MagicMock()
    chain.sort.return_value = chain
    chain.limit.return_value = chain
    chain.to_list = AsyncMock(return_value=[])  # No latest entity → no since
    entity_cls.find_scoped = MagicMock(return_value=chain)

    # Constructor — return a mock entity that records its kwargs
    saved = []

    def constructor(**kwargs):
        m = MagicMock()
        m.data = kwargs
        m.external_ref = kwargs.get("external_ref")
        m.id = f"mock-id-{len(saved) + 1}"
        saved.append(m.data)
        return m

    entity_cls.side_effect = constructor
    entity_cls._saved_items = saved  # expose for assertions
    return entity_cls


@pytest.fixture(autouse=True)
def patch_bulk_save():
    """Patch bulk_save_tracked so fetch_new tests exercise sorting/capping
    logic without hitting real DB. The mock records what entities were passed."""

    async def fake_bulk_save(entities, actor_id=None, method=None, correlation_id=None):
        return {
            "succeeded": len(entities),
            "errored": 0,
            "errors": [],
            "created_ids": [str(e.id) for e in entities],
            "duration_ms": 1.0,
        }

    with patch("kernel.entity.save.bulk_save_tracked", new=fake_bulk_save):
        yield


class TestChunkCap:
    """Bug #50 follow-on: limit param caps saves per call, oldest-first."""

    @pytest.mark.asyncio
    async def test_limit_caps_saves_at_N(self, patch_dispatch_with_results):
        """Given 250 raw items and limit=100, only 100 entities are saved."""
        from kernel.capability.fetch_new import fetch_new

        # 250 items with monotonically increasing dates
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        patch_dispatch_with_results["raw_results"] = [
            {"external_ref": f"ref-{i:04d}", "date": base + timedelta(minutes=i)}
            for i in range(250)
        ]

        entity_cls = _entity_cls_with_no_existing()

        result = await fetch_new(
            entity_cls,
            config={"system_type": "fake"},
            org_id="org-1",
            params={"limit": 100},
        )

        assert result["fetched"] == 250
        assert result["created"] == 100
        assert len(entity_cls._saved_items) == 100

    @pytest.mark.asyncio
    async def test_oldest_first_when_capped(self, patch_dispatch_with_results):
        """Given items in newest-first order (Gmail API default) and limit=100,
        the 100 oldest are saved — NOT the 100 newest. This guards the
        watermark-correctness invariant: if we save newest-first, the
        watermark advances past unsaved older items, stranding them."""
        from kernel.capability.fetch_new import fetch_new

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 250 items in NEWEST-FIRST order (mimics Gmail API default)
        items = [
            {"external_ref": f"ref-{i:04d}", "date": base + timedelta(minutes=i)}
            for i in range(250)
        ]
        items.reverse()  # newest first
        patch_dispatch_with_results["raw_results"] = items

        entity_cls = _entity_cls_with_no_existing()

        await fetch_new(
            entity_cls,
            config={"system_type": "fake"},
            org_id="org-1",
            params={"limit": 100},
        )

        # First 100 saved should be the 100 OLDEST (refs 0000-0099),
        # not the 100 newest (refs 0150-0249).
        saved_refs = [s["external_ref"] for s in entity_cls._saved_items]
        assert saved_refs[0] == "ref-0000"
        assert saved_refs[-1] == "ref-0099"

    @pytest.mark.asyncio
    async def test_no_limit_means_unbounded(self, patch_dispatch_with_results):
        """No `limit` param → save everything (preserves manual-backfill
        semantics: `indemn email fetch-new --data '{"since": "..."}'`
        should still pull EVERY email in the window)."""
        from kernel.capability.fetch_new import fetch_new

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        patch_dispatch_with_results["raw_results"] = [
            {"external_ref": f"ref-{i:04d}", "date": base + timedelta(minutes=i)}
            for i in range(250)
        ]

        entity_cls = _entity_cls_with_no_existing()

        result = await fetch_new(
            entity_cls,
            config={"system_type": "fake"},
            org_id="org-1",
            params={},  # No limit
        )

        assert result["created"] == 250
        assert len(entity_cls._saved_items) == 250

    @pytest.mark.asyncio
    async def test_dedup_before_cap(self, patch_dispatch_with_results):
        """If 50 of 150 items are dupes, limit=100 should save 100 NEW
        items (not 100 raw items minus 50 dupes = 50 actually saved).
        Otherwise the cap wastes save slots on items we'll skip anyway,
        and progress is much slower than expected."""
        from kernel.capability.fetch_new import fetch_new

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        all_items = [
            {"external_ref": f"ref-{i:04d}", "date": base + timedelta(minutes=i)}
            for i in range(150)
        ]
        patch_dispatch_with_results["raw_results"] = all_items

        # Mock: refs 0000-0049 already exist (50 dupes)
        entity_cls = MagicMock()
        entity_cls.__name__ = "FakeEntity"

        watermark_chain = MagicMock()
        watermark_chain.sort.return_value = watermark_chain
        watermark_chain.limit.return_value = watermark_chain
        watermark_chain.to_list = AsyncMock(return_value=[])

        existing_chain = MagicMock()
        existing_records = [
            MagicMock(external_ref=f"ref-{i:04d}") for i in range(50)
        ]
        existing_chain.to_list = AsyncMock(return_value=existing_records)

        def find_scoped(filter_):
            if "external_ref" in filter_:
                return existing_chain
            return watermark_chain

        entity_cls.find_scoped = MagicMock(side_effect=find_scoped)

        # Track what bulk_save_tracked receives
        bulk_saved_entities = []

        async def tracking_bulk_save(entities, actor_id=None, method=None, correlation_id=None):
            bulk_saved_entities.extend(entities)
            return {
                "succeeded": len(entities),
                "errored": 0,
                "errors": [],
                "created_ids": [str(e.id) for e in entities],
                "duration_ms": 1.0,
            }

        def constructor(**kwargs):
            m = MagicMock()
            m.data = kwargs
            m.external_ref = kwargs.get("external_ref")
            m.id = f"mock-id-{len(bulk_saved_entities) + 1}"
            return m

        entity_cls.side_effect = constructor

        with patch(
            "kernel.capability.fetch_new.current_actor_id",
            new=MagicMock(get=MagicMock(return_value="actor-fake")),
        ), patch(
            "kernel.integration.dispatch.get_adapter",
            new=AsyncMock(return_value=MagicMock()),
        ), patch(
            "kernel.integration.dispatch.execute_with_retry",
            new=AsyncMock(return_value=all_items),
        ), patch(
            "kernel.entity.save.bulk_save_tracked",
            new=tracking_bulk_save,
        ):
            result = await fetch_new(
                entity_cls,
                config={"system_type": "fake"},
                org_id="org-1",
                params={"limit": 100},
            )

        # 50 dupes skipped. 100 new ones saved (refs 0050-0149).
        assert result["fetched"] == 150
        assert result["skipped_duplicates"] == 50
        assert result["created"] == 100
        assert len(bulk_saved_entities) == 100
        # First saved should be ref-0050 (oldest non-dupe)
        assert bulk_saved_entities[0].data["external_ref"] == "ref-0050"
        # Last saved should be ref-0149
        assert bulk_saved_entities[-1].data["external_ref"] == "ref-0149"
