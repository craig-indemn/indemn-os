"""POST /sessions handler for the voice frontdoor (AI-407 §10.3.1).

This module owns the full /sessions request lifecycle. The handler is
built up incrementally across Tasks 2.25–2.36:

1. Task 2.25 — skeleton (501 Not Implemented; route registered)
2. Task 2.26 — body parse + 400 on malformed JSON / missing deployment_id
3. Task 2.27 — Origin allowlist check (403 origin_not_allowed)
4. Task 2.28 — JWT RS256 validation (Authorization: Bearer) — 401 family
5. Task 2.29 — Deployment load + status check (404, 409 deployment_not_active)
6. Task 2.30 — dynamic_params JSON Schema validation (400 validation_error)
7. Task 2.31 — acts_as security gate (403 actor_mismatch)
8. Task 2.32 — Interaction creation
9. Task 2.32.5 — integration test frontdoor → worker handoff
10. Task 2.33 — LiveKit room + AgentDispatch + token mint
11. Task 2.34 — 200 success response with try/except 500-with-request_id
12. Task 2.35 — resume flow (resume_interaction_id; TTL + identity check
    + kill_on_resume helper)
13. Task 2.36 — rate-limit BEFORE LiveKit dispatch

Validation order matters (§10.3.1):
- Body parse first (cheap; rejects malformed without doing any work)
- Deployment loaded BEFORE Origin check (Origin compares against
  Deployment.allowed_origins — can't validate without the Deployment).
  Design enumeration shows Origin (step 2) before Deployment-load (step 4)
  as a CONCEPTUAL order — execution loads Deployment first because Origin
  depends on it. The validation OUTCOME is identical.
- Rate-limit (§10.7) — MUST fire BEFORE LiveKit room creation +
  Interaction creation — otherwise an attacker exhausts LiveKit room slots
  + creates audit-trail Interactions before being throttled. Implementation
  places rate-limit BEFORE step 10 in execution order, even though it's
  "step 9" in design enumeration — design intent: rate-limit gates
  dispatch, not validation.

Wrap steps 10–11 in try/except to catch LiveKit/Interaction failures and
return 500 with request_id per §10.3.1 status table.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
import jsonschema
import jwt as pyjwt
from jsonschema.validators import Draft202012Validator
from starlette.requests import Request
from starlette.responses import JSONResponse

from harness import jwt_auth
from harness.rate_limit import RateLimiter

log = logging.getLogger(__name__)


# Module-level singleton — survives request scope so the sliding-window
# state accumulates across requests. Per §10.7: per-IP / per-actor /
# per-Deployment limits. Defaults sized for an internal-team sales
# surface; switch to Redis-backed limiter when the frontdoor scales
# beyond one container.
_rate_limiter = RateLimiter()


class DeploymentNotFound(Exception):
    """Raised by _load_deployment when the OS API returns 404."""

    def __init__(self, deployment_id: str):
        super().__init__(f"Deployment not found: {deployment_id}")
        self.deployment_id = deployment_id


def _validation_error(details: str) -> JSONResponse:
    """400 response per §10.3.1 error table — malformed input."""
    return JSONResponse(
        {"error": "validation_error", "details": details},
        status_code=400,
    )


def _forbidden(reason: str) -> JSONResponse:
    """403 response per §10.3.1 error table."""
    return JSONResponse(
        {"error": "forbidden", "reason": reason}, status_code=403
    )


def _not_found(resource: str) -> JSONResponse:
    """404 response per §10.3.1 error table."""
    return JSONResponse(
        {"error": "not_found", "resource": resource}, status_code=404
    )


def _unauthorized(reason: str) -> JSONResponse:
    """401 response per §10.3.1 error table.

    `reason` is one of: missing, invalid, expired (per §10.3.1). The
    finer-grained `wrong_audience` / `wrong_issuer` are folded into
    `invalid` because pyjwt's audience / issuer errors are subclasses of
    PyJWTError and a leaking token is a leaking token — the SDK's only
    actionable response is "re-mint a token" either way.
    """
    return JSONResponse(
        {"error": "unauthorized", "reason": reason}, status_code=401
    )


async def _load_deployment(deployment_id: str) -> dict:
    """Load the Deployment record from the OS API.

    Uses the public-metadata endpoint `/api/deployments/{id}/public` per
    §15.1 — returns the surface-safe field subset (allowed_origins,
    parameter_schema, acts_as, status, runtime_endpoint, etc.). No auth
    required (Deployment ID is semi-public per §10.7 — embed snippets on
    customer sites necessarily expose it).

    Raises DeploymentNotFound on 404; raises httpx.HTTPError on other
    failures (caller can wrap with try/except for 500).
    """
    api_url = os.environ.get("INDEMN_API_URL", "http://localhost:8000")
    url = f"{api_url.rstrip('/')}/api/deployments/{deployment_id}/public"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
    if resp.status_code == 404:
        raise DeploymentNotFound(deployment_id)
    resp.raise_for_status()
    return resp.json()


def _validate_parameters(
    deployment: dict, dynamic_params: dict
) -> tuple[dict, list[str]]:
    """Validate dynamic_params against Deployment.parameter_schema per §5.4.

    Validation is on the MERGED static+dynamic set (per §5.4 "the schema
    describes the union"). Returns (merged_context, validation_warnings):
    - merged_context: static_parameters + dynamic_params (dynamic wins on
      key collision)
    - validation_warnings: list of jsonschema error messages, empty if
      validation passed

    Caller decides what to do with non-empty warnings based on
    `Deployment.parameter_schema_validation_mode` (`strict` → 400;
    `forgiving` → log + proceed).

    Raises jsonschema.SchemaError if the schema itself is malformed —
    caller catches + returns 400.
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
    # time (Task 1.9 + Track 13e), but legacy records or out-of-band
    # writes could land malformed schemas; we'd rather 400 here than 500.
    Draft202012Validator.check_schema(schema)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(merged), key=lambda e: e.path)
    warnings = [_format_jsonschema_error(e) for e in errors]
    return merged, warnings


