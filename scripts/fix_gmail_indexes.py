"""
Adds performance indexes to gmail_emails and reports table size / query plans.
Run this once from your backend folder (uses the same DATABASE_URL as your app).
"""
import asyncio
import os
from dotenv import load_dotenv
import asyncpg

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL not found in .env")

# asyncpg wants a plain postgresql:// URL, not the +asyncpg SQLAlchemy variant
PLAIN_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


async def main():
    print(f"Connecting to database...")
    conn = await asyncpg.connect(PLAIN_URL)
    try:
        # --- Diagnose first ---
        count_row = await conn.fetchrow("SELECT reltuples::bigint AS estimate FROM pg_class WHERE relname = 'gmail_emails'")
        print(f"\nApproximate row count in gmail_emails: {count_row['estimate']:,}")

        existing = await conn.fetch("""
            SELECT indexname FROM pg_indexes WHERE tablename = 'gmail_emails'
        """)
        print(f"Existing indexes: {[r['indexname'] for r in existing]}")

        print("\n--- EXPLAIN ANALYZE: COUNT(*) ---")
        plan = await conn.fetch("EXPLAIN ANALYZE SELECT COUNT(*) FROM gmail_emails")
        for row in plan:
            print(row["QUERY PLAN"])

        print("\n--- EXPLAIN ANALYZE: ORDER BY date DESC LIMIT 20 ---")
        plan2 = await conn.fetch("EXPLAIN ANALYZE SELECT id FROM gmail_emails ORDER BY date DESC LIMIT 20")
        for row in plan2:
            print(row["QUERY PLAN"])

        # --- Add indexes (safe to re-run — IF NOT EXISTS) ---
        print("\nCreating index on date (this may take a moment on a large table)...")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_gmail_emails_date ON gmail_emails (date DESC)")
        print("Done: idx_gmail_emails_date")

        print("Creating index on fetched_at...")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_gmail_emails_fetched_at ON gmail_emails (fetched_at DESC)")
        print("Done: idx_gmail_emails_fetched_at")

        print("\n--- Re-checking plan after indexing ---")
        plan3 = await conn.fetch("EXPLAIN ANALYZE SELECT id FROM gmail_emails ORDER BY date DESC LIMIT 20")
        for row in plan3:
            print(row["QUERY PLAN"])

        print("\nAll done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())