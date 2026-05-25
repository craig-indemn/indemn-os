"""Voice frontdoor — Starlette HTTP application.

Per design §10.3 + §10.3.1. Two-Railway-services model: this service mints
LiveKit access tokens + dispatches workers; the worker service handles
per-room agent jobs.

Routes added incrementally per the playbook task sequence:
- /health (Task 2.24 — this task)
- POST /sessions (Task 2.25 skeleton, Task 2.26+ progressively fills in
  body parse / Origin / JWT / params validation / LiveKit dispatch / response)
"""

import logging
import os

from starlette.applications import Starlette
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

app = Starlette(debug=False, routes=routes)
