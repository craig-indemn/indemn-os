"""Tests for bulk_save_tracked — batched insert with audit chain + watch evaluation.

Replaces the sequential per-entity save_tracked loop in fetch_new with a bulk
path that does: construct entities → insert_many(ordered=False) → in-memory
hash-chained change records → batched watch evaluation → grouped messages.

Tests pin: happy path, partial failure on BulkWriteError, hash chain integrity
across batch, watch evaluation fires per entity, empty list returns zeros,
created_by auto-population, OTEL span attributes, dedup via E11000 silent skip.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_kernel_deps():
    """Patch kernel dependencies that bulk_save_tracked imports."""
    with patch(
        "kernel.entity.save.current_correlation_id",
        MagicMock(get=MagicMock(return_value="corr-123")),
    ), patch(
        "kernel.entity.save.current_effective_actor_id",
        MagicMock(get=MagicMock(return_value="eff-actor-456")),
    ), patch(
        "kernel.entity.save.current_causation_message_id",
        MagicMock(get=MagicMock(return_value="causation-789")),
    ), patch(
        "kernel.entity.save.current_depth",
        MagicMock(get=MagicMock(return_value=0)),
    ), patch(
        "kernel.entity.save.evaluate_computed_fields",
    ) as mock_ecf, patch(
        "kernel.entity.save.evaluate_watches_and_emit",
        new=AsyncMock(return_value=[]),
    ) as mock_watches, patch(
        "kernel.entity.save.build_event_metadata",
        return_value={"event": "created"},
    ), patch(
        "kernel.entity.save.create_span",
    ) as mock_span:
        # Make create_span a context manager
        mock_span.return_value.__enter__ = MagicMock(return_value=None)
        mock_span.return_value.__exit__ = MagicMock(return_value=False)
        yield {
            "evaluate_computed_fields": mock_ecf,
            "evaluate_watches_and_emit": mock_watches,
            "create_span": mock_span,
        }


def _make_entity(ext_ref, org_id="aabbccdd11223344aabbccdd"):
    """Build a mock entity that quacks like a domain entity for bulk insert."""
    from bson import ObjectId

    entity = MagicMock()
    entity.id = None
    entity.org_id = ObjectId(org_id)
    entity.version = 0
    entity.updated_at = None
    entity.created_by = None
    entity.external_ref = ext_ref
    entity.__class__.__name__ = "TestEntity"
    type(entity).__name__ = "TestEntity"

    # model_dump for serialization
    def model_dump(by_alias=False):
        return {
            "_id": entity.id,
            "org_id": entity.org_id,
            "version": entity.version,
            "updated_at": entity.updated_at,
            "created_by": entity.created_by,
            "external_ref": ext_ref,
        }

    entity.model_dump = model_dump

    # get_motor_collection returns a mock collection
    mock_coll = MagicMock()
    mock_coll.insert_many = AsyncMock()
    mock_coll.database.client = MagicMock()
    entity.get_motor_collection = MagicMock(return_value=mock_coll)

    return entity


@pytest.mark.asyncio
async def test_bulk_save_tracked_happy_path(mock_kernel_deps):
    """10 entities all insert cleanly — succeeded=10, errored=0."""
    from kernel.entity.save import bulk_save_tracked

    entities = [_make_entity(f"ref-{i}") for i in range(10)]

    # Patch ChangeRecord and hash chain
    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="genesis-hash"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        side_effect=lambda r: f"hash-{id(r)}",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = lambda **kwargs: MagicMock(
            model_dump=lambda by_alias=False: kwargs, **kwargs
        )

        result = await bulk_save_tracked(entities, actor_id="actor-1", method="fetch_new")

    assert result["succeeded"] == 10
    assert result["errored"] == 0
    assert len(result["created_ids"]) == 10
    assert result["errors"] == []
    assert "duration_ms" in result


@pytest.mark.asyncio
async def test_bulk_save_tracked_empty_list(mock_kernel_deps):
    """Empty input short-circuits — no DB calls."""
    from kernel.entity.save import bulk_save_tracked

    result = await bulk_save_tracked([], actor_id="actor-1")
    assert result == {"succeeded": 0, "errored": 0, "errors": [], "created_ids": []}


@pytest.mark.asyncio
async def test_bulk_save_tracked_partial_failure_dedup(mock_kernel_deps):
    """BulkWriteError with E11000 on some items — those are silently skipped, not errors."""
    from pymongo.errors import BulkWriteError

    from kernel.entity.save import bulk_save_tracked

    entities = [_make_entity(f"ref-{i}") for i in range(5)]

    bwe = BulkWriteError(
        {
            "writeErrors": [
                {"index": 1, "code": 11000, "errmsg": "E11000 duplicate key error"},
                {"index": 3, "code": 11000, "errmsg": "E11000 duplicate key error"},
            ],
            "nInserted": 3,
        }
    )

    mock_coll = MagicMock()
    mock_coll.insert_many = AsyncMock(side_effect=bwe)
    for e in entities:
        e.get_motor_collection = MagicMock(return_value=mock_coll)

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = lambda **kwargs: MagicMock(
            model_dump=lambda by_alias=False: kwargs, **kwargs
        )

        result = await bulk_save_tracked(entities, actor_id="actor-1", method="fetch_new")

    # 3 succeeded (indices 0, 2, 4), 2 dedup skipped (indices 1, 3), 0 real errors
    assert result["succeeded"] == 3
    assert result["errored"] == 0
    assert len(result["created_ids"]) == 3


@pytest.mark.asyncio
async def test_bulk_save_tracked_partial_failure_real_error(mock_kernel_deps):
    """Non-E11000 BulkWriteError entries are reported as errors."""
    from pymongo.errors import BulkWriteError

    from kernel.entity.save import bulk_save_tracked

    entities = [_make_entity(f"ref-{i}") for i in range(3)]

    bwe = BulkWriteError(
        {
            "writeErrors": [
                {"index": 1, "code": 121, "errmsg": "Document validation failed"},
            ],
            "nInserted": 2,
        }
    )

    mock_coll = MagicMock()
    mock_coll.insert_many = AsyncMock(side_effect=bwe)
    for e in entities:
        e.get_motor_collection = MagicMock(return_value=mock_coll)

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = lambda **kwargs: MagicMock(
            model_dump=lambda by_alias=False: kwargs, **kwargs
        )

        result = await bulk_save_tracked(entities, actor_id="actor-1", method="fetch_new")

    assert result["succeeded"] == 2
    assert result["errored"] == 1
    assert "Document validation failed" in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_bulk_save_tracked_hash_chain_integrity(mock_kernel_deps):
    """Change records are chained — each record's previous_hash = prior record's current_hash."""
    from kernel.entity.save import bulk_save_tracked

    entities = [_make_entity(f"ref-{i}") for i in range(4)]

    hash_calls = []

    def tracking_compute_hash(record):
        h = f"hash-{len(hash_calls)}"
        hash_calls.append({"prev": record.previous_hash, "computed": h})
        return h

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="genesis"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        side_effect=tracking_compute_hash,
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )

        def cr_factory(**kwargs):
            m = MagicMock()
            m.model_dump = lambda by_alias=False: kwargs
            for k, v in kwargs.items():
                setattr(m, k, v)
            return m

        MockCR.side_effect = cr_factory

        result = await bulk_save_tracked(entities, actor_id="actor-1", method="fetch_new")

    assert result["succeeded"] == 4
    # Verify chain: first record chains from genesis, subsequent from prior
    assert hash_calls[0]["prev"] == "genesis"
    assert hash_calls[1]["prev"] == "hash-0"
    assert hash_calls[2]["prev"] == "hash-1"
    assert hash_calls[3]["prev"] == "hash-2"


@pytest.mark.asyncio
async def test_bulk_save_tracked_watches_fire_per_entity(mock_kernel_deps):
    """Watch evaluation is called once per successfully inserted entity."""
    from kernel.entity.save import bulk_save_tracked

    entities = [_make_entity(f"ref-{i}") for i in range(3)]

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = lambda **kwargs: MagicMock(
            model_dump=lambda by_alias=False: kwargs, **kwargs
        )

        result = await bulk_save_tracked(entities, actor_id="actor-1", method="fetch_new")

    mock_watches = mock_kernel_deps["evaluate_watches_and_emit"]
    assert mock_watches.call_count == 3
    # All calls should have event_type="created"
    for call in mock_watches.call_args_list:
        assert call.kwargs["event_type"] == "created"


@pytest.mark.asyncio
async def test_bulk_save_tracked_created_by_populated(mock_kernel_deps):
    """created_by field is set via _resolve_created_by (effective_actor_id preferred)."""
    from kernel.entity.save import bulk_save_tracked

    entities = [_make_entity("ref-1")]
    assert entities[0].created_by is None

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = lambda **kwargs: MagicMock(
            model_dump=lambda by_alias=False: kwargs, **kwargs
        )

        await bulk_save_tracked(entities, actor_id="actor-1", method="fetch_new")

    # effective_actor_id is "eff-actor-456" per mock_kernel_deps fixture
    assert entities[0].created_by == "eff-actor-456"


@pytest.mark.asyncio
async def test_bulk_save_tracked_version_set_to_1(mock_kernel_deps):
    """All entities get version=1 (new inserts)."""
    from kernel.entity.save import bulk_save_tracked

    entities = [_make_entity(f"ref-{i}") for i in range(3)]
    for e in entities:
        assert e.version == 0

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = lambda **kwargs: MagicMock(
            model_dump=lambda by_alias=False: kwargs, **kwargs
        )

        await bulk_save_tracked(entities, actor_id="actor-1")

    for e in entities:
        assert e.version == 1


@pytest.mark.asyncio
async def test_bulk_save_tracked_objectid_assigned(mock_kernel_deps):
    """Entities without id get an ObjectId assigned before insert."""
    from bson import ObjectId

    from kernel.entity.save import bulk_save_tracked

    entities = [_make_entity(f"ref-{i}") for i in range(3)]
    for e in entities:
        assert e.id is None

    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=AsyncMock())
        )
        MockCR.side_effect = lambda **kwargs: MagicMock(
            model_dump=lambda by_alias=False: kwargs, **kwargs
        )

        await bulk_save_tracked(entities, actor_id="actor-1")

    for e in entities:
        assert isinstance(e.id, ObjectId)


@pytest.mark.asyncio
async def test_bulk_save_tracked_change_records_inserted(mock_kernel_deps):
    """Change records are bulk-inserted for all succeeded entities."""
    from kernel.entity.save import bulk_save_tracked

    entities = [_make_entity(f"ref-{i}") for i in range(5)]

    changes_insert_many = AsyncMock()
    with patch(
        "kernel.changes.hash_chain.get_previous_hash",
        new=AsyncMock(return_value="prev"),
    ), patch(
        "kernel.changes.collection.ChangeRecord",
    ) as MockCR, patch(
        "kernel.changes.hash_chain.compute_hash",
        return_value="h",
    ):
        MockCR.get_motor_collection = MagicMock(
            return_value=MagicMock(insert_many=changes_insert_many)
        )
        MockCR.side_effect = lambda **kwargs: MagicMock(
            model_dump=lambda by_alias=False: kwargs, **kwargs
        )

        await bulk_save_tracked(entities, actor_id="actor-1", method="fetch_new")

    # insert_many called once with 5 change record dicts
    changes_insert_many.assert_called_once()
    docs = changes_insert_many.call_args[0][0]
    assert len(docs) == 5


def test_bulk_save_tracked_is_async():
    """bulk_save_tracked is an async function (shape pin)."""
    from kernel.entity.save import bulk_save_tracked

    assert inspect.iscoroutinefunction(bulk_save_tracked)


def test_bulk_save_tracked_source_has_otel_span():
    """Source contains create_span call with entity.bulk_save_tracked (shape pin)."""
    from kernel.entity import save

    src = inspect.getsource(save.bulk_save_tracked)
    assert "entity.bulk_save_tracked" in src
    assert "batch_size" in src
