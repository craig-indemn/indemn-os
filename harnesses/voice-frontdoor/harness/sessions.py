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

from starlette.requests import Request
from starlette.responses import JSONResponse


async def create_session(request: Request) -> JSONResponse:
    """POST /sessions handler. Currently a skeleton (Task 2.25).

    Subsequent tasks fill the validation chain inline; the final shape
    will be a sequence of guards followed by LiveKit dispatch + Interaction
    creation + 200 response.
    """
    return JSONResponse({"error": "not_implemented"}, status_code=501)
