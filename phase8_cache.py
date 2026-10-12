# phase8_cache.py
# ---------------------------------------------------------------------------
# Phase 8 — Lightweight Redis cache helper (optional, fails gracefully)
# ---------------------------------------------------------------------------

import os
import logging

logger = logging.getLogger("rap.cache")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


async def check_redis_health() -> bool:
    try:
        import redis.asyncio as aioredis
        r = await aioredis.from_url(REDIS_URL, decode_responses=True)
        await r.ping()
        await r.aclose()
        return True
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}")
        return False