def _format_jsonschema_error(error: jsonschema.ValidationError) -> str:
    """Render a single ValidationError as `<path>: <message>` so the SDK
    can show the user which field failed. Includes the absolute path
    when present (e.g., `actor_id: 'has-hyphens' does not match pattern ...`)
    and falls back to the message alone for top-level errors (e.g.,
    `'actor_id' is a required property`).
    """
    if error.absolute_path:
        path = ".".join(str(p) for p in error.absolute_path)
        return f"{path}: {error.message}"
    return error.message


async def _create_lk_room_and_dispatch(
    deployment_id: str,
    interaction_id: str,
    dynamic_params: dict,
    correlation_id: str,
    *,
    agent_name: str = "voice-deepagents",
) -> dict:
    """Create the LiveKit room + dispatch the worker + mint participant token.

    Per §10.3.1:
    - Room name: `dep-{deployment_id}-int-{interaction_id}` (deterministic
      so the worker can derive interaction_id from the room name as a
      backup if metadata is corrupted; also makes resume + kill-prior
      mechanics in Task 2.35 simple).
    - Room metadata: JSON-serialized `{deployment_id, interaction_id,
      dynamic_params, correlation_id}`. **No credentials.** Per §10.6 +
      §10.7 — room metadata is visible to every participant per
      LiveKit's protocol; tokens or service secrets here would leak.
    - empty_timeout=1800 (30 min) auto-closes the room if no
      participants connect.
    - max-duration 4hr (design §10.3.1) — NOT enforced at the SDK level
      because livekit-api v1.x CreateRoomRequest has no max_duration
      field (verified via `[f.name for f in CreateRoomRequest.DESCRIPTOR
      .fields]`). Operational eviction or worker-level wall-clock check
      is the v1 mechanism; reassess when SDK adds the field.
    - max_participants=2 (one user + the agent worker).
    - AgentDispatch via `agent_dispatch.create_dispatch(...)` —
      `create_dispatch` is the SDK method name (NOT `create_agent_dispatch`
      as some docs read; verified against installed
      `livekit.api.agent_dispatch_service.AgentDispatchService`).
    - Participant token: short-lived JWT scoped to this room with
      room_join + can_publish + can_subscribe grants. Returned to the
      SDK so the browser's LiveKit client can join.

    Returns: dict with `room_name`, `livekit_url`, `livekit_token`.

    Caller wraps in try/except for §10.3.1 step-10 error handling — a
    LiveKit transport failure is a 500-with-request_id (Task 2.34) so
    the SDK + operator can grep logs.
    """
    # Lazy import — LiveKit SDK is heavy + adds boot time; only load
    # when the helper is actually invoked (most tests stub the helper).
    from livekit.api import LiveKitAPI, AccessToken, VideoGrants
    from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest
    from livekit.protocol.room import CreateRoomRequest

    room_name = f"dep-{deployment_id}-int-{interaction_id}"
    room_metadata = {
        "deployment_id": str(deployment_id),
        "interaction_id": str(interaction_id),
        "dynamic_params": dynamic_params,
        "correlation_id": correlation_id,
    }
    room_metadata_json = json.dumps(room_metadata)

    livekit_url = os.environ["LIVEKIT_URL"]
    livekit_api = LiveKitAPI(
        url=livekit_url,
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        await livekit_api.room.create_room(
            CreateRoomRequest(
                name=room_name,
                empty_timeout=1800,  # 30 min — auto-close idle rooms
                max_participants=2,  # user + agent
                metadata=room_metadata_json,
            )
        )
        await livekit_api.agent_dispatch.create_dispatch(
            CreateAgentDispatchRequest(
                agent_name=agent_name,
                room=room_name,
                metadata=room_metadata_json,
            )
        )
    finally:
        # LiveKitAPI holds an aiohttp session; close it explicitly to
        # avoid the noisy "Unclosed client session" warnings in logs.
        aclose = getattr(livekit_api, "aclose", None)
        if aclose is not None:
            await aclose()

    token = (
        AccessToken(
            os.environ["LIVEKIT_API_KEY"],
            os.environ["LIVEKIT_API_SECRET"],
        )
        .with_identity(f"user-{interaction_id}")
        .with_grants(
            VideoGrants(
                room=room_name,
                room_join=True,
                can_publish=True,
                can_subscribe=True,
            )
        )
    )
    return {
        "room_name": room_name,
        "livekit_url": livekit_url,
        "livekit_token": token.to_jwt(),
    }


class InteractionNotFound(Exception):
    """Raised by _load_interaction when the OS API returns 404."""

    def __init__(self, interaction_id: str):
        super().__init__(f"Interaction not found: {interaction_id}")
        self.interaction_id = interaction_id


async def _load_interaction(interaction_id: str) -> dict:
    """Load an Interaction record via the OS API (for resume flow).

    GET {INDEMN_API_URL}/api/interactions/{id} with INDEMN_SERVICE_TOKEN.
    Raises InteractionNotFound on 404; raises httpx.HTTPError on other
    failures (caller wraps with try/except).
    """
    api_url = os.environ.get("INDEMN_API_URL", "http://localhost:8000")
    service_token = os.environ.get("INDEMN_SERVICE_TOKEN", "")
    url = f"{api_url.rstrip('/')}/api/interactions/{interaction_id}"
    headers = {"Authorization": f"Bearer {service_token}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code == 404:
        raise InteractionNotFound(interaction_id)
    resp.raise_for_status()
    return resp.json()


def _interaction_age_seconds(interaction: dict) -> float:
    """How many seconds ago was the Interaction created? Handles three
    representations of created_at:
    - datetime (Python object)
    - ISO 8601 string (production API response format)
    - Unix timestamp (float / int — common in test fixtures)

    Returns a non-negative float (clamped to 0 if created_at is somehow
    in the future).
    """
    created = interaction.get("created_at")
    if created is None:
        return float("inf")  # missing field — treat as expired
    if isinstance(created, datetime):
        epoch = created.replace(tzinfo=timezone.utc).timestamp() if created.tzinfo is None else created.timestamp()
    elif isinstance(created, (int, float)):
        epoch = float(created)
    elif isinstance(created, str):
        # ISO 8601 with optional trailing Z (Pydantic + Mongo serialize as ISO)
        normalized = created.replace("Z", "+00:00") if created.endswith("Z") else created
        epoch = datetime.fromisoformat(normalized).timestamp()
    else:
        return float("inf")
    return max(0.0, time.time() - epoch)


def _is_interaction_expired(interaction: dict, resumption_config: dict) -> bool:
    """Per §12.4 step 12: an Interaction is past TTL if
    `now - created_at > Deployment.resumption_config.ttl_seconds`.
    Default ttl_seconds=86400 (24h) when the config omits the field.
    """
    ttl = (resumption_config or {}).get("ttl_seconds", 86400)
    return _interaction_age_seconds(interaction) > ttl


async def _kill_prior_room(interaction: dict) -> None:
    """Disconnect any agent participant in the prior LiveKit room for
    this Interaction (per §12.4 step 12 + kill_on_resume).

    Room name is deterministic: `dep-{deployment_id}-int-{interaction_id}`.
    If the room doesn't exist (prior worker already cleaned up via
    Attention TTL), LiveKit returns NOT_FOUND — treat as success +
    proceed with resume.

    Best-effort: if the prior worker is mid-response, LiveKit's
    disconnect signal may arrive after a TTS chunk; the new worker
    still wins because the resume returns new connection creds and the
    prior room is abandoned. Errors are logged + swallowed.
    """
    # Lazy import — LiveKit SDK is heavy; resume path only.
    from livekit.api import LiveKitAPI
    from livekit.protocol.room import (
        ListParticipantsRequest,
        RoomParticipantIdentity,
    )

    deployment_id = str(interaction.get("deployment_id"))
    interaction_id = str(interaction.get("_id"))
    room_name = f"dep-{deployment_id}-int-{interaction_id}"

    livekit_api = LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        participants = await livekit_api.room.list_participants(
            ListParticipantsRequest(room=room_name)
        )
        for p in participants.participants:
            # Worker identity is `user-{interaction_id}` for the user
            # token, and the agent's own identity (set by LiveKit
            # Agents framework) starts with `voice-deepagents` or
            # `agent-`. Kill only the agent participant.
            if p.identity.startswith(("voice-deepagents", "agent-")):
                await livekit_api.room.remove_participant(
                    RoomParticipantIdentity(
                        room=room_name, identity=p.identity
                    )
                )
                log.info(
                    "Disconnected prior agent %s from room %s",
                    p.identity,
                    room_name,
                )
    except Exception as e:
        # Common case: room doesn't exist anymore (prior worker
        # cleaned up); other transient LiveKit errors. Either way,
        # resume proceeds — the new worker's checkpointer state will
        # win on next state write.
        log.warning(
            "Could not kill prior room %s: %s — proceeding with resume",
            room_name,
            e,
        )
    finally:
        aclose = getattr(livekit_api, "aclose", None)
        if aclose is not None:
            await aclose()


async def _mark_interaction_failed(interaction_id: str, reason: str) -> None:
    """Best-effort cleanup: transition the orphaned Interaction to a
    failed state when LiveKit dispatch fails after Interaction creation
    succeeded. Without this, we'd leak Interactions stuck at
    status=active whose rooms never spawned, polluting analytics +
    confusing the resume flow.

    Best-effort by design: a failure here is logged but does NOT change
    the 500 response shape — the client already knows the session
    failed; the Interaction record is bookkeeping.
    """
    api_url = os.environ.get("INDEMN_API_URL", "http://localhost:8000")
    service_token = os.environ.get("INDEMN_SERVICE_TOKEN", "")
    url = f"{api_url.rstrip('/')}/api/interactions/{interaction_id}/transition"
    payload = {"to": "failed", "reason": reason[:500]}
    headers = {"Authorization": f"Bearer {service_token}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
    resp.raise_for_status()


async def _create_interaction(
    deployment: dict,
    effective_actor_id: str,
    dynamic_params: dict,
) -> dict:
    """Create the Interaction record via the OS API.

    POST {INDEMN_API_URL}/api/interactions/ with the frontdoor's
    INDEMN_SERVICE_TOKEN. Per §10.3.1 + §13:
    - `channel_type = "voice"` (always for this frontdoor)
    - `correlation_id` is freshly minted here — the lineage tracker
      propagated by the worker to every CLI subprocess (via env var per
      §13.7) and to every entity write the agent makes during the
      session.
    - `created_by = effective_actor_id` — the Task 2.31 acts_as gate's
      output. For session_actor it's the JWT.sub; for associate_self
      it's the Associate's own id.
    - `dynamic_params` stored RAW — sanitization (§10.7 layer-c) applies
      only to the <deployment_context> SystemMessage path; the
      Interaction record is for forensics. Downstream consumers (UI,
      analytics) MUST treat dynamic_params values as untrusted input.

    Returns the API's response body (dict containing `_id`,
    `correlation_id`, etc.). Raises httpx.HTTPError on transport
    failures + raise_for_status() converts 4xx/5xx into exceptions —
    callers catch + surface as 500 in the /sessions response per §10.3.1.
    """
    correlation_id = str(uuid.uuid4())
    api_url = os.environ.get("INDEMN_API_URL", "http://localhost:8000")
    service_token = os.environ.get("INDEMN_SERVICE_TOKEN", "")
    url = f"{api_url.rstrip('/')}/api/interactions/"
    payload = {
        "channel_type": "voice",
        "deployment_id": str(deployment.get("_id")),
        "correlation_id": correlation_id,
        "created_by": effective_actor_id,
        "status": "active",
        "dynamic_params": dynamic_params,
    }
    headers = {"Authorization": f"Bearer {service_token}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()


def _origin_allowed(origin: str | None, allowed_origins: list[str]) -> bool:
    """Return True iff `origin` is in `allowed_origins`.

    Per §5.1: empty allowed_origins = reject all. Missing Origin header
    also rejects (can't match what's absent).

    Case-sensitive — Origin headers are case-sensitive per RFC 6454.
    """
    if not origin:
        return False
    if not allowed_origins:
        return False
    return origin in allowed_origins


async def create_session(request: Request) -> JSONResponse:
    """POST /sessions handler. Validation chain per §10.3.1.

    Current state (Task 2.26): body-parse + required-fields validation.
    Subsequent tasks fill: Origin allowlist (2.27), JWT (2.28), Deployment
    load + status (2.29), parameter_schema (2.30), acts_as (2.31), resume
    (2.35), rate-limit (2.36), Interaction (2.32), LiveKit dispatch
    (2.33), success response (2.34).
    """
    # Step 1: parse JSON body
    try:
        raw = await request.body()
    except Exception as e:
        log.warning("Failed to read request body: %s", e)
        return _validation_error("Failed to read request body")

    if not raw:
        return _validation_error("Request body is empty; expected JSON object")

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        return _validation_error(f"Malformed JSON: {e}")

    if not isinstance(body, dict):
        return _validation_error(
            "Request body must be a JSON object (got "
            f"{type(body).__name__})"
        )

    # Step 2: required field — deployment_id
    deployment_id = body.get("deployment_id")
    if not deployment_id or not isinstance(deployment_id, str):
        return _validation_error(
            "Missing or invalid required field 'deployment_id' "
            "(expected non-empty string)"
        )

    # Step 3 (conceptual §10.3.1 step 2 + step 4): load Deployment first,
    # then check Origin. Per the §10.3.1 note: design enumerates Origin
    # before Deployment-load as conceptual ordering, but execution must
    # load the Deployment first because Origin compares against
    # deployment.allowed_origins. Outcome is identical (invalid origin →
    # 403; missing deployment → 404).
    try:
        deployment = await _load_deployment(deployment_id)
    except DeploymentNotFound:
        return _not_found("deployment")
    except Exception as e:
        # Upstream OS API unreachable / 5xx — return 500 with request_id.
        # Task 2.34 will formalize the request_id generation; until then
        # log + return a generic 500 so tests don't trip on the bare
        # exception.
        log.exception("Failed to load Deployment %s: %s", deployment_id, e)
        return JSONResponse(
            {"error": "internal", "details": "failed to load deployment"},
            status_code=500,
        )

    # Step 4: Origin allowlist check per §5.1 + §10.7
    origin = request.headers.get("origin")
    allowed_origins = deployment.get("allowed_origins") or []
    if not _origin_allowed(origin, allowed_origins):
        log.info(
            "Rejecting session (origin %r not in allowlist %r for deployment %s)",
            origin,
            allowed_origins,
            deployment_id,
        )
        return _forbidden("origin_not_allowed")

    # Step 5: JWT validation per §10.3.1 step 3 + §10.6
    # Authorization: Bearer <token>; RS256 with public key from AWS Secrets
    # (`indemn/dev/shared/jwt-public-key`). Required claims: sub, org_id,
    # exp, iss == "indemn-os", aud contains "runtime-voice-frontdoor".
    # 60s clock-skew tolerance.
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return _unauthorized("missing")
    token = auth_header[len("Bearer "):]
    try:
        claims = jwt_auth.verify_jwt(token)
    except pyjwt.ExpiredSignatureError:
        return _unauthorized("expired")
    except pyjwt.PyJWTError as e:
        log.info("Rejecting session (JWT validation failed: %s)", e)
        return _unauthorized("invalid")

    authenticated_actor_id = claims["sub"]
    log.debug(
        "JWT validated for actor %s on deployment %s",
        authenticated_actor_id,
        deployment_id,
    )

    # Step 6: Deployment status check per §10.3.1 step 5 + §5.7 state
    # machine. Only `active` accepts sessions; configured/paused/archived/
    # error reject. The SDK surfaces `status` so the user-facing message
    # can be specific ("temporarily paused" vs generic "unavailable").
    deployment_status = deployment.get("status")
    if deployment_status != "active":
        log.info(
            "Rejecting session for deployment %s (status=%r, expected active)",
            deployment_id,
            deployment_status,
        )
        return JSONResponse(
            {"error": "deployment_not_active", "status": deployment_status},
            status_code=409,
        )

    # Step 7: dynamic_params validation per §10.3.1 step 6 + §5.4.
    # Validate the MERGED static+dynamic set against parameter_schema
    # (JSON Schema Draft 2020-12). Strict mode (default for session_actor)
    # rejects with 400; forgiving mode (default for associate_self) logs +
    # proceeds with warnings attached to the 200 response (Task 2.34).
    dynamic_params = body.get("dynamic_params") or {}
    if not isinstance(dynamic_params, dict):
        return _validation_error(
            "Field 'dynamic_params' must be a JSON object"
        )
    try:
        merged_context, validation_warnings = _validate_parameters(
            deployment, dynamic_params
        )
    except jsonschema.SchemaError as e:
        # Malformed parameter_schema on the Deployment record itself.
        # Save-time validation (Task 1.9 + Track 13e) should prevent
        # this, but legacy records or out-of-band writes could land bad
        # schemas. Surface as 400 with the schema-error path rather than
        # 500-crashing.
        log.warning(
            "Deployment %s has malformed parameter_schema: %s",
            deployment_id,
            e,
        )
        return _validation_error(
            f"Deployment parameter_schema is invalid: {e.message}"
        )

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
            return _validation_error("; ".join(validation_warnings))
        # forgiving — log + proceed; warnings surfaced in Task 2.34's
        # success response shape
        log.info(
            "Forgiving-mode validation warnings on deployment %s: %s",
            deployment_id,
            validation_warnings,
        )

    # Step 8: acts_as security gate per §10.3.1 step 7 + §5.6 + §10.7.
    # LOAD-BEARING — this is the gate that makes the session_actor
    # capability safe. JWT IS the source of truth for effective_actor_id;
    # dynamic_params.actor_id is consulted ONLY for the mismatch check.
    # Code review verifies the gate is right by reading the
    # `effective_actor_id = authenticated_actor_id` line.
    acts_as = deployment.get("acts_as")
    if acts_as == "session_actor":
        supplied_actor_id = dynamic_params.get("actor_id")
        # `is not None` (not truthy-check) — empty string / 0 / False
        # still count as "supplied" and must match. Schema validation
        # earlier rejects most malformed cases; this is defense-in-depth
        # in case a Deployment's schema doesn't enforce the type.
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
            return _forbidden("actor_mismatch")
        # Source of truth: JWT. Never the supplied value, even when
        # they're identical — keeps the security invariant load-bearing
        # in code, not just in comments.
        effective_actor_id = authenticated_actor_id
    else:
        # associate_self (default for public surfaces / anonymous users)
        # — the agent acts AS the associate with its own permissions.
        # Supplied actor_id is ignored entirely. JWT only proved the
        # caller is authenticated; the JWT's actor_id is irrelevant.
        effective_actor_id = str(deployment.get("associate_id"))

    request_id = str(uuid.uuid4())

    # Step 8: Rate-limit per §10.3.1 step 9 + §10.7 row "Replay of
    # session creation". MUST fire BEFORE Interaction creation +
    # LiveKit room creation (whether fresh OR resume) — otherwise an
    # attacker exhausts LiveKit room slots + writes Interaction
    # audit-trail records before being throttled.
    client_ip = request.client.host if request.client else "unknown"
    rl_result = _rate_limiter.check_with_retry(
        ip=client_ip,
        actor=effective_actor_id,
        deployment=deployment_id,
    )
    if not rl_result["allowed"]:
        log.info(
            "Rate-limited /sessions request — scope=%s, ip=%s, "
            "actor=%s, deployment=%s",
            rl_result["scope"],
            client_ip,
            effective_actor_id,
            deployment_id,
        )
        retry_after = rl_result["retry_after_seconds"]
        return JSONResponse(
            {
                "error": "rate_limited",
                "retry_after_seconds": retry_after,
                "scope": rl_result["scope"],
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    # Step 8.5: Resume branch per §10.3.1 step 8 + §12.4 step 12.
    # If `resume_interaction_id` is present, load the prior Interaction
    # + verify ownership + TTL + status, then optionally kill the prior
    # worker before dispatching a new one with the SAME interaction_id.
    # No new Interaction created on resume — the existing one carries
    # forward.
    resume_interaction_id = body.get("resume_interaction_id")
    if resume_interaction_id:
        try:
            interaction = await _load_interaction(resume_interaction_id)
        except InteractionNotFound:
            return _not_found("interaction")
        except Exception:
            log.exception(
                "Failed to load resume Interaction %s (request_id=%s)",
                resume_interaction_id,
                request_id,
            )
            return JSONResponse(
                {
                    "error": "internal",
                    "request_id": request_id,
                    "stage": "resume_load",
                },
                status_code=500,
            )

        # Resumption hijacking prevention per §10.7 — only the original
        # caller can resume. Compare to the JWT's actor (NOT the
        # supplied actor_id; the acts_as gate's load-bearing invariant
        # holds here too).
        if interaction.get("created_by") != authenticated_actor_id:
            log.warning(
                "Resumption hijacking attempt rejected — "
                "JWT.sub=%r, interaction.created_by=%r, interaction=%s",
                authenticated_actor_id,
                interaction.get("created_by"),
                resume_interaction_id,
            )
            return _forbidden("actor_mismatch")

        # TTL check per §12.4: now - created_at > ttl_seconds → 410
        resumption_config = deployment.get("resumption_config") or {}
        if _is_interaction_expired(interaction, resumption_config):
            log.info(
                "Resume rejected — Interaction %s past TTL %ss",
                resume_interaction_id,
                resumption_config.get("ttl_seconds", 86400),
            )
            return JSONResponse(
                {"error": "resume_expired"}, status_code=410
            )

        # Status check — closed/archived Interactions are permanently
        # terminal and not resumable
        if interaction.get("status") in ("closed", "archived"):
            log.info(
                "Resume rejected — Interaction %s is terminal (status=%s)",
                resume_interaction_id,
                interaction.get("status"),
            )
            return JSONResponse(
                {
                    "error": "resume_expired",
                    "reason": "closed",
                },
                status_code=410,
            )

        # Kill prior worker if kill_on_resume=true (default per §12.4)
        if resumption_config.get("kill_on_resume", True):
            try:
                await _kill_prior_room(interaction)
            except Exception:
                # _kill_prior_room is itself best-effort + already
                # logs; this extra try/except defends against
                # importpath-style failures that would crash the resume.
                log.exception(
                    "kill_prior_room failed (request_id=%s, "
                    "interaction=%s) — proceeding with resume anyway",
                    request_id,
                    resume_interaction_id,
                )

        # Dispatch new LiveKit room + worker with SAME interaction_id
        interaction_id = interaction["_id"]
        correlation_id = interaction["correlation_id"]
        try:
            lk_result = await _create_lk_room_and_dispatch(
                deployment_id=deployment_id,
                interaction_id=interaction_id,
                dynamic_params=dynamic_params,
                correlation_id=correlation_id,
            )
        except Exception:
            log.exception(
                "LiveKit dispatch failed on resume "
                "(request_id=%s, interaction=%s)",
                request_id,
                resume_interaction_id,
            )
            # No Interaction cleanup on resume — the original
            # Interaction was already alive; resume just opens a new
            # room. Failure here leaves Interaction in its prior state.
            return JSONResponse(
                {
                    "error": "internal",
                    "request_id": request_id,
                    "stage": "livekit",
                },
                status_code=500,
            )

        return JSONResponse(
            {
                "room_name": lk_result["room_name"],
                "livekit_url": lk_result["livekit_url"],
                "livekit_token": lk_result["livekit_token"],
                "interaction_id": interaction_id,
            },
            status_code=200,
        )

    # Steps 9-10: fresh session — Interaction creation + LiveKit
    # dispatch wrapped in try/except per §10.3.1 status-500 contract.
    try:
        interaction = await _create_interaction(
            deployment=deployment,
            effective_actor_id=effective_actor_id,
            dynamic_params=dynamic_params,
        )
    except Exception:
        log.exception(
            "Interaction creation failed (request_id=%s, deployment=%s)",
            request_id,
            deployment_id,
        )
        return JSONResponse(
            {
                "error": "internal",
                "request_id": request_id,
                "stage": "interaction",
            },
            status_code=500,
        )

    interaction_id = interaction["_id"]
    correlation_id = interaction["correlation_id"]

    try:
        lk_result = await _create_lk_room_and_dispatch(
            deployment_id=deployment_id,
            interaction_id=interaction_id,
            dynamic_params=dynamic_params,
            correlation_id=correlation_id,
        )
    except Exception as e:
        log.exception(
            "LiveKit dispatch failed (request_id=%s, deployment=%s, "
            "interaction=%s)",
            request_id,
            deployment_id,
            interaction_id,
        )
        # Best-effort cleanup: don't leak orphaned `active` Interactions
        # whose room never spawned. Cleanup failure is logged but does
        # NOT change the 500 response shape.
        try:
            await _mark_interaction_failed(
                interaction_id, reason=f"LiveKit dispatch failed: {e}"
            )
        except Exception:
            log.exception(
                "Cleanup of orphaned Interaction %s failed (request_id=%s)",
                interaction_id,
                request_id,
            )
        return JSONResponse(
            {
                "error": "internal",
                "request_id": request_id,
                "stage": "livekit",
            },
            status_code=500,
        )

    # Success — §10.3.1 contract, exactly 4 fields. No leaked internals
    # (authenticated_actor_id / effective_actor_id / correlation_id /
    # validation_warnings stay server-side; the SDK has what it needs).
    return JSONResponse(
        {
            "room_name": lk_result["room_name"],
            "livekit_url": lk_result["livekit_url"],
            "livekit_token": lk_result["livekit_token"],
            "interaction_id": interaction_id,
        },
        status_code=200,
    )
