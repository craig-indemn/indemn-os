"""Standardized error responses for the API."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from kernel.entity.save import VersionConflictError
from kernel.entity.state_machine import StateMachineError, TransitionValidationError


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
