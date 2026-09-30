"""Async SQLAlchemy engine and session factory — the only engine of the backend (v2 merge M3)."""
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from backend.config import settings

engine = None
async_session_factory = None


def build_engine(url: str) -> AsyncEngine:
    """Every engine of the backend is built here: the app's own (``init_engine``) and the short-lived one of a
    CLI run (``python -m backend.sync purge``).

    ``pool_pre_ping=True``: a connection the server closed while it sat idle in the pool is replaced instead of
    failing the next request. ``hide_parameters=True``: a DB error's ``str()``/traceback never carries bound
    business values (narration, party/ledger names, amounts) into the server logs (S1 review I1).
    """
    return create_async_engine(
        url, echo=False, pool_size=5, max_overflow=10, pool_pre_ping=True, hide_parameters=True
    )


def init_engine(database_url: str | None = None) -> None:
    global engine, async_session_factory
    url = database_url or settings.DATABASE_URL
    if not url:
        raise ValueError("DATABASE_URL must be set to initialize the database engine")
    engine = build_engine(url)
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db() -> AsyncSession:
    if async_session_factory is None:
        raise RuntimeError("Database not initialized. Set DATABASE_URL to enable DB mode.")
    async with async_session_factory() as session:
        yield session

async def close_engine() -> None:
    global engine, async_session_factory
    if engine:
        await engine.dispose()
    engine = None
    async_session_factory = None
