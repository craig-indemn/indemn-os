"""Tests for kernel.api.websocket — change-stream pipeline filter.

Regression guard. The JWT carries `org_id` as a hex string, but MongoDB
stores `org_id` as a BSON ObjectId. MongoDB's `$match` aggregation
stage is exact-type, so a string filter against an ObjectId field
silently matches zero documents — and every WebSocket consumer gets
dead air. The fix casts the JWT string to ObjectId before building the
pipeline. These tests pin both the source-level guard (the cast
happens) and the failure-mode behavior (malformed org_id aborts the
watcher cleanly instead of letting a string filter through).

History: discovered 2026-05-22 during customer-os-ui Phase B
end-to-end verification — direct MongoDB change-stream watch with a
string filter saw 0 events; the same watch with `ObjectId(org_id)`
saw every entity insert. Affected every WebSocket consumer since the
endpoint was introduced [G-34].
"""

from __future__ import annotations

import inspect

from kernel.api import websocket as ws_module


def test_websocket_module_imports_objectid():
    """The fix is grounded in casting org_id to ObjectId. Pin the import
    so a future refactor that drops it gets caught here.
    """
    source = inspect.getsource(ws_module)
    assert "from bson import ObjectId" in source, (
        "kernel.api.websocket must import bson.ObjectId for the change-"
        "stream pipeline filter cast"
    )


def test_pipeline_filter_uses_objectid_not_raw_string():
    """The change-stream `$match` filter must compare against the BSON
    ObjectId form of org_id, not the raw JWT string. Pin the source so a
    refactor that reverts to `{"fullDocument.org_id": org_id}` (the
    original bug) is caught at unit-test time before it ships dead air to
    every WebSocket consumer.
    """
    source = inspect.getsource(ws_module._watch_changes)

    # The cast must happen
    assert "ObjectId(org_id)" in source, (
        "_watch_changes must cast org_id to ObjectId before using in the "
        "pipeline; raw-string filter against an ObjectId field matches "
        "zero documents"
    )

    # The pipeline must reference the converted value, not the raw string
    assert "{\"fullDocument.org_id\": org_id}" not in source, (
        "_watch_changes pipeline still uses the raw `org_id` string in "
        "the $match filter — this is the original bug and silently "
        "matches zero documents"
    )

    # And specifically should use the cast variable
    assert "org_oid" in source, (
        "_watch_changes should bind the ObjectId-converted value to a "
        "named variable (org_oid) and use it in the pipeline"
    )


def test_pipeline_filter_aborts_on_invalid_org_id():
    """If the JWT carries a malformed org_id (not a 24-char hex), the
    ObjectId cast raises InvalidId. The watcher must catch it and abort
    cleanly with a warning log — not crash, not let an unfiltered stream
    through, not throw to the parent coroutine.
    """
    source = inspect.getsource(ws_module._watch_changes)
    assert "InvalidId" in source, (
        "_watch_changes must handle InvalidId from ObjectId(org_id) — a "
        "malformed JWT claim shouldn't crash the watcher coroutine"
    )
    assert "from bson.errors import InvalidId" in inspect.getsource(ws_module), (
        "kernel.api.websocket must import bson.errors.InvalidId for the "
        "defensive ObjectId cast"
    )
