"""Run after testing in the UI:  .\\venv\\Scripts\\python.exe check_s3_fix.py"""
import asyncio
import time
from pathlib import Path

from sqlalchemy import text

from database import AsyncSessionLocal
from s3_service import s3_client, DO_SPACES_BUCKET


async def db_checks():
    async with AsyncSessionLocal() as db:
        print("\n1) Latest tailored resumes (docx_path must start with generated-resumes/):")
        for r in (await db.execute(text(
            "SELECT id, docx_path, created_at FROM generated_resumes ORDER BY id DESC LIMIT 3"))).all():
            ok = "OK " if (r[1] or "").startswith("generated-resumes/") else "OLD"
            print(f"   [{ok}] id={r[0]}  {r[1]}  {r[2]}")

        print("\n2) Latest admin resumes (s3_key must start with users/):")
        for r in (await db.execute(text(
            "SELECT id, s3_key, status FROM resumes ORDER BY id DESC LIMIT 3"))).all():
            print(f"   id={r[0]}  {r[1]}  status={r[2]}")


def s3_checks():
    print(f"\n3) Newest files in bucket '{DO_SPACES_BUCKET}':")
    for prefix in ("generated-resumes/", "email-queue-attachments/", "users/", "uploads/resumes/"):
        objs = s3_client.list_objects_v2(Bucket=DO_SPACES_BUCKET, Prefix=prefix).get("Contents", [])
        objs.sort(key=lambda o: o["LastModified"], reverse=True)
        print(f"   {prefix:28} total={len(objs):5}  newest={objs[0]['Key'] + '  ' + str(objs[0]['LastModified']) if objs else '-'}")


def disk_checks():
    print("\n4) Files written to local disk in the last 30 min (must be NONE):")
    cutoff = time.time() - 1800
    found = [p for d in ("uploads",) if Path(d).exists()
             for p in Path(d).rglob("*") if p.is_file() and p.stat().st_mtime > cutoff]
    print("   NONE - OK" if not found else "\n".join(f"   FOUND: {p}" for p in found))


asyncio.run(db_checks())
s3_checks()
disk_checks()