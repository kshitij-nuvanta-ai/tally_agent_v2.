"""The v2 cloud API's single error shape (S1 spec §11): ``{"error": code, "detail": detail, **extra}``."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Raised anywhere in v2/cloud to produce a JSON error response with a stable ``code``."""

    def __init__(self, status: int, code: str, detail: str = "", **extra):
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def install_error_handler(app: FastAPI) -> None:
    """Register the ``ApiError`` -> JSON handler. A ``retry_after`` extra also sets the ``Retry-After`` header."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        body = {"error": exc.code, "detail": exc.detail, **exc.extra}
        headers = {"Retry-After": str(exc.extra["retry_after"])} if "retry_after" in exc.extra else None
        return JSONResponse(status_code=exc.status, content=body, headers=headers)

    @app.exception_handler(DBAPIError)
    async def _handle_db_error(request: Request, exc: DBAPIError) -> JSONResponse:
        # S1 review I1: the engine hides bound parameters, but the driver's own message can still quote a value
        # (asyncpg: "invalid input for query argument $1: '<narration>'"). An unhandled DB error would reach
        # uvicorn's traceback log, so answer 500 here and log the exception *classes* only — never str(exc).
        # A 500 is always retried by the agent (§11), so no stable code beyond "internal_error" is needed.
        log.error("v2 db error on %s %s: %s/%s", request.method, request.url.path,
                  type(exc).__name__, type(exc.orig).__name__ if exc.orig is not None else "-")
        return JSONResponse(status_code=500, content={"error": "internal_error", "detail": ""})
