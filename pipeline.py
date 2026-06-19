# =============================================================
# Phase 2 - Main Pipeline
# Connects all tasks: Gmail Reader → Parser → Cleaner → Dedup
# =============================================================

from sqlalchemy.ext.asyncio import AsyncSession
from gmail_reader import save_raw_email
from parser import parse_requirement
from cleaner import clean_requirement_text, html_to_text
from dedup import create_jd_hash, save_requirement


async def process_email(db: AsyncSession, gmail_msg: dict) -> dict:
    """
    Main pipeline function.
    Processes one email through all Phase 2 tasks.

    Returns:
    {
        "email_status": "saved" | "already_exists",
        "requirement_status": "saved" | "duplicate" | "skipped",
        "requirement_id": id or None
    }
    """

    # ---------------------------------------------------------------------------
    # Task 1: Save raw email
    # ---------------------------------------------------------------------------
    email_status = await save_raw_email(db, gmail_msg)
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
    headers = gmail_msg.get("headers", {})

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
    result = await save_requirement(
        db=db,
        parsed=parsed,
        cleaned_jd=cleaned_jd,
        raw_email_id=gmail_msg.get("raw_email_id"),
    )

    return {
        "email_status": email_status,
        "requirement_status": result["status"],
        "requirement_id": result["id"],
    }
