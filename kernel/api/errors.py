"""Standardized error responses for the API.

Bug surfaced 2026-04-27 (Alliance trace, plus Bugs #25 / #26 from the bug
register): any exception not in the small set of typed handlers below fell
through to FastAPI's default and returned a literal `"Internal Server
Error"` text body with no detail. That made the entire create flow opaque
— autonomous associates and humans had no way to self-correct after a
500. The catch-all and Pydantic ValidationError handlers fix that: every
unhandled exception now returns a structured JSON body with the exception
type and message. Typed handlers still take precedence.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from kernel.entity.save import VersionConflictError
from kernel.entity.state_machine import StateMachineError, TransitionValidationError
from kernel.integration.adapter import AdapterValidationError

logger = logging.getLogger(__name__)

_MAX_500_MESSAGE_CHARS = 4096
_TRUNCATION_SUFFIX = "...[truncated]"


def register_error_handlers(app: FastAPI):
    """Register consistent error handlers for all known exception types."""

    @app.exception_handler(StateMachineError)
    async def state_machine_error(request: Request, exc: StateMachineError):
        return JSONResponse(
            status_code=400,
            content={"error": "StateMachineError", "message": str(exc)},
        )

    @app.exception_handler(TransitionValidationError)
    async def transition_validation_error(request: Request, exc: TransitionValidationError):
        return JSONResponse(
            status_code=400,
            content={"error": "TransitionValidationError", "message": str(exc)},
        )

    @app.exception_handler(VersionConflictError)
    async def version_conflict_error(request: Request, exc: VersionConflictError):
        return JSONResponse(
            status_code=409,
            content={"error": "VersionConflict", "message": str(exc)},
        )

    @app.exception_handler(PermissionError)
    async def permission_error(request: Request, exc: PermissionError):
        return JSONResponse(
            status_code=403,
            content={"error": "PermissionDenied", "message": str(exc)},
        )

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError):
        return JSONResponse(
            status_code=400,
            content={"error": "ValidationError", "message": str(exc)},
        )

    @app.exception_handler(AdapterValidationError)
    async def adapter_validation_error(request: Request, exc: AdapterValidationError):
        # Operator passed a bad/unknown param to an adapter (Bug #36 made this
        # a real failure mode). 400 Bad Request — actionable for the caller.
        return JSONResponse(
            status_code=400,
            content={"error": "AdapterValidationError", "message": str(exc)},
        )

    @app.exception_handler(PydanticValidationError)
    async def pydantic_validation_error(request: Request, exc: PydanticValidationError):
        # Field-level structure: each error has loc/msg/type. Surface that
        # so callers can map errors back to specific fields.
        errors = []
        for err in exc.errors():
            errors.append(
                {
                    "loc": list(err.get("loc", ())),
                    "msg": err.get("msg", ""),
                    "type": err.get("type", ""),
                }
            )
        return JSONResponse(
            status_code=400,
            content={
                "error": "ValidationError",
                "message": f"{exc.error_count()} validation error(s)",
                "errors": errors,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        # Catch-all: anything not handled above. Log the full exception
        # with traceback for ops, and return a structured body so the
        # caller can self-correct rather than seeing "Internal Server
        # Error" with no detail.
        logger.exception(
            "Unhandled exception in %s %s: %s",
            request.method,
            request.url.path,
            exc,
        )
        message = str(exc)
        if len(message) > _MAX_500_MESSAGE_CHARS:
            keep = _MAX_500_MESSAGE_CHARS - len(_TRUNCATION_SUFFIX)
            message = message[:keep] + _TRUNCATION_SUFFIX
        return JSONResponse(
            status_code=500,
            content={
                "error": "InternalServerError",
                "type": exc.__class__.__name__,
                "message": message,
            },
        )
