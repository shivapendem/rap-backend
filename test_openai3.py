import asyncio
import httpx
import os
import calendar
import time
from datetime import datetime, timezone

async def test():
    api_key = "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"
    
    now = datetime.now(timezone.utc)
    start_date = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    start_time = int(start_date.timestamp())
    
    last_day = calendar.monthrange(now.year, now.month)[1]
    end_date = datetime(now.year, now.month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    end_time = int(end_date.timestamp())

    base_url = f"https://api.openai.com/v1/organization/usage/completions?start_time={start_time}&end_time={end_time}&limit=100"
    url = base_url
    print("URL:", url)
    total_tokens = 0
    t0 = time.time()
    loop_count = 0
    try:
        async with httpx.AsyncClient() as client:
            while url and loop_count < 10:
                loop_count += 1
                response = await client.get(url, headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    for bucket in data.get("data", []):
                        for res in bucket.get("results", []):
                            total_tokens += res.get("input_tokens", 0) + res.get("output_tokens", 0) + res.get("num_tokens", 0)
                    
                    if data.get("has_more") and data.get("next_page"):
                        next_cursor = data.get("next_page")
                        url = f"{base_url}&after={next_cursor}"
                    else:
                        break
                else:
                    print("Error:", response.status_code, response.text)
                    break
    except Exception as e:
        print(f"Error fetching OpenAI usage: {e}")

    t1 = time.time()
    print(f"Total tokens calculated: {total_tokens}")
    print(f"Time taken: {t1-t0:.2f}s, API calls: {loop_count}")

asyncio.run(test())
