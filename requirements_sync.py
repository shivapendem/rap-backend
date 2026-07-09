# =============================================================
# Bridges gmail_emails (raw IMAP sync table) -> requirements
#
# CONFIRMED via pg_constraint: requirements.raw_email_id references
# gmail_emails.id directly (NOT emails.id). The old pipeline.py /
# gmail_reader.save_raw_email() path inserted into the separate
# `emails` table and used THAT id as raw_email_id -- those ids don't
# correspond to gmail_emails.id, so every insert hit a foreign key
# violation and silently failed the whole requirement save.
#
# This module bypasses that path entirely: parse + dedup + insert
# directly, using gmail_emails.id as raw_email_id. Importable so it
# can run both as a one-off CLI script and as a background job
# inside the FastAPI app (see main.py).
# =============================================================

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from parser import parse_requirement
from cleaner import clean_requirement_text, html_to_text
from dedup import save_requirement


async def sync_pending_emails(db: AsyncSession, batch_size: int = 100) -> dict:
    """
    Process all unprocessed gmail_emails rows into requirements.
    Returns a summary dict: {saved, duplicates, errors, total}.
    """
    result = await db.execute(
        text("""
            SELECT id, message_id, thread_id, account_email, subject,
                   from_address, from_name, reply_to, body_text, body_html, date
            FROM gmail_emails
            WHERE processed IS NOT TRUE
            ORDER BY date ASC
            LIMIT :limit
        """),
        {"limit": batch_size},
    )
    rows = result.mappings().all()

    saved, duplicates, errors = 0, 0, 0

    for row in rows:
        try:
            subject = row["subject"] or ""
            body_text = row["body_text"] or ""
            body_html = row["body_html"] or ""
            body = body_text or html_to_text(body_html)

            headers = {}
            if row["from_address"]:
                headers["from"] = (
                    f'{row["from_name"]} <{row["from_address"]}>'
                    if row["from_name"] else row["from_address"]
                )
            if row["reply_to"]:
                headers["reply_to"] = row["reply_to"]

            parsed = parse_requirement(subject, body, headers)
            cleaned_jd = clean_requirement_text(body)

            save_result = await save_requirement(
                db=db,
                parsed=parsed,
                cleaned_jd=cleaned_jd,
                raw_email_id=row["id"],       # gmail_emails.id -- matches the real FK
                received_date=row["date"],
            )

            await db.execute(
                text("UPDATE gmail_emails SET processed = true WHERE id = :id"),
                {"id": row["id"]},
            )
            await db.commit()

            if save_result["status"] == "saved":
                saved += 1
            elif save_result["status"] == "duplicate":
                duplicates += 1

        except Exception as e:
            await db.rollback()
            errors += 1
            print(f"[requirements_sync] FAILED gmail_emails.id={row['id']}: {e}")

    return {"saved": saved, "duplicates": duplicates, "errors": errors, "total": len(rows)}


if __name__ == "__main__":
    import asyncio
    from database import AsyncSessionLocal

    async def _run():
        async with AsyncSessionLocal() as db:
            summary = await sync_pending_emails(db)
            print(summary)

    asyncio.run(_run())
