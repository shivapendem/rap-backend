# =============================================================
# Phase 2 - Task 1: Gmail Reader
# Reads new emails from Gmail and saves to raw_emails table
# =============================================================

import os
import base64
from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from models import Email


async def save_raw_email(db: AsyncSession, gmail_msg: dict) -> str:
    """
    Save a raw email to database.
    Returns 'saved' or 'already_exists'
    """
    # Check if email already exists
    result = await db.execute(
        select(Email).where(Email.gmail_message_id == gmail_msg["id"])
    )
    if result.scalars().first():
        return "already_exists"

    # Save new email
    new_email = Email(
        gmail_message_id=gmail_msg["id"],
        gmail_thread_id=gmail_msg.get("thread_id"),
        recruiter_email=gmail_msg.get("recruiter_email", ""),
        sender_email=gmail_msg.get("from_email", ""),
        sender_name=gmail_msg.get("from_name", ""),
        subject=gmail_msg.get("subject", ""),
        body_text=gmail_msg.get("plain_text_body", ""),
        body_html=gmail_msg.get("html_body", ""),
        reply_to_address=gmail_msg.get("reply_to_email"),
        received_at=gmail_msg.get("received_at"),
        parse_status="NEW",
    )
    db.add(new_email)
    await db.commit()
    await db.refresh(new_email)
    return "saved"


def decode_gmail_body(payload: dict) -> tuple[str, str]:
    """
    Decode Gmail message payload to plain text and HTML.
    Returns (plain_text, html_text)
    """
    plain_text = ""
    html_text = ""

    def extract_parts(part):
        nonlocal plain_text, html_text
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data", "")

        if data:
            decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
            if mime == "text/plain":
                plain_text = decoded
            elif mime == "text/html":
                html_text = decoded

        for sub_part in part.get("parts", []):
            extract_parts(sub_part)

    extract_parts(payload)
    return plain_text, html_text


def parse_gmail_headers(headers: list) -> dict:
    """Extract useful headers from Gmail message."""
    result = {}
    for header in headers:
        name = header.get("name", "").lower()
        value = header.get("value", "")
        if name == "from":
            result["from"] = value
        elif name == "reply-to":
            result["reply_to"] = value
        elif name == "subject":
            result["subject"] = value
        elif name == "date":
            result["date"] = value
    return result
