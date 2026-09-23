"""
One-off: move resume files that were written to the server's disk into
DigitalOcean Spaces, then point the DB at the S3 key.

  python migrate_local_files_to_s3.py            # dry run (prints only)
  python migrate_local_files_to_s3.py --apply    # upload + update DB
"""
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

from database import AsyncSessionLocal
from s3_service import upload_file_to_s3

APPLY = "--apply" in sys.argv
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
BASE = Path(__file__).resolve().parent


def _local(p: str):
    if not p:
        return None
    cand = Path(p) if Path(p).is_absolute() else BASE / p
    return cand if cand.is_file() else None


def _mime(p: Path) -> str:
    return "application/pdf" if p.suffix.lower() == ".pdf" else DOCX


async def main():
    moved = skipped = failed = 0
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT id, consultant_id, requirement_id, docx_path FROM generated_resumes WHERE docx_path IS NOT NULL"
        ))).mappings().all()
        for r in rows:
            lp = _local(r["docx_path"])
            if not lp:
                skipped += 1
                continue
            key = f"generated-resumes/{r['consultant_id']}/{r['requirement_id']}/migrated-{r['id']}/{lp.name}"
            print(f"[generated_resumes {r['id']}] {lp} -> {key}")
            if APPLY:
                with open(lp, "rb") as f:
                    ok = upload_file_to_s3(f, key, _mime(lp))
                if ok:
                    await db.execute(text("UPDATE generated_resumes SET docx_path=:k WHERE id=:id"), {"k": key, "id": r["id"]})
                    await db.commit()
                    moved += 1
                else:
                    failed += 1

        rows = (await db.execute(text(
            "SELECT id, base_resume_file_path FROM consultants WHERE base_resume_file_path IS NOT NULL"
        ))).mappings().all()
        for r in rows:
            lp = _local(r["base_resume_file_path"])
            if not lp:
                skipped += 1
                continue
            key = str(r["base_resume_file_path"]).lstrip("/")
            print(f"[consultants {r['id']}] {lp} -> {key}")
            if APPLY:
                with open(lp, "rb") as f:
                    ok = upload_file_to_s3(f, key, _mime(lp))
                if ok:
                    await db.execute(text("UPDATE consultants SET base_resume_file_path=:k WHERE id=:id"), {"k": key, "id": r["id"]})
                    await db.commit()
                    moved += 1
                else:
                    failed += 1

    print(f"\n{'APPLIED' if APPLY else 'DRY RUN'}: moved={moved} skipped(no local file)={skipped} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
