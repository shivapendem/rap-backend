# phase8_cache.py
# ---------------------------------------------------------------------------
# Phase 8 — Lightweight Redis cache helper (optional, fails gracefully)
# ---------------------------------------------------------------------------

import os
import json
import logging

logger = logging.getLogger("rap.cache")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


async def cache_get(key: str):
    """Returns cached value or None. Never raises — falls back silently."""
    try:
        import redis.asyncio as aioredis
        r = await aioredis.from_url(REDIS_URL, decode_responses=True)
        val = await r.get(key)
        await r.aclose()
        return json.loads(val) if val else None
    except Exception:
        return None


async def cache_set(key: str, value, ttl: int = 300):
    """Set a cached value with TTL seconds. Never raises — fails silently."""
    try:
        import redis.asyncio as aioredis
        r = await aioredis.from_url(REDIS_URL, decode_responses=True)
        await r.set(key, json.dumps(value), ex=ttl)
        await r.aclose()
    except Exception:
        pass


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