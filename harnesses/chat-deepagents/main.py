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
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from harness.session import ChatSession
from harness_common.cli import CLIError, indemn
from harness_common.runtime import RUNTIME_ID, heartbeat_loop, register_instance
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def _setup_gcp_credentials():
    """Write GCP service account JSON to file if provided via env var.

    Fixes escaped newlines in PEM keys — Railway env vars store \\n as
    literal backslash-n, but PEM needs actual newlines.
    """
    sa_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")
    if sa_json:
        import json as json_mod

        try:
            data = json_mod.loads(sa_json)
            if "private_key" in data:
                data["private_key"] = data["private_key"].replace("\\n", "\n")
            data.setdefault("type", "service_account")
            data.setdefault("auth_uri", "https://accounts.google.com/o/oauth2/auth")
            data.setdefault("token_uri", "https://oauth2.googleapis.com/token")
            data.setdefault("universe_domain", "googleapis.com")
            sa_json = json_mod.dumps(data)
        except Exception as e:
            log.warning("Failed to parse GCP SA JSON: %s", e)

        sa_path = "/tmp/gcp-sa.json"
        with open(sa_path, "w") as f:
            f.write(sa_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path
        log.info("GCP credentials written to %s", sa_path)


# Conversation persistence — LangGraph MongoDB checkpointer
_checkpointer = None


async def _init_checkpointer_at_startup():
    """Initialize async MongoDB checkpointer during app startup.

    Uses motor (AsyncIOMotorClient) — the same async driver the kernel API uses.
    This avoids the pymongo sync connectivity issue on Railway containers.
    """
    global _checkpointer
    mongodb_uri = os.environ.get("MONGODB_URI", "")
    if not mongodb_uri:
        log.warning("No MONGODB_URI — conversation persistence disabled")
        _checkpointer = False
        return

    try:
        from langgraph.checkpoint.mongodb import MongoDBSaver
        from motor.motor_asyncio import AsyncIOMotorClient

        # Use motor for connectivity (works on Railway), then pass its
        # underlying pymongo client to MongoDBSaver (which needs sync client)
        motor_client = AsyncIOMotorClient(mongodb_uri)
        await motor_client.admin.command("ping")
        # motor wraps pymongo — .delegate is the underlying sync MongoClient
        _checkpointer = MongoDBSaver(motor_client.delegate, db_name="indemn_os_checkpoints")
        log.info("MongoDB checkpointer initialized — conversation persistence enabled")
    except Exception as e:
        log.warning("MongoDB checkpointer unavailable — persistence disabled: %s", e)
        _checkpointer = False


def get_checkpointer():
    """Return the checkpointer (initialized at startup). None if unavailable."""
    return _checkpointer if _checkpointer is not False else None


# Active sessions by WebSocket connection
_sessions: dict[int, ChatSession] = {}


async def _start_deployment_session(
    *,
    websocket: WebSocket,
    deployment_id: str,
    dynamic_params: dict,
    auth_token: str,
    connect_msg: dict,
):
    """Start a Deployment-driven chat session (AI-408 §15.3).

    Validation chain (additively wired across Tasks 3.2-3.7):
      Task 3.2 — Deployment load + status check (this task)
      Task 3.3 — Origin allowlist (Deployment.allowed_origins)
      Task 3.4 — JWT validation (HS256 dual-mode + purpose-claim per AI-407)
      Task 3.5 — acts_as security gate
      Task 3.6 — JSON Schema validation of dynamic_params
      Task 3.7 — <deployment_context> SystemMessage with sanitized params

    WebSocket close codes (chat-vs-HTTP divergence from voice frontdoor):
      4004 — deployment_not_found  (HTTP analog: 404)
      4009 — deployment_not_active (HTTP analog: 409)
      1008 — policy_violation      (HTTP analogs: 401/403; future tasks)

    Returns ChatSession on success, None on validation failure (after
    sending an error message + closing the WebSocket).
    """
    # Step 1: Load Deployment via authenticated CLI. The runtime's
    # INDEMN_SERVICE_TOKEN (process env) carries read access; we don't pass
    # the connect-message auth_token here because the runtime needs to read
    # the Deployment regardless of which user is connecting.
    try:
        deployment = await asyncio.to_thread(
            indemn, "deployment", "get", deployment_id
        )
    except CLIError as e:
        log.info(
            "Deployment not found or CLI error: %s (%s)", deployment_id, e
        )
        await websocket.send_json(
            {
                "type": "error",
                "content": f"Deployment not found: {deployment_id}",
                "code": "not_found",
            }
        )
        await websocket.close(code=4004)
        return None

    # Step 2: Deployment status check per §5.7 state machine — only `active`
    # accepts sessions. `configured` / `paused` / `archived` / `error` reject.
    status = deployment.get("status")
    if status != "active":
        log.info(
            "Rejecting session for deployment %s (status=%r, expected active)",
            deployment_id,
            status,
        )
        await websocket.send_json(
            {
                "type": "error",
                "content": f"Deployment not active (status={status})",
                "code": "deployment_not_active",
                "status": status,
            }
        )
        await websocket.close(code=4009)
        return None

    # TODO Task 3.3: Origin header validation against deployment.allowed_origins
    # TODO Task 3.4: JWT validation (HS256 + purpose-claim per AI-407)
    # TODO Task 3.5: acts_as security gate — JWT.sub vs supplied actor_id
    # TODO Task 3.6: parameter_schema validation of dynamic_params

    # Pre-Task-3.5 default: effective_actor_id = Deployment.associate_id
    # (i.e., associate_self semantics — the agent acts as itself, the
    # supplied dynamic_params.actor_id is irrelevant until the acts_as gate
    # is wired in Task 3.5). The legacy associate_id-only path reaches
    # ChatSession with this same defaulting via the existing __init__ logic.
    associate_id = str(deployment["associate_id"])
    effective_actor_id = associate_id

    session = ChatSession(
        websocket=websocket,
        associate_id=associate_id,
        auth_token=auth_token,
        checkpointer=get_checkpointer(),
        interaction_id=connect_msg.get("interaction_id"),
        deployment=deployment,
        dynamic_params=dynamic_params,
        effective_actor_id=effective_actor_id,
    )
    await session.start()
    return session


async def websocket_handler(websocket: WebSocket):
    """Handle one WebSocket connection — one ChatSession per connection."""
    await websocket.accept()
    session = None

    try:
        # First message must be connect with auth
        connect_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        if connect_msg.get("type") != "connect":
            err = {"type": "error", "content": "First message must be type=connect"}
            await websocket.send_json(err)
            await websocket.close()
            return

        # AI-408 Task 3.1: accept deployment_id + dynamic_params additively.
        # When deployment_id is set, take the Deployment-driven path. Otherwise
        # the legacy associate_id-only path runs (current OS Base UI flow).
        deployment_id = connect_msg.get("deployment_id")
        associate_id = connect_msg.get("associate_id", "")
        auth_token = connect_msg.get("auth_token", os.environ.get("INDEMN_SERVICE_TOKEN", ""))
        interaction_id = connect_msg.get("interaction_id")
        dynamic_params = connect_msg.get("dynamic_params") or {}

        if not isinstance(dynamic_params, dict):
            await websocket.send_json(
                {"type": "error", "content": "dynamic_params must be a JSON object"}
            )
            await websocket.close()
            return

        if not deployment_id and not associate_id:
            await websocket.send_json(
                {
                    "type": "error",
                    "content": (
                        "Either deployment_id or associate_id required "
                        "in connect message"
                    ),
                }
            )
            await websocket.close()
            return

        if deployment_id:
            # New Deployment-driven path — full chain (Deployment load + Origin
            # + JWT + acts_as + parameter_schema + deployment_context) shipped
            # in Tasks 3.2-3.7.
            session = await _start_deployment_session(
                websocket=websocket,
                deployment_id=deployment_id,
                dynamic_params=dynamic_params,
                auth_token=auth_token,
                connect_msg=connect_msg,
            )
            if session is None:
                return  # _start_deployment_session sent its own error + close
        else:
            # Legacy associate_id-only path — current OS Base UI flow.
            session = ChatSession(
                websocket=websocket,
                associate_id=associate_id,
                auth_token=auth_token,
                checkpointer=get_checkpointer(),
                interaction_id=interaction_id,
            )
            await session.start()

        _sessions[id(websocket)] = session
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


async def health(request):
    """Health check for Railway."""
    return JSONResponse({"status": "healthy", "service": "indemn-runtime-chat"})


routes = [
    Route("/health", health),
    WebSocketRoute("/ws/chat", websocket_handler),
]


@asynccontextmanager
async def lifespan(app):
    log.info("Starting chat-deepagents harness, runtime=%s", RUNTIME_ID)
    log.info("Sandbox type: %s", os.environ.get("INDEMN_SANDBOX_TYPE", "localshell"))
    _setup_gcp_credentials()
    await register_instance()
    asyncio.create_task(heartbeat_loop(interval_s=30.0))
    # Initialize MongoDB checkpointer at startup (warm connection pool before sessions)
    await _init_checkpointer_at_startup()
    yield


app = Starlette(routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
