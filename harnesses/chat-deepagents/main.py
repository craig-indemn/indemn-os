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

import jsonschema
import jwt as pyjwt
import uvicorn
from harness.session import ChatSession
from harness_common.cli import CLIError, indemn
from harness_common.jwt_auth import verify_jwt as _verify_jwt_shared
from harness_common.runtime import RUNTIME_ID, heartbeat_loop, register_instance
from jsonschema.validators import Draft202012Validator
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect


# AI-408 Task 3.4: audience pinned per surface. A JWT minted for voice
# (audience="runtime-voice-frontdoor") MUST NOT validate against chat.
# HS256 mode ignores this (OS doesn't set aud); RS256 mode enforces it.
JWT_AUDIENCE = "runtime-chat"


def _verify_jwt(token: str) -> dict:
    """Chat wrapper around the shared `harness_common.jwt_auth.verify_jwt`
    with the chat audience pinned. Wrapped so tests can patch a single
    symbol on this module without reaching into harness_common."""
    return _verify_jwt_shared(token, audience=JWT_AUDIENCE)


def _format_jsonschema_error(error: jsonschema.ValidationError) -> str:
    """Render a single ValidationError as `<path>: <message>` so the SDK
    can show the user which field failed. Mirrors voice-frontdoor."""
    if error.absolute_path:
        path = ".".join(str(p) for p in error.absolute_path)
        return f"{path}: {error.message}"
    return error.message


def _validate_parameters(
    deployment: dict, dynamic_params: dict
) -> tuple[dict, list[str]]:
    """Validate dynamic_params against `Deployment.parameter_schema` per §5.4
    (AI-408 Task 3.6 — mirrors voice-frontdoor's helper).

    Validation is on the MERGED static + dynamic set (per §5.4: "the schema
    describes the union"). Returns (merged_context, validation_warnings):
    - merged_context: static_parameters + dynamic_params (dynamic wins on
      key collision — operators expect this since dynamic is user-supplied
      override of operator-configured defaults)
    - validation_warnings: list of jsonschema error messages, empty if
      validation passed

    Caller decides what to do with non-empty warnings based on
    `Deployment.parameter_schema_validation_mode` ("strict" → reject with
    WS close; "forgiving" → log + proceed).

    Raises jsonschema.SchemaError if the schema itself is malformed —
    caller catches + returns the same error shape as a validation failure.
    """
    static_parameters = deployment.get("static_parameters") or {}
    merged = {**static_parameters, **(dynamic_params or {})}

    schema = deployment.get("parameter_schema")
    if not schema:
        # No schema = no validation per §5.2 (parameter_schema is optional
        # on Deployments with no dynamic params).
        return merged, []

    # Check the schema itself is valid before using it. Cheap defense in
    # depth — kernel save_tracked validates this at Deployment-creation
    # time, but legacy records or out-of-band writes could land malformed
    # schemas; we'd rather reject here than crash on a malformed validator.
    Draft202012Validator.check_schema(schema)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(merged), key=lambda e: e.path)
    warnings = [_format_jsonschema_error(e) for e in errors]
    return merged, warnings

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


