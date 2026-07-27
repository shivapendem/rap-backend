"""
One-off: regenerates the downloadable PDF for every resume that already
has one, using the current data + current phase6.py template.

This exists because saving a resume edit never used to regenerate its PDF
(see the fix in resume_router.py's update_resume()) -- every resume
generated before that fix has a PDF sitting in S3 that's permanently
frozen at whatever phase6.py looked like the moment it was first created,
regardless of any edits made since or any template fixes (like the
Declaration/Personal Details removal, or the Technical Proficiencies
table). This brings every existing resume's PDF up to date in one pass.

Usage:
    python regenerate_all_pdfs.py              # dry run -- lists resumes that would be regenerated
    python regenerate_all_pdfs.py --apply       # actually regenerates and re-uploads them
    python regenerate_all_pdfs.py --apply --id 102   # just one resume, by id
"""
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select
from database import AsyncSessionLocal
from models import Resume
from phase6 import _generate_docx, _convert_to_pdf
from s3_service import upload_file_to_s3


async def main(apply: bool, only_id: int | None):
    async with AsyncSessionLocal() as session:
        query = select(Resume).where(Resume.data.isnot(None))
        if only_id:
            query = query.where(Resume.id == only_id)
        result = await session.execute(query)
        resumes = [r for r in result.scalars().all() if r.data]

        if not resumes:
            print("No resumes with data found.")
            return

        print(f"Found {len(resumes)} resume(s) to regenerate:\n")

        succeeded = 0
        failed = 0
        for resume in resumes:
            print(f"  Resume #{resume.id} (user {resume.user_id}) ... ", end="", flush=True)

            if not apply:
                print("would regenerate (dry run)")
                continue

            resume_dir = Path("/tmp/resumes") / str(resume.user_id) / str(resume.id)
            resume_dir.mkdir(parents=True, exist_ok=True)
            docx_path = resume_dir / "resume.docx"
            pdf_path = resume_dir / "resume.pdf"

            try:
                _generate_docx(resume.data, docx_path)
                if not _convert_to_pdf(docx_path, pdf_path):
                    print("FAILED (pdf conversion)")
                    failed += 1
                    continue

                s3_key = f"users/{resume.user_id}/resumes/{resume.id}/resume.pdf"
                with open(pdf_path, "rb") as f:
                    if upload_file_to_s3(f, s3_key, "application/pdf"):
                        resume.s3_key = s3_key
                        resume.status = "completed"
                        await session.commit()
                        print("done")
                        succeeded += 1
                    else:
                        print("FAILED (s3 upload)")
                        failed += 1
            except Exception as e:
                print(f"FAILED ({e})")
                failed += 1

        if apply:
            print(f"\nRegenerated {succeeded} resume(s), {failed} failed.")
        else:
            print(f"\nDry run only -- no files written. Re-run with --apply to regenerate these {len(resumes)} resume(s).")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    only_id = None
    if "--id" in sys.argv:
        only_id = int(sys.argv[sys.argv.index("--id") + 1])
    asyncio.run(main(apply=apply, only_id=only_id))