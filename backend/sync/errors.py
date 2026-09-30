"""The sync/agent API's single error shape (S1 spec §11): ``{"error": code, "detail": detail, **extra}``.

Two error shapes live on the one app (v2 merge M9): the sync and agent routes answer with this one (it is the
agent's wire contract), every other route keeps its own ``{"detail": ...}``. The split is made per ROUTE, not per
exception type or path prefix:

- ``ApiError`` is raised only by sync code, so its handler is registered on the app (``install_error_handler``).
- A database error can be raised by any route. ``SyncRoute`` — the route class of the four sync routers — turns it
  into this shape for those routes only; on every other route it propagates exactly as before.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import DBAPIError

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Raised anywhere in the sync code to produce a JSON error response with a stable ``code``."""

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


class SyncRoute(APIRoute):
    """Route class of the sync routers (``APIRouter(..., route_class=SyncRoute)``): a database error raised while
    serving one of THEIR routes — in the endpoint or in one of its dependencies — is answered 500
    ``internal_error`` and logged by exception class only.

    S1 review I1: the engine hides bound parameters, but the driver's own message can still quote a value
    (asyncpg: "invalid input for query argument $1: '<narration>'"). An unhandled DB error would reach the
    traceback log, so it is answered here and ``str(exc)`` is never logged. A 500 is always retried by the agent
    (§11), so no stable code beyond "internal_error" is needed.

    It is a route class and not an app-level ``DBAPIError`` handler on purpose (M9): an app-level handler would
    also change what every other route answers on a database error.
    """

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def _db_errors_as_internal_error(request: Request):
            try:
                return await handler(request)
            except DBAPIError as exc:
                log.error("v2 db error on %s %s: %s/%s", request.method, request.url.path,
                          type(exc).__name__, type(exc.orig).__name__ if exc.orig is not None else "-")
                return JSONResponse(status_code=500, content={"error": "internal_error", "detail": ""})

        return _db_errors_as_internal_error
