# openai_usage_client.py
# ---------------------------------------------------------------------------
# Shared, cached reader for OpenAI's organization costs and usage APIs,
# used by phase8.py's /ai-usage/stats, /ai-usage/openai, and /ai-usage/daily endpoints.
#
# Key Features:
#   1. Fetches exact billed USD from https://api.openai.com/v1/organization/costs
#      so backend figures match the OpenAI dashboard and CSV export exactly.
#   2. Fetches completions tokens & requests from https://api.openai.com/v1/organization/usage/completions.
#   3. Results are cached for OPENAI_USAGE_CACHE_SECONDS (default 300s) to avoid
#      redundant network calls across simultaneous dashboard widgets.
#   4. Paginates cleanly using `has_more` and `next_page`.
# ---------------------------------------------------------------------------

import os
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import httpx

_COSTS_URL = "https://api.openai.com/v1/organization/costs"
_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"
_CACHE_SECONDS = int(os.getenv("OPENAI_USAGE_CACHE_SECONDS", "300"))
_MAX_PAGES = 12

_costs_cache: Dict[int, Any] = {}
_usage_cache: Dict[int, Any] = {}
_lock = asyncio.Lock()


class OpenAIUsageError(Exception):
    """Raised when OpenAI's usage or costs API returns a non-200 response."""


def month_start_ts(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    return int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())


def days_start_ts(days: int, now: Optional[datetime] = None) -> int:
    """Midnight UTC `days-1` days ago, so the window includes today.
    Rounded to midnight so the cache key is stable across polls."""
    now = now or datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int((today - timedelta(days=max(1, days) - 1)).timestamp())


async def fetch_costs_buckets(start_time: int) -> Optional[List[Dict[str, Any]]]:
    """Daily cost buckets from OpenAI /v1/organization/costs starting from start_time."""
    api_key = os.getenv("OPENAI_ADMIN_API_KEY")
    if not api_key:
        return None

    hit = _costs_cache.get(start_time)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    async with _lock:
        hit = _costs_cache.get(start_time)
        if hit and hit[0] > time.monotonic():
            return hit[1]

        buckets: List[Dict[str, Any]] = []
        page: Optional[str] = None
        async with httpx.AsyncClient() as client:
            for _ in range(_MAX_PAGES):
                params: Dict[str, Any] = {
                    "start_time": start_time,
                    "bucket_width": "1d",
                    "limit": 31,
                }
                if page:
                    params["page"] = page
                resp = await client.get(
                    _COSTS_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    raise OpenAIUsageError(f"OpenAI Costs API Error {resp.status_code}: {resp.text}")
                data = resp.json()
                buckets.extend(data.get("data", []) or [])
                if data.get("has_more") and data.get("next_page"):
                    page = data["next_page"]
                else:
                    break

        _costs_cache[start_time] = (time.monotonic() + _CACHE_SECONDS, buckets)
        if len(_costs_cache) > 20:
            for k in sorted(_costs_cache, key=lambda k: _costs_cache[k][0])[:-20]:
                _costs_cache.pop(k, None)
        return buckets


async def fetch_usage_buckets(start_time: int) -> Optional[List[Dict[str, Any]]]:
    """Daily completions usage buckets from OpenAI /v1/organization/usage/completions."""
    api_key = os.getenv("OPENAI_ADMIN_API_KEY")
    if not api_key:
        return None

    hit = _usage_cache.get(start_time)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    async with _lock:
        hit = _usage_cache.get(start_time)
        if hit and hit[0] > time.monotonic():
            return hit[1]

        buckets: List[Dict[str, Any]] = []
        page: Optional[str] = None
        async with httpx.AsyncClient() as client:
            for _ in range(_MAX_PAGES):
                params: Dict[str, Any] = {
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
                    raise OpenAIUsageError(f"OpenAI Usage API Error {resp.status_code}: {resp.text}")
                data = resp.json()
                buckets.extend(data.get("data", []) or [])
                if data.get("has_more") and data.get("next_page"):
                    page = data["next_page"]
                else:
                    break

        _usage_cache[start_time] = (time.monotonic() + _CACHE_SECONDS, buckets)
        if len(_usage_cache) > 20:
            for k in sorted(_usage_cache, key=lambda k: _usage_cache[k][0])[:-20]:
                _usage_cache.pop(k, None)
        return buckets


# Backward-compatible alias for existing callers
async def fetch_buckets(start_time: int) -> Optional[List[Dict[str, Any]]]:
    return await fetch_usage_buckets(start_time)


def summarize_costs(cost_buckets: List[Dict[str, Any]]) -> float:
    """Calculate exact total USD cost from OpenAI organization/costs buckets."""
    total_cost = 0.0
    for bucket in cost_buckets or []:
        for res in bucket.get("results", []) or []:
            amount_obj = res.get("amount")
            if isinstance(amount_obj, dict):
                total_cost += float(amount_obj.get("value") or 0.0)
            elif amount_obj is not None:
                total_cost += float(amount_obj)
    return total_cost


def daily_costs_from_api(cost_buckets: List[Dict[str, Any]]) -> Dict[str, float]:
    """Map OpenAI organization/costs buckets to {'YYYY-MM-DD': cost_usd} using UTC dates."""
    out: Dict[str, float] = {}
    for bucket in cost_buckets or []:
        start = bucket.get("start_time")
        if not start:
            continue
        day = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
        out.setdefault(day, 0.0)
        for res in bucket.get("results", []) or []:
            amount_obj = res.get("amount")
            if isinstance(amount_obj, dict):
                val = float(amount_obj.get("value") or 0.0)
            elif amount_obj is not None:
                val = float(amount_obj)
            else:
                val = 0.0
            out[day] += val
    return out


def summarize(buckets: List[Dict[str, Any]], cost_buckets: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Totals across buckets: exact cost_usd (if cost_buckets provided), tokens, requests."""
    tokens, requests = 0, 0
    for bucket in buckets or []:
        for res in bucket.get("results", []) or []:
            tokens += int(res.get("input_tokens") or 0) + int(res.get("output_tokens") or 0)
            requests += int(res.get("num_model_requests") or 0)

    if cost_buckets is not None:
        cost = summarize_costs(cost_buckets)
    else:
        # Fallback if costs API wasn't fetched
        cost = 0.0

    return {"cost_usd": cost, "tokens": tokens, "requests": requests}


def daily_costs(buckets: List[Dict[str, Any]]) -> Dict[str, float]:
    """Backward-compatible helper."""
    return daily_costs_from_api(buckets)