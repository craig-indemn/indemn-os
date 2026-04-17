"""Chat harness entry point — WebSocket server + deepagents.

Per the design: "The conversation panel is a running harness instance —
a real-time actor." One image serves many associates. Generic per kind+framework.

Two auth modes on same server:
- User JWT (default assistant in UI) — permissions match user's roles
- Service token (external chat agents) — permissions from service actor's roles

Session lifecycle per connection:
1. WebSocket connects with auth + associate metadata
2. Load associate config, create Interaction + Attention
3. Build deepagents agent with checkpointer for persistence
4. Process messages: user turn → agent → streamed response
5. On disconnect: close Attention + Interaction, cleanup
"""

import asyncio
import json
import logging
import os

import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from harness_common.runtime import RUNTIME_ID, register_instance, heartbeat_loop
from harness.session import ChatSession

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def _setup_gcp_credentials():
    """Write GCP service account JSON to file if provided via env var."""
    sa_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")
    if sa_json:
        sa_path = "/tmp/gcp-sa.json"
        with open(sa_path, "w") as f:
            f.write(sa_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path
        log.info("GCP credentials written to %s", sa_path)


# Active sessions by WebSocket connection
_sessions: dict[int, ChatSession] = {}


async def websocket_handler(websocket: WebSocket):
    """Handle one WebSocket connection — one ChatSession per connection."""
    await websocket.accept()
    session = None

    try:
        # First message must be connect with auth
        connect_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        if connect_msg.get("type") != "connect":
            await websocket.send_json({"type": "error", "content": "First message must be type=connect"})
            await websocket.close()
            return

        associate_id = connect_msg.get("associate_id", "")
        auth_token = connect_msg.get("auth_token", os.environ.get("INDEMN_SERVICE_TOKEN", ""))

        if not associate_id:
            await websocket.send_json({"type": "error", "content": "associate_id required"})
            await websocket.close()
            return

        # Create session
        session = ChatSession(
            websocket=websocket,
            associate_id=associate_id,
            auth_token=auth_token,
            checkpointer=None,  # TODO: LangGraph MongoDB checkpointer
        )
        _sessions[id(websocket)] = session

        # Initialize session (load config, create Interaction + Attention)
        await session.start()
        await websocket.send_json({"type": "connected", "interaction_id": session.interaction_id})

        # Message loop
        while True:
            raw = await websocket.receive_json()
            msg_type = raw.get("type", "message")

            if msg_type == "message":
                content = raw.get("content", "")
                context = raw.get("context")
                await session.handle_message(content, context)
            elif msg_type == "disconnect":
                break

    except WebSocketDisconnect:
        log.info("WebSocket disconnected")
    except asyncio.TimeoutError:
        log.warning("WebSocket connect timeout")
    except Exception as e:
        log.error("WebSocket error: %s", e, exc_info=True)
    finally:
        if session:
            await session.close()
            _sessions.pop(id(websocket), None)


routes = [
    WebSocketRoute("/ws/chat", websocket_handler),
]

app = Starlette(routes=routes)


async def startup():
    log.info("Starting chat-deepagents harness, runtime=%s", RUNTIME_ID)
    log.info("Sandbox type: %s", os.environ.get("INDEMN_SANDBOX_TYPE", "localshell"))
    _setup_gcp_credentials()
    await register_instance()
    # Start Runtime heartbeat in background
    asyncio.create_task(heartbeat_loop(interval_s=30.0))


app.add_event_handler("startup", startup)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
