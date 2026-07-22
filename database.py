
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

# Validate driver compatibility
if DATABASE_URL.startswith("postgresql://"):
    # asyncpg requires the +asyncpg driver scheme
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

if DATABASE_URL.startswith("sqlite://") and not DATABASE_URL.startswith("sqlite+aiosqlite://"):
    DATABASE_URL = DATABASE_URL.replace("sqlite://", "sqlite+aiosqlite://", 1)

engine = create_async_engine(
    DATABASE_URL,
    echo=os.getenv("DB_ECHO", "false").lower() == "true",  # Don't echo in production by default
    pool_pre_ping=True,  # Detect stale connections
    # pool_size / max_overflow only supported for non-SQLite
    **({
        "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "10")),
    } if not DATABASE_URL.startswith("sqlite") else {})
)

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
 
