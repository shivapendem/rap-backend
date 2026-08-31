# =============================================================
# Phase 2 - Main Pipeline
# Connects all tasks: Gmail Reader → Parser → Cleaner → Dedup
# =============================================================
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from gmail_reader import save_raw_email
from parser import parse_requirements, is_hotlist_email
from cleaner import clean_requirement_text, html_to_text
from dedup import create_jd_hash, save_requirement


async def process_email(
    db: AsyncSession,
    gmail_msg: dict,
    raw_email_id: Optional[int] = None,
    create_requirements: bool = True,
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

    create_requirements:
    Some callers (see phase2.py's reparse_email) invoke this function
    ONLY to create the missing `emails` bookkeeping row for an email
    that's about to be parsed and saved separately, by their OWN
    explicit parse_requirement()-based logic further down. That
    downstream logic assumes at most one Requirement row exists per
    raw_email_id — true when this function could only ever create 0 or
    1 row itself. Since parse_requirements() (plural — see parser.py)
    can now produce several Requirement rows for one email, letting
    THIS call also create rows in that scenario would leave extra rows
    orphaned: uncounted in the caller's response, and never touched by
    that caller's own update-or-insert-by-raw_email_id logic again.
    Callers in that situation should pass create_requirements=False —
    this function still saves the `emails` row and returns normally,
    it just skips parsing/saving Requirement rows entirely.

    Returns:
    {
        "email_status": "saved" | "already_exists",
        "requirement_status": "saved" | "duplicate" | "skipped",
        "requirement_id": id or None,
        "all_requirement_ids": [id, ...],
        "requirement_count": int
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
            "all_requirement_ids": [],
            "requirement_count": 0,
        }

    if not create_requirements:
        return {
            "email_status": email_status,
            "requirement_status": "skipped",
            "requirement_id": None,
            "all_requirement_ids": [],
            "requirement_count": 0,
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

    # BUG FIX: is_hotlist_email() existed in parser.py but was never
    # actually wired into this pipeline. "Hotlist"/bench-broadcast emails
    # (a recruiter advertising THEIR available consultants, asking others
    # to send THEM requirements) use nearly all the same staffing
    # keywords as a real job posting, so they were being run through
    # requirement parsing like any other email -- producing garbage rows
    # (role = the raw subject line, location/skills pulled from a
    # candidate roster table, etc.) for emails that were never job
    # postings in the first place. Skip requirement creation entirely for
    # these; the `emails` bookkeeping row above is still saved either way.
    if is_hotlist_email(body):
        return {
            "email_status": email_status,
            "requirement_status": "skipped_hotlist",
            "requirement_id": None,
            "all_requirement_ids": [],
            "requirement_count": 0,
        }

    # MULTI-REQUIREMENT FIX: an email can contain more than one distinct
    # job posting. parse_requirements() (plural) splits the body into
    # candidate blocks only when there's strong evidence of more than one
    # posting — otherwise it returns exactly the single (parsed, body)
    # pair parse_requirement() alone would have, so single-requirement
    # emails behave identically to before. See parser.py for details.
    parsed_items = parse_requirements(subject, body, headers)

    # ---------------------------------------------------------------------------
    # Task 4/5: Clean each requirement's own JD text and save it
    # ---------------------------------------------------------------------------
    # Use the caller-supplied gmail_emails.id if we have it. Do NOT fall back
    # to emails.id here — that was the source of the FK violation. Every
    # requirement pulled from this email shares the same raw_email_id —
    # there's no unique constraint on that column (dedup is keyed on
    # vendor_email|role|jd_hash instead), so multiple rows per email are
    # already safe to create.
    saved_results = []
    for parsed, segment_text in parsed_items:
        cleaned_jd = clean_requirement_text(segment_text)
        result = await save_requirement(
            db=db,
            parsed=parsed,
            cleaned_jd=cleaned_jd,
            raw_email_id=raw_email_id,
            received_date=gmail_msg.get("received_at"),  # BUG FIX: was never passed through — column stayed NULL forever
        )
        saved_results.append(result)

    # BACKWARD COMPATIBILITY: existing callers of process_email() (see
    # phase2.py's /api/pipeline/process-email) read result["requirement_status"]
    # and result["requirement_id"] as singular values. For the overwhelmingly
    # common single-requirement case (len(saved_results) == 1) these fields
    # behave exactly as before. When an email really did contain multiple
    # postings, requirement_status/requirement_id reflect the FIRST one
    # saved (so nothing downstream breaks or crashes on an unexpected
    # shape), and the full set is additionally available under
    # "all_requirement_ids" for any caller that wants to know about the rest.
    first_result = saved_results[0] if saved_results else {"status": "skipped", "id": None}

    return {
        "email_status": email_status,
        "requirement_status": first_result["status"],
        "requirement_id": first_result["id"],
        "all_requirement_ids": [r["id"] for r in saved_results if r.get("id") is not None],
        "requirement_count": len(saved_results),
    }