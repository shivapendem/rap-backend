import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
from dotenv import load_dotenv

load_dotenv()

# We expect an asyncpg URL, e.g., postgresql+asyncpg://user:pass@host/db
# Fallback for local testing if not provided
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")

engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

Base = declarative_base()

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
