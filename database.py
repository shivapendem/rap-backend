
import os
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # Fallback for local development only
    import warnings
    warnings.warn(
        "DATABASE_URL not set. Falling back to SQLite for local dev. "
        "Set DATABASE_URL (postgresql+asyncpg://...) for production.",
        UserWarning,
        stacklevel=2,
    )
    DATABASE_URL = "sqlite+aiosqlite:///./test.db"

from sqlalchemy.pool import NullPool

# Validate driver compatibility
if DATABASE_URL.startswith("postgresql://"):
    # asyncpg requires the +asyncpg driver scheme
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

if DATABASE_URL.startswith("sqlite://") and not DATABASE_URL.startswith("sqlite+aiosqlite://"):
    DATABASE_URL = DATABASE_URL.replace("sqlite://", "sqlite+aiosqlite://", 1)

use_null_pool = os.getenv("DB_USE_NULLPOOL", "false").lower() in ("true", "1") or os.getenv("DB_POOL_SIZE") == "0"

engine_kwargs = {
    "echo": os.getenv("DB_ECHO", "false").lower() == "true",  # Don't echo in production by default
    "pool_pre_ping": True,  # Verify connection is alive before handing it out
}

if use_null_pool:
    engine_kwargs["poolclass"] = NullPool
elif not DATABASE_URL.startswith("sqlite"):
    engine_kwargs.update({
        "pool_size": int(os.getenv("DB_POOL_SIZE", "2")),        # Strictly cap connection pool size to 2 per worker process
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "0")),   # 0 overflow allowed — prevents connection limit exhaustion
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE", "60")),  # Recycle idle connections after 60s
        "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT", "10")),  # Fail fast after 10s if connection queue is full
    })

engine = create_async_engine(DATABASE_URL, **engine_kwargs)

from sqlalchemy import event

def _clean_sql_param(val):
    if isinstance(val, str):
        if "\x00" in val or "\u0000" in val:
            return val.replace("\x00", "").replace("\u0000", "")
        return val
    if isinstance(val, dict):
        return {k: _clean_sql_param(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_clean_sql_param(v) for v in val]
    return val

@event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
def _sanitize_nul_bytes(conn, cursor, statement, parameters, context, executemany):
    """
    Global Engine Safeguard:
    PostgreSQL UTF-8 strictly forbids NUL (0x00) bytes in VARCHAR/TEXT columns,
    raising CharacterNotInRepertoireError. This intercepts and strips 0x00 bytes
    from all parameter strings before they reach asyncpg/PostgreSQL.
    """
    if parameters:
        if isinstance(parameters, tuple):
            parameters = tuple(_clean_sql_param(p) for p in parameters)
        elif isinstance(parameters, list):
            parameters = [_clean_sql_param(p) for p in parameters]
        elif isinstance(parameters, dict):
            parameters = {k: _clean_sql_param(v) for k, v in parameters.items()}
    return statement, parameters

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

Base = declarative_base()


async def get_db():
    """Dependency that provides a database session and ensures it is closed after use."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except HTTPException:
            # BUG FIX: this except block used to catch bare `Exception`,
            # which also caught HTTPException — meaning every routine 401
            # "Not authenticated" and 403 "Requires role: [...]" rejection
            # from an ordinary auth/permission check anywhere in the app
            # got logged to the Error Queue as if it were an application
            # bug. That's expected control flow, not an error: re-raise it
            # untouched (still rolling back first) without logging noise
            # that buries genuine crashes.
            await session.rollback()
            raise
        except Exception as e:
            await session.rollback()
            from error_logger import log_db_error
            await log_db_error(stage="get_db_dependency", error=e)
            raise
        finally:
            await session.close()
 