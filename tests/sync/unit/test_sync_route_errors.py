"""``SyncRoute``: what a sync route answers when something goes wrong (review M6; no database needed).

An unexpected exception on a sync route keeps the sync error shape — 500 ``{"error": "internal_error",
"detail": ""}``, logged by exception class and route only. ``ApiError``, ``HTTPException`` and request validation
errors are untouched, and so is every route that is not a sync route (the app's generic handler answers those).
"""
import logging
import traceback

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.main import generic_error_handler
from backend.sync.errors import ApiError, SyncRoute, install_error_handler

SECRET = "NARRATION-Being-paid-to-Sharma-Traders-123456.78"
INTERNAL = {"error": "internal_error", "detail": ""}


class _Unexpected(Exception):
    """Not a database error, not an ``ApiError``, not an HTTP error."""


async def _boom() -> dict:
    raise _Unexpected(SECRET)


async def _failing_dependency() -> None:
    raise KeyError(SECRET)


async def _needs_dependency(_: None = Depends(_failing_dependency)) -> dict:
    return {}


async def _db_boom() -> dict:
    raise DBAPIError("SELECT 1", {}, ValueError(SECRET))


async def _api_error() -> dict:
    raise ApiError(409, "device_conflict", "another device is active", retry_after=7)


async def _http_error() -> dict:
    raise HTTPException(status_code=403, detail="Not your workspace", headers={"X-Why": "test"})


async def _starlette_http_error() -> dict:
    raise StarletteHTTPException(status_code=404, detail="gone")


async def _typed(count: int) -> dict:
    return {"count": count}


ROUTES = {"boom": _boom, "dep": _needs_dependency, "db": _db_boom, "api": _api_error, "http": _http_error,
          "starlette": _starlette_http_error, "typed": _typed}


def _app(route_class) -> FastAPI:
    """An app with the same two handlers the main app registers (``ApiError`` and the generic ``Exception``
    one), and every probe endpoint on a router of the given route class."""
    app = FastAPI()
    install_error_handler(app)
    app.add_exception_handler(Exception, generic_error_handler)
    router = APIRouter(route_class=route_class) if route_class else APIRouter()
    for name, endpoint in ROUTES.items():
        router.add_api_route(f"/api/sync/_{name}", endpoint)
    app.include_router(router)
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path)


def _everything_logged(caplog) -> str:
    return caplog.text + "".join(
        "".join(traceback.format_exception(rec.exc_info[1])) for rec in caplog.records if rec.exc_info)


@pytest.mark.parametrize("name,exc_class", [("boom", "_Unexpected"), ("dep", "KeyError")])
async def test_unexpected_error_on_a_sync_route_keeps_the_sync_error_shape(caplog, name, exc_class):
    """In the endpoint or in one of its dependencies: 500 ``internal_error``, and the log names the exception
    class and the route but never the exception's own text (it can carry business data)."""
    caplog.set_level(logging.DEBUG)
    r = await _get(_app(SyncRoute), f"/api/sync/_{name}")
    assert (r.status_code, r.json()) == (500, INTERNAL)
    logged = _everything_logged(caplog)
    assert SECRET[:20] not in logged and SECRET[:20] not in r.text
    assert f"sync unexpected error on GET /api/sync/_{name}: {exc_class}" in caplog.text
    assert "Unhandled exception" not in caplog.text           # it never reached the app's generic handler
    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(errors) == 1 and errors[0].name == "backend.sync.errors" and not errors[0].exc_info


async def test_database_error_on_a_sync_route_is_logged_as_a_sync_db_error(caplog):
    caplog.set_level(logging.DEBUG)
    r = await _get(_app(SyncRoute), "/api/sync/_db")
    assert (r.status_code, r.json()) == (500, INTERNAL)
    assert "sync db error on GET /api/sync/_db: DBAPIError/ValueError" in caplog.text
    assert "v2 db error" not in caplog.text and "sync unexpected error" not in caplog.text
    assert SECRET[:20] not in _everything_logged(caplog)


async def test_api_error_on_a_sync_route_is_unchanged(caplog):
    caplog.set_level(logging.DEBUG)
    r = await _get(_app(SyncRoute), "/api/sync/_api")
    assert r.status_code == 409 and r.headers["Retry-After"] == "7"
    assert r.json() == {"error": "device_conflict", "detail": "another device is active", "retry_after": 7}
    assert "sync unexpected error" not in caplog.text


@pytest.mark.parametrize("route_class", [SyncRoute, None])
async def test_http_and_validation_errors_answer_the_same_on_sync_and_other_routes(caplog, route_class):
    """``HTTPException`` (FastAPI's and Starlette's) and request validation errors pass through ``SyncRoute``
    untouched: the answer on a sync route is byte-for-byte the answer on a plain route."""
    caplog.set_level(logging.DEBUG)
    app = _app(route_class)

    r = await _get(app, "/api/sync/_http")
    assert (r.status_code, r.json(), r.headers["X-Why"]) == (403, {"detail": "Not your workspace"}, "test")
    r = await _get(app, "/api/sync/_starlette")
    assert (r.status_code, r.json()) == (404, {"detail": "gone"})

    r = await _get(app, "/api/sync/_typed?count=abc")
    assert r.status_code == 422
    (error,) = r.json()["detail"]
    assert error["type"] == "int_parsing" and error["loc"] == ["query", "count"] and "error" not in r.json()
    r = await _get(app, "/api/sync/_typed")
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "missing"
    r = await _get(app, "/api/sync/_typed?count=3")
    assert (r.status_code, r.json()) == (200, {"count": 3})

    assert "sync unexpected error" not in caplog.text and "Unhandled exception" not in caplog.text


@pytest.mark.parametrize("name", ["boom", "dep", "db"])
async def test_routes_that_are_not_sync_routes_are_unaffected(caplog, name):
    """The same failures on a plain router: the app's generic handler answers, exactly as before."""
    caplog.set_level(logging.DEBUG)
    r = await _get(_app(None), f"/api/sync/_{name}")
    assert (r.status_code, r.json()) == (500, {"error": "Internal server error", "detail": None})
    assert "Unhandled exception" in caplog.text
    assert "sync unexpected error" not in caplog.text and "sync db error" not in caplog.text