def _is_origin_allowed(origin: str | None, allowed_origins: list[str]) -> bool:
    """Return True iff the WebSocket's `Origin` header is in the Deployment's
    allowed_origins list (AI-408 Task 3.3).

    Per §5.1: empty allowed_origins = reject ALL (Track 13f equivalent for
    chat). Missing Origin header also rejects — RFC 6455 requires browsers
    to send it on WebSocket upgrade, so absence indicates a non-browser
    client we have no allowlist policy for.

    Case-sensitive comparison — RFC 6454 specifies Origin headers are
    case-sensitive. Exact match only (no wildcards in v1).
    """
    if not origin:
        return False
    if not allowed_origins:
        return False
    return origin in allowed_origins


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
    # Sync call inside async function — matches the existing session.py.start()
    # pattern (which does 3+ sequential indemn() calls the same way). The
    # event-loop-blocking concern is real for chat (multi-session-per-process)
    # and is tracked as a cross-cutting follow-up in os-learnings.md so the
    # fix can land uniformly across legacy + deployment-driven paths.
    try:
        deployment = indemn("deployment", "get", deployment_id)
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

    # Step 2: Origin allowlist check (Task 3.3) — must come before status check
    # so an unauthorized origin can't probe Deployment activation state. Mirrors
    # voice-frontdoor's validation order (sessions.py: Origin → JWT → status).
    origin = websocket.headers.get("origin") if websocket.headers else None
    allowed_origins = deployment.get("allowed_origins") or []
    if not _is_origin_allowed(origin, allowed_origins):
        log.info(
            "Rejecting session for deployment %s (origin %r not in allowlist %r)",
            deployment_id,
            origin,
            allowed_origins,
        )
        await websocket.send_json(
            {
                "type": "error",
                "content": (
                    f"Origin '{origin}' not allowed for deployment {deployment_id}"
                ),
                "code": "origin_not_allowed",
            }
        )
        # RFC 6455 close code 1008 = "Policy Violation" — the canonical
        # WebSocket-side analog of HTTP 403.
        await websocket.close(code=1008)
        return None

    # Step 3: JWT validation per §10.6 + AI-407 pre-merge security fix.
    # Inherits HS256 dual-mode + purpose-claim enforcement from the shared
    # `harness_common.jwt_auth.verify_jwt`. WebSocket close 1008 ("Policy
    # Violation" per RFC 6455) is the canonical analog of HTTP 401.
    if not auth_token:
        log.info("Rejecting session %s — auth_token missing", deployment_id)
        await websocket.send_json(
            {
                "type": "error",
                "content": "auth_token required for Deployment-driven sessions",
                "code": "unauthorized",
                "reason": "missing",
            }
        )
        await websocket.close(code=1008)
        return None
    try:
        claims = _verify_jwt(auth_token)
    except pyjwt.ExpiredSignatureError:
        log.info("Rejecting session %s — auth_token expired", deployment_id)
        await websocket.send_json(
            {
                "type": "error",
                "content": "auth_token expired",
                "code": "unauthorized",
                "reason": "expired",
            }
        )
        await websocket.close(code=1008)
        return None
    except pyjwt.PyJWTError as e:
        log.info(
            "Rejecting session %s — JWT validation failed: %s", deployment_id, e
        )
        await websocket.send_json(
            {
                "type": "error",
                "content": "auth_token invalid",
                "code": "unauthorized",
                "reason": "invalid",
            }
        )
        await websocket.close(code=1008)
        return None

    # JWT.sub is the authenticated identity. Stashed for Task 3.5's acts_as
    # gate: session_actor mode compares it to dynamic_params.actor_id.
    authenticated_actor_id = claims["sub"]
    log.debug(
        "JWT validated for actor %s on deployment %s",
        authenticated_actor_id,
        deployment_id,
    )

    # Step 4: Deployment status check per §5.7 state machine — only `active`
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

    # Step 5: parameter_schema validation per §5.4 + §10.3.1 (Task 3.6).
    # Validates the MERGED static + dynamic param set against the Deployment's
    # parameter_schema (JSON Schema Draft 2020-12). Strict mode rejects with
    # WS close 1008 + validation_error; forgiving mode logs + proceeds. Runs
    # BEFORE acts_as so a malformed actor_id type gets rejected as a
    # validation error (operator-actionable) rather than silently passing
    # through to the impersonation-mismatch check.
    try:
        _merged_context, validation_warnings = _validate_parameters(
            deployment, dynamic_params
        )
    except jsonschema.SchemaError as e:
        # Malformed parameter_schema on the Deployment record itself.
        # Save-time validation should prevent this, but legacy records or
        # out-of-band writes could land bad schemas; surface as a
        # validation_error rather than crashing.
        log.warning(
            "Deployment %s has malformed parameter_schema: %s",
            deployment_id,
            e,
        )
        await websocket.send_json(
            {
                "type": "error",
                "content": (
                    f"Deployment parameter_schema is invalid: {e.message}"
                ),
                "code": "validation_error",
            }
        )
        await websocket.close(code=1008)
        return None

    validation_mode = (
        deployment.get("parameter_schema_validation_mode") or "strict"
    )
    if validation_warnings:
        if validation_mode == "strict":
            log.info(
                "Rejecting session for deployment %s (strict schema "
                "validation failed: %s)",
                deployment_id,
                validation_warnings,
            )
            await websocket.send_json(
                {
                    "type": "error",
                    "content": "; ".join(validation_warnings),
                    "code": "validation_error",
                }
            )
            await websocket.close(code=1008)
            return None
        # forgiving — log + proceed. Warnings stay server-side (matches
        # voice-frontdoor's behavior — success response shape is the
        # canonical 4 keys, no warnings field).
        log.info(
            "Forgiving-mode validation warnings on deployment %s: %s",
            deployment_id,
            validation_warnings,
        )

    # Step 6: acts_as security gate per §5.6 + §10.7 (Task 3.5).
    # LOAD-BEARING — this is the gate that makes the session_actor capability
    # safe. JWT IS the source of truth for `effective_actor_id`;
    # dynamic_params.actor_id is consulted ONLY for the mismatch check.
    # Inherited verbatim from voice-frontdoor's Task 2.31 contract — code
    # review verifies by reading `effective_actor_id = authenticated_actor_id`.
    associate_id = str(deployment["associate_id"])
    acts_as = deployment.get("acts_as")
    if acts_as == "session_actor":
        supplied_actor_id = dynamic_params.get("actor_id")
        # `is not None` (not truthy-check) — empty string / 0 / False still
        # count as "supplied" and must match. Schema validation (Task 3.6)
        # will catch most malformed cases; this is defense-in-depth.
        if (
            supplied_actor_id is not None
            and supplied_actor_id != authenticated_actor_id
        ):
            log.warning(
                "JWT impersonation attempt rejected — JWT.sub=%r, "
                "supplied actor_id=%r, deployment=%s",
                authenticated_actor_id,
                supplied_actor_id,
                deployment_id,
            )
            await websocket.send_json(
                {
                    "type": "error",
                    "content": (
                        "Supplied actor_id does not match authenticated JWT"
                    ),
                    "code": "actor_mismatch",
                }
            )
            await websocket.close(code=1008)
            return None
        # Source of truth: JWT. Never the supplied value, even when they're
        # identical — keeps the security invariant load-bearing in code, not
        # just in comments.
        effective_actor_id = authenticated_actor_id
    else:
        # `associate_self` (default for public surfaces / anonymous users) —
        # the agent acts AS the associate with its own permissions. Supplied
        # actor_id is ignored entirely. JWT only proved the caller is
        # authenticated; the JWT's actor_id is irrelevant.
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
        validation_warnings=validation_warnings,
    )
    # Register BEFORE start() — matches original (pre-AI-408) pattern so the
    # `finally: _sessions.pop(...)` cleanup fires even when start() raises
    # mid-way. Legacy path does the same just below.
    _sessions[id(websocket)] = session
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
            # in Tasks 3.2-3.7. _start_deployment_session registers the session
            # in _sessions BEFORE calling session.start() (matches the legacy
            # path's pre-AI-408 ordering — cleanup fires even if start raises).
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
            # Register BEFORE start() — pre-AI-408 pattern preserved.
            _sessions[id(websocket)] = session
            await session.start()

        # AI-408 Task 3.6 follow-up: surface forgiving-mode parameter_schema
        # warnings to the client per plan §3.6. Always include the field
        # (empty list when no warnings) so SDK consumers can iterate
        # without null-checking. Legacy path's ChatSession defaults to [].
        await websocket.send_json(
            {
                "type": "connected",
                "interaction_id": session.interaction_id,
                "validation_warnings": session.validation_warnings,
            }
        )

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
