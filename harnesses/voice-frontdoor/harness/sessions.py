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

import httpx
import jwt as pyjwt
from starlette.requests import Request
from starlette.responses import JSONResponse

from harness import jwt_auth

log = logging.getLogger(__name__)


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

    # Subsequent validation chain to be filled in Tasks 2.29–2.36.
    # Until then, return 501 (parsing + origin + JWT passed but
    # status/parameter_schema/acts_as/etc not wired).
    return JSONResponse(
        {
            "error": "not_implemented",
            "deployment_id": deployment_id,
            "authenticated_actor_id": authenticated_actor_id,
        },
        status_code=501,
    )
