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
#
# BUG FIX — column collision with the Node.js analyze-mail classifier:
# gmail_emails.processed was being used by TWO unrelated systems for
# TWO different meanings:
#   - analyze-mail (Node, cron.js/db.js) sets processed=true the
#     moment it FINISHES CLASSIFYING a row — even for category=
#     'unclassified' or 'ignore' (see markEmailClassified/markEmailError
#     in /home/analyze-mail/src/db.js).
#   - this module used to treat processed=true as "already turned into
#     a Requirement" and pulled everything with processed IS NOT TRUE,
#     with NO category check at all.
#
# Whichever service touched a row first "won", which meant:
#   1. Non-job emails (invites, newsletters, "welcome to X" emails)
#      got converted into Requirements before they were ever classified.
#   2. Once a row was marked processed=true by either service, the
#      other service would never look at it again.
#
# Fix: stop reading/writing `processed` here entirely. Only pull rows
# the classifier has confidently marked category='job_posting', and
# use "does a Requirement already exist for this raw_email_id" as the
# completion check instead of a shared boolean flag.
# =============================================================

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from parser import parse_requirement
from cleaner import clean_requirement_text, html_to_text
from dedup import save_requirement


async def sync_pending_emails(db: AsyncSession, batch_size: int = 100) -> dict:
    """
    Auto-parse every incoming Gmail email into a requirement — does NOT
    wait on the external Node.js classifier to tag category='job_posting'
    first. Previously this only picked up rows the classifier had already
    confirmed, so any email sitting un-classified (classifier down, slow,
    or hasn't gotten to it yet) stayed "Pending" on the Gmail screen
    forever, with manual per-email Reparse as the only way through.

    Still respects an EXPLICIT non-job classification if the classifier
    already made one (category NOT NULL, not 'job_posting', and not
    'unclassified') — that guards against the original bug this file's
    header comment describes (newsletters/invites becoming fake
    Requirements). What's removed is only the requirement to WAIT for a
    positive classification; unclassified (category IS NULL, or the
    literal string 'unclassified' — this classifier's actual "not decided
    yet" value, confirmed from live gmail_emails rows) rows are now
    eligible immediately.

    The real safety gate is parse_requirement()'s own is_likely_requirement
    check below — a row only becomes a Requirement if the parser itself
    is confident it's an actual job posting, regardless of classifier state.

    Returns a summary dict: {saved, duplicates, skipped_not_a_requirement, errors, total}.
    """
    result = await db.execute(
        text("""
            SELECT ge.id, ge.message_id, ge.thread_id, ge.account_email, ge.subject,
                   ge.from_address, ge.from_name, ge.reply_to, ge.body_text,
                   ge.body_html, ge.date
            FROM gmail_emails ge
            WHERE (ge.category IS NULL OR ge.category = 'job_posting' OR ge.category = 'unclassified')
              AND NOT EXISTS (
                  SELECT 1 FROM requirements r WHERE r.raw_email_id = ge.id
              )
            ORDER BY ge.date ASC
            LIMIT :limit
        """),
        {"limit": batch_size},
    )
    rows = result.mappings().all()

    saved, duplicates, skipped_not_a_requirement, errors = 0, 0, 0, 0

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

            # Gate on the parser's own confidence instead of the external
            # classifier — this IS the "auto-reparse every gmail" behavior:
            # every email gets run through the parser automatically, but
            # only ones it's actually confident are job postings become
            # Requirement rows. Non-matches are simply skipped (not saved,
            # not errored) — they'll be re-evaluated next cycle if still
            # unclassified, which is cheap (regex-only, no DB writes).
            if not parsed.get("is_likely_requirement"):
                skipped_not_a_requirement += 1
                continue

            cleaned_jd = clean_requirement_text(body)

            save_result = await save_requirement(
                db=db,
                parsed=parsed,
                cleaned_jd=cleaned_jd,
                raw_email_id=row["id"],       # gmail_emails.id -- matches the real FK
                received_date=row["date"],
            )

            # NOTE: we deliberately do NOT touch gmail_emails.processed here —
            # that column belongs to the Node.js classifier. Completion is
            # now tracked purely by requirements.raw_email_id existing (the
            # NOT EXISTS check above), so nothing needs updating on this row.
            await db.commit()

            if save_result["status"] == "saved":
                saved += 1
                # BUG FIX: nothing ever called match_requirement() for
                # requirements created here — only the manual admin
                # "Rematch"/"Match All" buttons did. That left
                # ats_match_count stuck at its column default of 0 for
                # every auto-synced requirement forever, since this loop
                # is the only path that creates new Requirement rows on
                # an ongoing basis. Local import avoids a top-level
                # circular import between this module and phase4.
                try:
                    from phase4 import match_requirement
                    await match_requirement(db, save_result["id"])
                    
                    # Also run the JobMatch engine to populate Pending Applications
                    from models import Requirement
                    from sqlalchemy.future import select
                    from matching_router import run_matching_for_requirement
                    req_res = await db.execute(select(Requirement).where(Requirement.id == save_result["id"]))
                    req_obj = req_res.scalars().first()
                    if req_obj:
                        await run_matching_for_requirement(db, req_obj)
                        await db.commit()
                        
                except Exception as match_err:
                    # Don't let a matching failure undo the successful
                    # requirement save above — log and move on.
                    print(f"[requirements_sync] auto-match FAILED for requirement_id={save_result['id']}: {match_err}")
                    from error_logger import log_db_error
                    await log_db_error(
                        stage="requirements_sync_automatch",
                        error=match_err,
                        source_type="requirement",
                        source_id=save_result["id"],
                    )
            elif save_result["status"] == "duplicate":
                duplicates += 1

        except Exception as e:
            await db.rollback()
            errors += 1
            print(f"[requirements_sync] FAILED gmail_emails.id={row['id']}: {e}")
            from error_logger import log_db_error
            await log_db_error(
                stage="requirements_sync",
                error=e,
                source_type="gmail_emails",
                source_id=row["id"],
            )

    return {
        "saved": saved,
        "duplicates": duplicates,
        "skipped_not_a_requirement": skipped_not_a_requirement,
        "errors": errors,
        "total": len(rows),
    }


if __name__ == "__main__":
    import asyncio
    from database import AsyncSessionLocal

    async def _run():
        async with AsyncSessionLocal() as db:
            summary = await sync_pending_emails(db)
            print(summary)

    asyncio.run(_run())