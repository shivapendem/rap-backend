# openai_usage_client.py
# ---------------------------------------------------------------------------
# NEW FILE (AI-usage fixes). Shared, cached reader for OpenAI's organization
# usage API, used by phase8.py's /ai-usage/stats, /ai-usage/openai and
# /ai-usage/daily endpoints.
#
# Why this exists (bugs it fixes):
#   1. Each endpoint called OpenAI on its own, every 30s, per open browser
#      tab -- 3 identical 30-day fetches per poll. Results are now cached
#      for OPENAI_USAGE_CACHE_SECONDS (default 300s) and shared.
#   2. /ai-usage/stats read the DB first, then held that DB connection
#      while waiting up to 10s on OpenAI -> contributed to the
#      "QueuePool limit ... reached" errors. This module does no DB work,
#      so callers can fetch usage BEFORE touching the DB.
#   3. limit=30 with no pagination dropped the newest daily bucket
#      (today). This follows `has_more` / `next_page`.
#   4. `res.get("model")` could be None (crash). Handled here.
#   5. Prices were outdated (gpt-4o at $5/$15) and cached input tokens were
#      billed at full price. Both corrected below -- verify against your
#      OpenAI billing page and adjust PRICING if your contract differs.
#
# Reading usage from OpenAI's admin API does NOT consume model tokens and
# is not billed -- it only reports usage.
# ---------------------------------------------------------------------------

import os
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import httpx

_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"
_CACHE_SECONDS = int(os.getenv("OPENAI_USAGE_CACHE_SECONDS", "300"))
_MAX_PAGES = 12

# USD per 1M tokens: (input, cached_input, output). Most specific name first.
PRICING = [
    ("gpt-4o-mini", 0.15, 0.075, 0.60),
    ("gpt-4.1-nano", 0.10, 0.025, 0.40),
    ("gpt-4.1-mini", 0.40, 0.10, 1.60),
    ("gpt-4.1", 2.00, 0.50, 8.00),
    ("gpt-4o", 2.50, 1.25, 10.00),
    ("gpt-4-turbo", 10.00, 10.00, 30.00),
    ("gpt-4", 30.00, 30.00, 60.00),
    ("gpt-3.5", 0.50, 0.50, 1.50),
]
DEFAULT_PRICE = (2.50, 1.25, 10.00)

_cache: Dict[int, Any] = {}
_lock = asyncio.Lock()


class OpenAIUsageError(Exception):
    """Raised when OpenAI's usage API returns a non-200 response."""


def _price_for(model: Optional[str]):
    m = (model or "").lower()
    for prefix, p_in, p_cached, p_out in PRICING:
        if prefix in m:
            return p_in, p_cached, p_out
    return DEFAULT_PRICE


def result_cost(res: Dict[str, Any]) -> float:
    """Estimated USD cost of one usage result row (one model, one bucket)."""
    p_in, p_cached, p_out = _price_for(res.get("model"))
    in_t = int(res.get("input_tokens") or 0)
    cached_t = int(res.get("input_cached_tokens") or 0)
    out_t = int(res.get("output_tokens") or 0)
    uncached_t = max(0, in_t - cached_t)
    return (uncached_t * p_in + cached_t * p_cached + out_t * p_out) / 1_000_000


def result_tokens(res: Dict[str, Any]) -> int:
    return int(res.get("input_tokens") or 0) + int(res.get("output_tokens") or 0)


def result_requests(res: Dict[str, Any]) -> int:
    return int(res.get("num_model_requests") or 0)


def month_start_ts(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    return int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())


def days_start_ts(days: int, now: Optional[datetime] = None) -> int:
    """Midnight UTC `days-1` days ago, so the window includes today.
    Rounded to midnight so the cache key is stable across polls."""
    now = now or datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int((today - timedelta(days=max(1, days) - 1)).timestamp())


async def fetch_buckets(start_time: int) -> Optional[List[Dict[str, Any]]]:
    """Daily usage buckets (grouped by model) from start_time until now.

    Returns None when OPENAI_ADMIN_API_KEY is not configured.
    Raises OpenAIUsageError on an OpenAI API error.
    """
    api_key = os.getenv("OPENAI_ADMIN_API_KEY")
    if not api_key:
        return None

    hit = _cache.get(start_time)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    async with _lock:
        hit = _cache.get(start_time)
        if hit and hit[0] > time.monotonic():
            return hit[1]

        buckets: List[Dict[str, Any]] = []
        page: Optional[str] = None
        async with httpx.AsyncClient() as client:
            for _ in range(_MAX_PAGES):
                params = {
                    "start_time": start_time,
                    "bucket_width": "1d",
                    "group_by": "model",
                    "limit": 31,
                }
                if page:
                    params["page"] = page
                resp = await client.get(
                    _USAGE_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    raise OpenAIUsageError(f"OpenAI API Error {resp.status_code}: {resp.text}")
                data = resp.json()
                buckets.extend(data.get("data", []) or [])
                if data.get("has_more") and data.get("next_page"):
                    page = data["next_page"]
                else:
                    break

        _cache[start_time] = (time.monotonic() + _CACHE_SECONDS, buckets)
        # keep the cache small
        if len(_cache) > 20:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:-20]:
                _cache.pop(k, None)
        return buckets


def summarize(buckets: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Totals across all buckets: cost_usd, tokens, requests."""
    cost, tokens, requests = 0.0, 0, 0
    for bucket in buckets or []:
        for res in bucket.get("results", []) or []:
            cost += result_cost(res)
            tokens += result_tokens(res)
            requests += result_requests(res)
    return {"cost_usd": cost, "tokens": tokens, "requests": requests}


def daily_costs(buckets: List[Dict[str, Any]]) -> Dict[str, float]:
    """{'YYYY-MM-DD': cost_usd} per UTC day."""
    out: Dict[str, float] = {}
    for bucket in buckets or []:
        start = bucket.get("start_time")
        if not start:
            continue
        day = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
        out.setdefault(day, 0.0)
        for res in bucket.get("results", []) or []:
            out[day] += result_cost(res)
    return out