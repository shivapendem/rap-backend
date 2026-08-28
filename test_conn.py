import asyncio
import asyncpg
import time

async def main():
    start = time.monotonic()
    try:
        conn = await asyncpg.connect(
            user='rap_user',
            password='6Pu3GSvyj5ENA',
            host='137.184.96.50',
            port=5432,
            database='rap_db',
            timeout=15,
        )
        print(f'Connected! ({time.monotonic() - start:.2f}s)')
        await conn.close()
    except Exception as e:
        print(f'FAILED after {time.monotonic() - start:.2f}s: {e!r}')

asyncio.run(main())
