"""Bug #50 — `POST /api/message_queues/{id}/extend-visibility` endpoint.

Lets a runtime extend the Mongo queue's visibility_timeout on a still-
claimed message in lockstep with its Temporal activity heartbeat.
Bug #49 fixed the Temporal heartbeat half; this endpoint is the queue
heartbeat half.

Tests pin the endpoint's shape via source inspection (route registration
+ contract) and behavior via FastAPI TestClient against the in-memory
test setup.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shape pins
# ---------------------------------------------------------------------------


def _src() -> str:
    return Path(
        "/Users/home/Repositories/indemn-os/kernel/api/queue_routes.py"
    ).read_text()


def test_extend_visibility_route_registered():
    """The route MUST be registered at the canonical URL — the harness
    calls it from the cron_runner heartbeat loop."""
    src = _src()
    assert (
        '@queue_router.post("/api/message_queues/{message_id}/extend-visibility")'
        in src
    )
    assert "async def extend_visibility(" in src


def test_extend_visibility_idempotent_on_terminal_status():
    """Late calls after the activity completes / dead-letters MUST
    no-op — must not surprise the caller with 400/404."""
    src = _src()
    start = src.index("async def extend_visibility(")
    end = src.index("\n@queue_router.post", start + 1)
    body = src[start:end]

    assert '("completed", "dead_letter", "failed")' in body, (
        "Terminal-status idempotency must include all three terminal states"
    )
    assert '"idempotent": True' in body


def test_extend_visibility_uses_5min_offset():
    """The new visibility timeout MUST be 5 minutes from now — same
    duration as the initial claim's timeout (kernel/message/mongodb_bus.py:38).
    Anything shorter and the whole point is defeated; anything longer and
    we drift from the queue's documented contract."""
    src = _src()
    start = src.index("async def extend_visibility(")
    end = src.index("\n@queue_router.post", start + 1)
    body = src[start:end]

    assert "timedelta(minutes=5)" in body


def test_extend_visibility_only_processing():
    """Refuses to extend on `pending` (nothing to extend — message
    isn't claimed). Must check status before update."""
    src = _src()
    start = src.index("async def extend_visibility(")
    end = src.index("\n@queue_router.post", start + 1)
    body = src[start:end]

    # The update query also includes `"status": "processing"` to prevent
    # races where the status changed between the read and the write
    assert '{"_id": message.id, "status": "processing"}' in body


# ---------------------------------------------------------------------------
# Behavior tests — exercise the function directly with mocked Message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extend_visibility_processing_returns_extended_status():
    """Happy path: processing message gets visibility extended."""
    from bson import ObjectId

    from kernel.api import queue_routes

    msg_id = ObjectId()
    fake_message = MagicMock()
    fake_message.id = msg_id
    fake_message.status = "processing"

    fake_coll = MagicMock()
    fake_coll.update_one = AsyncMock()

    with patch.object(
        queue_routes.Message, "get", AsyncMock(return_value=fake_message)
    ), patch.object(
        queue_routes.Message,
        "get_motor_collection",
        lambda: fake_coll,
    ):
        before = datetime.now(timezone.utc)
        result = await queue_routes.extend_visibility(
            str(msg_id), actor=MagicMock()
        )
        after = datetime.now(timezone.utc)

    assert result["status"] == "extended"
    assert result["message_id"] == str(msg_id)

    # update_one should have been called once with new visibility_timeout
    fake_coll.update_one.assert_called_once()
    call_args = fake_coll.update_one.call_args
    set_clause = call_args[0][1]["$set"]
    new_vis = set_clause["visibility_timeout"]
    # ~5 min from now — allow some slack for test execution
    assert before + timedelta(minutes=4, seconds=55) <= new_vis
    assert new_vis <= after + timedelta(minutes=5, seconds=5)


@pytest.mark.asyncio
async def test_extend_visibility_terminal_returns_idempotent():
    """Terminal status (completed / dead_letter / failed) returns
    idempotent=True without raising or updating."""
    from bson import ObjectId

    from kernel.api import queue_routes

    for terminal in ("completed", "dead_letter", "failed"):
        fake_message = MagicMock()
        fake_message.status = terminal

        fake_coll = MagicMock()
        fake_coll.update_one = AsyncMock()

        msg_id = ObjectId()
        with patch.object(
            queue_routes.Message, "get", AsyncMock(return_value=fake_message)
        ), patch.object(
            queue_routes.Message,
            "get_motor_collection",
            lambda: fake_coll,
        ):
            result = await queue_routes.extend_visibility(
                str(msg_id), actor=MagicMock()
            )

        assert result["status"] == terminal
        assert result["idempotent"] is True
        fake_coll.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_extend_visibility_pending_raises_400():
    """Pending message (not claimed) cannot have visibility extended —
    nothing to extend. Must raise 400."""
    from bson import ObjectId
    from fastapi import HTTPException

    from kernel.api import queue_routes

    fake_message = MagicMock()
    fake_message.status = "pending"

    msg_id = ObjectId()
    with patch.object(
        queue_routes.Message, "get", AsyncMock(return_value=fake_message)
    ):
        with pytest.raises(HTTPException) as exc_info:
            await queue_routes.extend_visibility(
                str(msg_id), actor=MagicMock()
            )
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_extend_visibility_missing_raises_404():
    """Missing message returns 404."""
    from bson import ObjectId
    from fastapi import HTTPException

    from kernel.api import queue_routes

    msg_id = ObjectId()
    with patch.object(queue_routes.Message, "get", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await queue_routes.extend_visibility(
                str(msg_id), actor=MagicMock()
            )
        assert exc_info.value.status_code == 404
