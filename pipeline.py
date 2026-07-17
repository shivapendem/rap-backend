# =============================================================
# Phase 2 - Main Pipeline
# Connects all tasks: Gmail Reader → Parser → Cleaner → Dedup
# =============================================================
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from gmail_reader import save_raw_email
from parser import parse_requirement
from cleaner import clean_requirement_text, html_to_text
from dedup import create_jd_hash, save_requirement


async def process_email(
    db: AsyncSession,
    gmail_msg: dict,
    raw_email_id: Optional[int] = None,
) -> dict:
    """
    Main pipeline function.
    Processes one email through all Phase 2 tasks.

    IMPORTANT — raw_email_id:
    requirements.raw_email_id is a foreign key into gmail_emails.id
    (CONFIRMED via pg_constraint — see requirements_sync.py). It is
    NOT a foreign key into emails.id, despite emails.id being what
    save_raw_email() below returns for internal bookkeeping.

    Previously this function used emails.id (from save_raw_email) as
    raw_email_id directly, which either matched an unrelated
    gmail_emails row by coincidence or hit a ForeignKeyViolationError
    like:
        Key (raw_email_id)=(98) is not present in table "gmail_emails"

    Callers that know the real gmail_emails.id (e.g. reparse_email,
    which already has source_gmail_emails_id) MUST pass it in via the
    raw_email_id parameter. If it's genuinely unknown, we save NULL —
    the column allows NULL (ON DELETE SET NULL) — rather than guessing
    wrong and crashing the whole save.

    Returns:
    {
        "email_status": "saved" | "already_exists",
        "requirement_status": "saved" | "duplicate" | "skipped",
        "requirement_id": id or None
    }
    """
    # ---------------------------------------------------------------------------
    # Task 1: Save raw email (into `emails` table — internal bookkeeping only,
    # NOT the same id space as gmail_emails / raw_email_id FK)
    # ---------------------------------------------------------------------------
    email_result = await save_raw_email(db, gmail_msg)
    email_status = email_result["status"]

    if email_status == "already_exists":
        return {
            "email_status": "already_exists",
            "requirement_status": "skipped",
            "requirement_id": None,
        }

    # ---------------------------------------------------------------------------
    # Task 2: Parse requirement fields
    # ---------------------------------------------------------------------------
    subject = gmail_msg.get("subject", "")
    body_text = gmail_msg.get("plain_text_body", "")
    body_html = gmail_msg.get("html_body", "")

    # BUG FIX: parser.extract_vendor_email() / vendor-name extraction both read
    # headers["from"] and headers["reply_to"] — but the real production payload
    # (ProcessEmailRequest) sends from_email/from_name/reply_to_email as FLAT
    # top-level fields, not nested under "headers". That meant headers was
    # almost always {} in real usage, so vendor_email/vendor/"from" silently
    # came back None every time. Build headers from whichever shape we got.
    headers = dict(gmail_msg.get("headers") or {})
    if "from" not in headers:
        from_email = gmail_msg.get("from_email")
        from_name = gmail_msg.get("from_name")
        if from_email:
            headers["from"] = f'{from_name} <{from_email}>' if from_name else from_email
    if "reply_to" not in headers:
        reply_to_email = gmail_msg.get("reply_to_email")
        if reply_to_email:
            headers["reply_to"] = reply_to_email

    # Use plain text if available, else convert HTML
    body = body_text or html_to_text(body_html)
    parsed = parse_requirement(subject, body, headers)

    # ---------------------------------------------------------------------------
    # Task 4: Clean the JD text
    # ---------------------------------------------------------------------------
    cleaned_jd = clean_requirement_text(body)

    # ---------------------------------------------------------------------------
    # Task 5: Dedup and save requirement
    # ---------------------------------------------------------------------------
    # Use the caller-supplied gmail_emails.id if we have it. Do NOT fall back
    # to emails.id here — that was the source of the FK violation.
    result = await save_requirement(
        db=db,
        parsed=parsed,
        cleaned_jd=cleaned_jd,
        raw_email_id=raw_email_id,
        received_date=gmail_msg.get("received_at"),  # BUG FIX: was never passed through — column stayed NULL forever
    )

    return {
        "email_status": email_status,
        "requirement_status": result["status"],
        "requirement_id": result["id"],
    }
