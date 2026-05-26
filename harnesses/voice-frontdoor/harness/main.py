"""voice-frontdoor entry point — runs uvicorn against the Starlette app.

Production deployment: Railway runs `python -m harness.main` per Dockerfile
CMD. PORT env var supplied by Railway (Railway-internal load balancer
expects the configured port).
"""

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    log_level = os.environ.get("UVICORN_LOG_LEVEL", "info")
    uvicorn.run(
        "harness.app:app",
        host=host,
        port=port,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()
