"""The v2 cloud API's single error shape (S1 spec §11): ``{"error": code, "detail": detail, **extra}``."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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
