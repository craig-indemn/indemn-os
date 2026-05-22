"""WebSocket handler — real-time entity updates via MongoDB Change Streams. [G-34]

Each WebSocket connection watches the database-level change stream,
filtered by org_id. Clients send subscribe/unsubscribe messages to
control which entity types and entities they receive updates for.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import orjson
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import WebSocket, WebSocketDisconnect

from kernel.auth.jwt import verify_access_token
from kernel.db import get_database

logger = logging.getLogger(__name__)

# Track active connections
_connections: dict[str, dict] = {}  # connection_id -> {ws, subscriptions, org_id}


async def websocket_handler(websocket: WebSocket):
    """WebSocket endpoint handler. Authenticate via query param token."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001)
        return

    try:
        payload = verify_access_token(token)
    except Exception:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    connection_id = str(uuid4())
    org_id = payload["org_id"]

    _connections[connection_id] = {
        "ws": websocket,
        "subscriptions": {},
        "org_id": org_id,
    }

    watcher_task = None
    try:
        # Start change stream watcher for this connection
        watcher_task = asyncio.create_task(_watch_changes(connection_id, org_id, websocket))

        # Handle incoming messages (subscribe/unsubscribe/ping)
        async for data in websocket.iter_text():
            try:
                msg = orjson.loads(data)
            except Exception:
                continue

            msg_type = msg.get("type")
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "subscribe":
                sub_id = msg.get("subscription_id", str(uuid4()))
                _connections[connection_id]["subscriptions"][sub_id] = msg.get("filter", {})
            elif msg_type == "unsubscribe":
                _connections[connection_id]["subscriptions"].pop(msg.get("subscription_id"), None)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error for connection %s", connection_id)
    finally:
        if watcher_task:
            watcher_task.cancel()
        _connections.pop(connection_id, None)


async def _watch_changes(connection_id: str, org_id: str, websocket: WebSocket):
    """Watch MongoDB Change Streams and push matching changes to the client.

    Filter-aware: only sends changes matching active subscriptions. [G-34]

    The JWT carries `org_id` as a hex string, but MongoDB stores `org_id`
    as a BSON ObjectId. `$match` is exact-type, so the filter MUST cast
    the string to an ObjectId or it silently matches zero documents and
    every WebSocket consumer gets dead air. Cast defensively — a
    malformed claim raises InvalidId, which we log and abort.
    """
    db = get_database()

    try:
        org_oid = ObjectId(org_id)
    except (InvalidId, TypeError):
        logger.warning(
            "WebSocket %s: invalid org_id claim %r; aborting watcher",
            connection_id,
            org_id,
        )
        return

    # Database-level change stream filtered by org_id (BSON ObjectId form).
    pipeline = [
        {"$match": {"fullDocument.org_id": org_oid}},
    ]

    try:
        async with db.watch(pipeline, full_document="updateLookup") as stream:
            async for change in stream:
                conn = _connections.get(connection_id)
                if not conn:
                    break

                doc = change.get("fullDocument", {})
                ns = change.get("ns", {})
                collection = ns.get("coll", "")

                # Check against active subscriptions
                for sub_id, sub_filter in conn["subscriptions"].items():
                    if _matches_filter(collection, doc, sub_filter):
                        try:
                            await websocket.send_json(
                                {
                                    "type": "entity_change",
                                    "subscription_id": sub_id,
                                    "collection": collection,
                                    "operation": change.get("operationType"),
                                    "entity_type": _collection_to_entity_type(collection),
                                    "entity_id": str(doc.get("_id", "")),
                                    "data": _serialize_for_ws(doc),
                                }
                            )
                        except Exception:
                            return
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Change stream watcher error for %s", connection_id)


def _matches_filter(collection: str, doc: dict, sub_filter: dict) -> bool:
    """Check if a change matches a subscription filter."""
    if not sub_filter:
        return True

    if sub_filter.get("entity_type"):
        expected_coll = sub_filter["entity_type"].lower() + "s"
        if collection != expected_coll:
            return False

    if sub_filter.get("entity_id"):
        if str(doc.get("_id")) != sub_filter["entity_id"]:
            return False

    if sub_filter.get("collection"):
        if collection != sub_filter["collection"]:
            return False

    return True


def _collection_to_entity_type(collection: str) -> str:
    """Map collection name back to entity type name."""
    from kernel.db import ENTITY_REGISTRY

    for name, cls in ENTITY_REGISTRY.items():
        coll_name = getattr(getattr(cls, "Settings", None), "name", None)
        if coll_name == collection:
            return name
        # Domain entities
        class_coll = getattr(cls, "_collection_name", None)
        if class_coll == collection:
            return name
    # Fallback: strip trailing 's' and capitalize
    return collection.rstrip("s").capitalize()


def _serialize_for_ws(doc: dict) -> dict:
    """Serialize a MongoDB document for WebSocket transmission."""
    result = {}
    for key, value in doc.items():
        if key == "_id":
            result["_id"] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif isinstance(value, bytes):
            continue  # Skip binary fields
        else:
            _json_safe = (str, int, float, bool, list, dict, type(None))
            result[key] = value if isinstance(value, _json_safe) else str(value)
    return result
