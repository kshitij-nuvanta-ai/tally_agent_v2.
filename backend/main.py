"""FastAPI application — main entry point for the TallyPrime AI Agent."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from backend.config import settings

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL, logging.INFO))
logger = logging.getLogger(__name__)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.agents.context import SessionStore
from backend.api import chat, companies, health, reports, tally, tally_mode
from backend.api.models import ErrorResponse
from backend.config import settings
from backend.sync.clock import SystemClock
from backend.sync.wiring import install_state, install_sync
from tally_bridge.client import TallyClient
from tally_bridge.exceptions import TallyConnectionError, TallyResponseError


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.tally_client = TallyClient(settings.TALLY_HOST, settings.TALLY_PORT)
    app.state.tally_client.mock_mode = settings.TALLY_MODE == "mock"
    app.state.session_store = SessionStore(ttl_minutes=settings.SESSION_TTL_MINUTES)
    # Initialize database if configured
    if settings.db_mode:
        from backend.db.engine import init_engine
        if not settings.JWT_SECRET or len(settings.JWT_SECRET) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters when DATABASE_URL is set")
        if settings.DEVICE_TOKEN_SECRET:
            # Set but unusable (under 32 characters, or equal to JWT_SECRET): refuse to start.
            settings.validate_for_serving()
        else:
            logger.warning(
                "Agent sync is disabled: the sync and agent routes are not served until DEVICE_TOKEN_SECRET is set"
            )
        init_engine()
        logger.info("Database mode enabled (PostgreSQL)")
    if settings.LANGFUSE_PUBLIC_KEY:
        import base64

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        auth = base64.b64encode(
            f"{settings.LANGFUSE_PUBLIC_KEY}:{settings.LANGFUSE_SECRET_KEY}".encode()
        ).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{settings.LANGFUSE_BASE_URL}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
        )
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(exporter))

        from opentelemetry import trace
        trace.set_tracer_provider(provider)

        AnthropicInstrumentor().instrument(tracer_provider=provider)
        app.state.tracer_provider = provider
        logger.info("Langfuse instrumentation enabled (OTLP → %s)", settings.LANGFUSE_BASE_URL)
    yield
    if hasattr(app.state, "tracer_provider"):
        app.state.tracer_provider.shutdown()
    if settings.db_mode:
        from backend.db.engine import close_engine
        await close_engine()
    await app.state.tally_client.close()


async def tally_connection_error_handler(
    request: Request, exc: TallyConnectionError
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=ErrorResponse(error=str(exc)).model_dump(),
    )


async def tally_response_error_handler(
    request: Request, exc: TallyResponseError
) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content=ErrorResponse(error=str(exc)).model_dump(),
    )


async def generic_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(error="Internal server error").model_dump(),
    )


def build_app() -> FastAPI:
    """Assemble the one app from the ``settings`` singleton as it is NOW. Creates no database engine — that
    happens in the lifespan (``init_engine``). Module-level ``app`` below is what uvicorn serves; tests call this
    again after changing ``settings`` to get the app another configuration would serve."""
    app = FastAPI(
        title="TallyPrime AI Agent",
        version="1.0.0",
        lifespan=lifespan,
    )

    cors_origins = (
        settings.CORS_ORIGINS.split(",")
        if settings.CORS_ORIGINS and settings.CORS_ORIGINS != "*"
        else ["*"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(chat.router, prefix="/api")
    app.include_router(health.router, prefix="/api")
    app.include_router(companies.router, prefix="/api")
    app.include_router(reports.router, prefix="/api")
    app.include_router(tally_mode.router, prefix="/api")
    app.include_router(tally.router)  # router already carries the /api/tally prefix

    # DB-mode routers (auth, workspaces, conversations, usage)
    if settings.db_mode:
        from backend.api import auth, conversations, usage, workspaces
        app.include_router(auth.router, prefix="/api")
        app.include_router(workspaces.router, prefix="/api")
        app.include_router(conversations.router, prefix="/api")
        app.include_router(usage.router, prefix="/api")

        # Sync / agent routes (agent_auth, devices, sync, workspace_sync) — served only when the device-token
        # secret is configured, so a deployment that predates agent sync starts as before. Whether the secret is
        # usable is checked in the lifespan (startup fails if it is not), never here at import. Without it only
        # the shared state is installed: web login still needs the login limiter.
        if settings.DEVICE_TOKEN_SECRET:
            install_sync(app, settings, SystemClock())
        else:
            install_state(app, settings, SystemClock())

    app.add_exception_handler(TallyConnectionError, tally_connection_error_handler)
    app.add_exception_handler(TallyResponseError, tally_response_error_handler)
    app.add_exception_handler(Exception, generic_error_handler)
    return app


app = build_app()
