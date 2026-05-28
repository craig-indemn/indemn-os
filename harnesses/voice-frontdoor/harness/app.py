"""Voice frontdoor — Starlette HTTP application.

Per design §10.3 + §10.3.1. Two-Railway-services model: this service mints
LiveKit access tokens + dispatches workers; the worker service handles
per-room agent jobs.

Routes added incrementally per the playbook task sequence:
- /health (Task 2.24 — this task)
- POST /sessions (Task 2.25 skeleton, Task 2.26+ progressively fills in
  body parse / Origin / JWT / params validation / LiveKit dispatch / response)

AI-409 smoke fix: CORS middleware so browsers can issue the cross-origin
OPTIONS preflight before POST /sessions. Without it, Starlette returns
405 Method Not Allowed for OPTIONS and the browser refuses to send the
POST. Per-Deployment Origin policy is still enforced server-side at the
POST handler (`_origin_allowed` in sessions.py) — this layer only enables
the browser's CORS handshake.
"""

import logging
import os

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from harness.sessions import create_session

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


async def health(request):
    """Railway health-check endpoint — confirms the frontdoor process is up.

    Does NOT exercise downstream dependencies (Indemn OS API, LiveKit, AWS
    Secrets). Per the pattern of voice-deepagents + chat-deepagents: depth
    health checks happen via per-request paths, not the health endpoint.
    """
    return JSONResponse(
        {"status": "healthy", "service": "indemn-runtime-voice-frontdoor"}
    )


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/sessions", create_session, methods=["POST"]),
]


# Permissive CORS at the middleware layer; per-Deployment Origin check
# still gates the actual POST handler. The set of legitimate origins
# spans every Deployment.allowed_origins entry — that list is dynamic
# (operators add Deployments at runtime) so we can't enumerate it at
# boot time. `allow_origins=["*"]` lets the browser complete the
# preflight handshake; the real security gate (`_origin_allowed` in
# sessions.py) rejects any POST whose Origin is not in that Deployment's
# allowlist. `allow_credentials=False` because the SDK authenticates
# with a Bearer token in the Authorization header, not cookies — which
# also keeps `*` compatible (browsers reject `*` with credentialed
# requests per CORS spec).
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["authorization", "content-type"],
        allow_credentials=False,
        max_age=600,
    ),
]

app = Starlette(debug=False, routes=routes, middleware=middleware)
