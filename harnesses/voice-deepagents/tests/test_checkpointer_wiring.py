"""Pin MongoDBSaver wiring in voice harness (AI-407 §15.4 voice + §13).

Phase 4 (AI-407): voice harness wires MongoDBSaver (was MemorySaver per the
v2 runbook § Known gaps). Keyed by interaction_id per §13 — real-time
sessions accumulate state across turns; resumes load prior state cleanly.

Why module-level lazy init + asyncio.Lock (vs chat's Starlette lifespan):
LiveKit Agents (`agents.cli.run_app(WorkerOptions(...))`) owns its own
event loop and dispatches per-room jobs into it. There's no Starlette app
to attach a lifespan to. Module-level `_checkpointer` cache + asyncio.Lock
makes init safe under concurrent room dispatches.

Module path imports + heavy-dep stubs come from `tests/conftest.py`.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_checkpointer_module_cache():
    """Reset the module-level cache between tests so each test starts clean."""
    import main as main_mod

    main_mod._checkpointer = None
    yield
    main_mod._checkpointer = None


class TestCheckpointerLazyInit:
    async def test_missing_mongodb_uri_disables_gracefully(self):
        """No MONGODB_URI env → checkpointer disabled (returns None) — voice
        falls back to per-turn in-memory state (no resume), matching today's
        degraded behavior. Caches the False sentinel so subsequent calls
        don't retry."""
        import main as main_mod

        with patch.dict("os.environ", {}, clear=True):
            saver = await main_mod._get_or_init_checkpointer()

        assert saver is None
        # Cached False sentinel means "tried + failed; don't keep retrying"
        assert main_mod._checkpointer is False

    async def test_repeated_call_returns_cached_value(self):
        """Once initialized (or marked disabled), subsequent calls return
        the cached value without re-running the init logic."""
        import main as main_mod

        # Prime the cache with a fake saver
        fake_saver = MagicMock(name="cached-saver")
        main_mod._checkpointer = fake_saver

        result = await main_mod._get_or_init_checkpointer()
        assert result is fake_saver

    async def test_lock_protects_concurrent_init(self):
        """Concurrent first-call attempts should not race; init runs once."""
        import main as main_mod

        # Provide MONGODB_URI but mock the Mongo client to track calls
        with patch.dict("os.environ", {"MONGODB_URI": "mongodb://localhost:27017/?test"}):
            with patch.object(main_mod, "AsyncIOMotorClient") as mock_client_cls, \
                 patch.object(main_mod, "MongoDBSaver") as mock_saver_cls:
                # Make admin.command awaitable
                mock_client_instance = MagicMock()
                mock_client_instance.admin.command = AsyncMock(return_value={"ok": 1})
                mock_client_cls.return_value = mock_client_instance
                mock_saver_cls.return_value = MagicMock(name="real-saver")

                # Fire many concurrent _get_or_init_checkpointer calls
                results = await asyncio.gather(
                    *[main_mod._get_or_init_checkpointer() for _ in range(5)]
                )

                # All return the same instance (cached after first init)
                assert all(r is results[0] for r in results)
                # AsyncIOMotorClient was called exactly once (lock prevented races)
                assert mock_client_cls.call_count == 1

    async def test_unreachable_mongo_caches_failure_sentinel(self):
        """If MONGODB_URI is set but Mongo is unreachable, the checkpointer
        gracefully degrades to None + caches the False sentinel so we don't
        keep retrying on every entrypoint call."""
        import main as main_mod

        with patch.dict("os.environ", {"MONGODB_URI": "mongodb://unreachable:1234"}):
            with patch.object(main_mod, "AsyncIOMotorClient") as mock_client_cls:
                # Simulate connection failure
                mock_client_instance = MagicMock()
                mock_client_instance.admin.command = AsyncMock(
                    side_effect=ConnectionError("unreachable")
                )
                mock_client_cls.return_value = mock_client_instance

                saver = await main_mod._get_or_init_checkpointer()

                assert saver is None
                assert main_mod._checkpointer is False
