# phase2.py
# ---------------------------------------------------------------------------
# Phase 2 — Gmail Requirement Intake, Parser, Cleaner, Deduplication
#
# Architecture: single flat file in project root, same pattern as
# phase3.py / phase4.py. Reuses get_db, get_current_user from auth.py —
# no circular dependency with main.py.
#
# This file deliberately does NOT reimplement consultant/user/recruiter
# management endpoints — those already exist correctly in phase3.py/phase4.py.
# Only the genuinely new Phase 2 endpoints (Task 6 of the doc) live here.
#
# New endpoints:
#
#   GET   /api/requirements/{requirement_id}          single requirement detail
#   PATCH /api/requirements/{requirement_id}/status    update requirement status
#   GET   /api/requirements/stats                      dashboard summary counts
#   POST  /api/pipeline/process-email                  run one email through the full pipeline
#   POST  /api/pipeline/parse-text                      test parser against raw subject/body
#   GET   /api/admin/gmail-emails                      list all gmail emails
#   GET   /api/admin/raw-emails/{id}                   get single raw email
#   POST  /api/admin/raw-emails/{id}/reparse           reparse email
#   GET   /api/admin/gmail-accounts                    list all gmail accounts
#   GET   /api/admin/gmail-sync-logs                   list sync logs
#
# Note: GET /api/requirements (list, paginated, filterable) already exists
# in main.py — not duplicated here.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, case, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, Requirement, Email
from auth import get_current_user
from pipeline import process_email
from parser import parse_requirement
from cleaner import clean_requirement_text, html_to_text
from dedup import create_jd_hash, build_dedup_key, save_requirement, clamp_received_date

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_role(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {list(roles)}",
        )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RequirementDetailResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    role: str
    vendor: Optional[str] = None
    vendor_email: Optional[str] = None
    vendor_contact: Optional[str] = None
    client: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[str] = None
    employment_types: Optional[List[str]] = None
    rate: Optional[str] = None
    duration: Optional[str] = None
    experience: Optional[str] = None
    skills: Optional[str] = None
    job_description: Optional[str] = None
    parsed_fields: Optional[dict] = None
    parse_confidence: Optional[float] = None
    status: str
    received_date: Optional[str] = None


class UpdateStatusRequest(BaseModel):
    status: str = Field(..., min_length=1)


class RequirementStatsResponse(BaseModel):
    total: int
    new: int
    reviewing: int
    submitted: int
    interviewing: int
    closed: int
    rejected: int


class ProcessEmailRequest(BaseModel):
    """Mirrors the gmail_msg dict shape expected by pipeline.process_email()."""
    id: str
    thread_id: Optional[str] = None
    recruiter_email: Optional[str] = None
    from_email: Optional[str] = None
    from_name: Optional[str] = None
    subject: Optional[str] = None
    plain_text_body: Optional[str] = None
    html_body: Optional[str] = None
    reply_to_email: Optional[str] = None
    received_at: Optional[str] = None
    headers: Optional[dict] = None
    raw_email_id: Optional[int] = None


class ProcessEmailResponse(BaseModel):
    email_status: str
    requirement_status: str
    requirement_id: Optional[str] = None


class ParseTextRequest(BaseModel):
    subject: str = Field(..., min_length=1)
    body: str = Field(default="")
    from_header: Optional[str] = Field(default=None, alias="from")
    reply_to: Optional[str] = None

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Requirement detail / status / stats — Task 6
# ---------------------------------------------------------------------------

@router.get(
    "/api/requirements/stats",
    response_model=RequirementStatsResponse,
    summary="Get requirement counts by status (admin/recruiter dashboard)",
)
async def get_requirement_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER")

    result = await db.execute(
        select(
            func.count(Requirement.id).label("total"),
            func.sum(case((Requirement.status == "NEW", 1), else_=0)).label("new"),
            func.sum(case((Requirement.status == "REVIEWING", 1), else_=0)).label("reviewing"),
            func.sum(case((Requirement.status == "SUBMITTED", 1), else_=0)).label("submitted"),
            func.sum(case((Requirement.status == "INTERVIEWING", 1), else_=0)).label("interviewing"),
            func.sum(case((Requirement.status == "CLOSED", 1), else_=0)).label("closed"),
            func.sum(case((Requirement.status == "REJECTED", 1), else_=0)).label("rejected"),
        )
    )
    row = result.first()
    return RequirementStatsResponse(
        total=row.total or 0,
        new=row.new or 0,
        reviewing=row.reviewing or 0,
        submitted=row.submitted or 0,
        interviewing=row.interviewing or 0,
        closed=row.closed or 0,
        rejected=row.rejected or 0,
    )


@router.get(
    "/api/requirements/{requirement_id}",
    response_model=RequirementDetailResponse,
    summary="Get a single requirement's full detail",
)
async def get_requirement_detail(
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER", "CONSULTANT")

    result = await db.execute(select(Requirement).where(Requirement.id == requirement_id))
    requirement = result.scalars().first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    # experience/skills are NOT real columns on Requirement — they only
    # ever live inside parsed_fields (see dedup.py / reparse_email below).
    # Reading requirement.experience / requirement.skills directly would
    # raise AttributeError the moment this endpoint is hit. Pull them out
    # of parsed_fields instead.
    parsed_fields = requirement.parsed_fields or {}

    return RequirementDetailResponse(
        id=str(requirement.id),
        role=requirement.role,
        vendor=requirement.vendor,
        vendor_email=requirement.vendor_email,
        vendor_contact=requirement.vendor_contact,
        client=requirement.client,
        location=requirement.location,
        work_mode=requirement.work_mode,
        employment_types=requirement.employment_types,
        rate=requirement.rate,
        duration=requirement.duration,
        experience=parsed_fields.get("experience"),
        skills=parsed_fields.get("skills"),
        job_description=requirement.job_description,
        parsed_fields=requirement.parsed_fields,
        parse_confidence=float(requirement.parse_confidence) if requirement.parse_confidence is not None else None,
        status=requirement.status,
        received_date=requirement.received_date.isoformat() if requirement.received_date else None,
    )


@router.patch(
    "/api/requirements/{requirement_id}/status",
    summary="Update a requirement's status (admin/recruiter only)",
)
async def update_requirement_status(
    requirement_id: int,
    payload: UpdateStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER")

    result = await db.execute(select(Requirement).where(Requirement.id == requirement_id))
    requirement = result.scalars().first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    if payload.status not in Requirement.VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {sorted(Requirement.VALID_STATUSES)}",
        )

    requirement.status = payload.status
    await db.commit()

    logger.info(
        "Requirement id=%s status changed to '%s' by user=%s",
        requirement_id, payload.status, current_user.email,
    )
    return {"success": True, "message": f"Status updated to {payload.status}"}


# ---------------------------------------------------------------------------
# Pipeline test/trigger endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/api/pipeline/process-email",
    response_model=ProcessEmailResponse,
    summary="Run one Gmail-shaped email payload through the full Phase 2 pipeline (admin only)",
)
async def process_email_endpoint(
    payload: ProcessEmailRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")

    gmail_msg = {k: v for k, v in payload.model_dump().items() if v is not None}

    # payload.raw_email_id is the gmail_emails.id the frontend/caller knows
    # about (if any) — this is the value the FK actually needs. Passing it
    # through here instead of letting pipeline.process_email() guess avoids
    # ForeignKeyViolationError on requirements.raw_email_id.
    result = await process_email(db, gmail_msg, raw_email_id=payload.raw_email_id)

    # Update gmail_emails processed status
    if payload.raw_email_id:
        await db.execute(
            text("UPDATE gmail_emails SET processed = true WHERE id = :id"),
            {"id": payload.raw_email_id}
        )
        await db.commit()

    return ProcessEmailResponse(
        email_status=result["email_status"],
        requirement_status=result["requirement_status"],
        requirement_id=str(result["requirement_id"]) if result["requirement_id"] is not None else None,
    )


@router.post(
    "/api/pipeline/parse-text",
    summary="Test the parser against raw subject/body text without saving anything",
)
async def parse_text_endpoint(
    payload: ParseTextRequest,
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")

    headers = {}
    if payload.from_header:
        headers["from"] = payload.from_header
    if payload.reply_to:
        headers["reply_to"] = payload.reply_to

    parsed = parse_requirement(subject=payload.subject, body=payload.body, headers=headers)
    return parsed


# ---------------------------------------------------------------------------
# Gmail Emails endpoints — reads from gmail_emails table
# All columns included as per table structure
# ---------------------------------------------------------------------------

# BUG FIX ("Raw Email Viewer shows literal HTML tags/markup" -- confirmed
# real case: a full <!DOCTYPE html>...<style>...</style>...<p>... document
# rendered verbatim in the read-only viewer instead of readable text): this
# endpoint has always returned body_text/body_html completely raw -- no
# caller-side conversion at all. That's invisible for a plain-text email
# (body_text alone is already readable), but for an HTML-templated email
# (the overwhelmingly common case for recruiter broadcasts) with no real
# body_text, callers were left to guess how to render body_html themselves.
# GmailPage.tsx's own email-detail modal already has a crude client-side
# fallback for this (a bare `.replace(/<[^>]+>/g, " ")` regex, which strips
# tags but leaves <style> block CSS sitting in the output as visible text --
# see that function's own BUG FIX history), but RawEmailModal.tsx (the
# Requirements page's "Raw Email Viewer", i.e. THIS exact symptom) has no
# fallback at all -- it renders whatever this endpoint sends completely
# verbatim inside a <pre> block. Meanwhile the Apply screen shows clean
# text because it reads the Requirement's own already-cleaned
# job_description (cleaned once, at ingestion, via
# cleaner.clean_requirement_text -- see dedup.py's save_requirement), not
# this endpoint at all -- so the two screens were never guaranteed to
# agree. Computing one correctly-converted "body" HERE, backend-side, using
# the SAME html_to_text() this file already imports (and dedup.py already
# uses for the ingestion-time cleaning that makes the Apply screen work),
# fixes it at the one source both screens should share, instead of leaving
# every caller to reinvent (or skip) its own HTML-to-text conversion.
# Purely additive to the response shape -- every existing key is
# unchanged, this only adds a new "body" key -- so it can't break any
# caller that doesn't already read it. GmailPage.tsx's own emailBody()
# already prefers e.body first when present, so it picks this up
# automatically too, upgrading its own cruder fallback for free rather
# than leaving two different, inconsistent conversions in the codebase.
#
# Defined as its own top-level function, BEFORE get_raw_email, and used
# nowhere else in its own body except its own two parameters -- kept
# fully self-contained on purpose so it can't accidentally end up nested
# inside (or swallowing) the endpoint function below it.
# Characters that render as a space (or nothing) but aren't a plain
# U+0020 -- NBSP, narrow NBSP, thin/figure spaces, zero-width space/joiners,
# word joiner, BOM. Same set as the Gmail search fix further down.
_DISPLAY_INVISIBLE_SPACES = re.compile(
    "[\u00A0\u1680\u180E\u2000-\u200D\u202F\u205F\u3000\u2060\uFEFF]"
)


def _tidy_display_text(text_value: Optional[str]) -> Optional[str]:
    """Whitespace cleanup for the read-only Raw Email Viewer ONLY.

    BUG FIX ("Raw Email Viewer shows big gaps between lines"): the
    text/plain part that mass-mailers generate from their HTML template
    turns every <p>, <br> and &nbsp; spacer paragraph into a newline, so
    each real line arrives followed by 2-6 blank or whitespace-only lines.
    _build_display_body() returned that verbatim and the viewer's
    whitespace-pre-wrap <pre> rendered every one of them.

    Keeps paragraph breaks (one blank line) rather than squashing to single
    newlines like clean_requirement_text() does, so the viewer stays
    readable. Display-only: stored body_text/body_html and parsing are
    untouched.
    """
    if not text_value:
        return text_value
    t = text_value.replace("\r\n", "\n").replace("\r", "\n")
    t = _DISPLAY_INVISIBLE_SPACES.sub(" ", t)
    # Trailing whitespace off every line -> whitespace-only lines become empty.
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    # 3+ newlines (2+ blank lines) -> exactly one blank line.
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip("\n")


def _build_display_body(body_text: Optional[str], body_html: Optional[str]) -> Optional[str]:
    # A real plain-text body needs no conversion. Same "does this actually
    # look like plain text or is it HTML sitting in the wrong column"
    # check clean_requirement_text() itself uses, so a body_text that's
    # actually raw HTML markup (some ingestion paths do this) still gets
    # converted instead of dumped verbatim.
    if body_text and "<html" not in body_text.lower() and "<body" not in body_text.lower():
        return _tidy_display_text(body_text)
    if body_text:
        return _tidy_display_text(html_to_text(body_text))
    if body_html:
        return _tidy_display_text(html_to_text(body_html))
    return None


@router.get(
    "/api/admin/raw-emails/{email_id}",
    summary="Get raw email — checks gmail_emails first, falls back to emails table",
)
async def get_raw_email(
    email_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    BUG FIX: this docstring previously said raw_email_id is a foreign key
    into `emails.id` — that was backwards and contradicted the confirmed
    truth stated elsewhere in this same codebase (pipeline.py,
    requirements_sync.py, and this file's own get_gmail_emails endpoint
    below): requirements.raw_email_id references gmail_emails.id,
    CONFIRMED via pg_constraint. That wrong comment is the likely reason
    models.py's FK got "corrected" back to emails.id at least once —
    trust this codebase's other three confirmations over any comment
    that disagrees with them.

    Checks gmail_emails first since that's where raw_email_id actually
    points; falls back to the emails table only for any legacy rows that
    predate this pipeline (there shouldn't be any going forward).

    BUG FIX ("View Raw shows an error for some requirements, works fine
    for others"): the frontend (RequirementsDebugTable.tsx) sends a
    synthetic "req-{requirementId}" id for any requirement with no real
    raw_email_id, per a comment claiming a Next.js reference build's API
    route has special-case handling for that exact prefix — that route
    doesn't exist in this backend; it was never ported. email_id used to
    be typed `int` here, so FastAPI rejected "req-123" with a 422 before
    this function even ran, for every requirement lacking a real linked
    email. Now accepts the id as a string and special-cases that prefix
    with a clean, honest response instead of an error.
    """
    _require_role(current_user, "ADMIN", "RECRUITER", "CONSULTANT")

    if email_id.startswith("req-"):
        return {
            "source": "none",
            "id": email_id,
            "subject": None,
            "from_address": None,
            "fetched_at": None,
            "body_text": None,
            "body_html": None,
            # The frontend's fetchRawEmail checks `raw.body` first, before
            # falling back through body_text/body_html to a generic
            # "(empty)" — adding this key directly is what actually
            # surfaces this message, rather than the two cases (never
            # had a linked email vs. a linked email with a genuinely
            # empty body) looking identical to the person viewing it.
            "body": "No email is linked to this requirement.",
            "processed": False,
        }

    try:
        email_id_int = int(email_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid email id: {email_id!r}")
    # BUG FIX: this endpoint used to block CONSULTANT entirely, so the
    # "View Raw" button on the consultant's own Requirements page always
    # 403'd. Consultants can view raw email content, but ONLY for a
    # requirement they're actually matched to — not arbitrary emails by ID.
    if current_user.role == "CONSULTANT":
        from models import Consultant, RequirementConsultantMatch
        cons_result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        consultant = cons_result.scalars().first()
        if not consultant:
            raise HTTPException(status_code=403, detail="No consultant profile found.")
        owns_req = await db.execute(
            select(Requirement.id)
            .join(RequirementConsultantMatch, RequirementConsultantMatch.requirement_id == Requirement.id)
            .where(Requirement.raw_email_id == email_id_int, RequirementConsultantMatch.consultant_id == consultant.id)
        )
        if not owns_req.scalars().first():
            raise HTTPException(status_code=403, detail="This email isn't linked to a requirement matched to you.")

    result = await db.execute(
        text("""
            SELECT id, account_id, account_email, message_id, uid, folder,
                   subject, from_address, from_name, to_addresses, cc_addresses,
                   bcc_addresses, reply_to, body_text, body_html, date,
                   is_read, is_starred, has_attachments, attachments, labels,
                   thread_id, raw_headers, fetched_at, category, priority,
                   processed, classified_at, classifier_tier, job_posting_id, status_desc, reason,
                   EXISTS (SELECT 1 FROM requirements r WHERE r.raw_email_id = gmail_emails.id) AS has_requirement
            FROM gmail_emails WHERE id = :id
        """),
        {"id": email_id_int}
    )
    row = result.mappings().first()
    if row:
        return {
            "source": "gmail_emails",
            **dict(row),
            "body": _build_display_body(row["body_text"], row["body_html"]),
        }

    # Fall back to the emails table — this is the correct source for any
    # requirement created after the raw_email_id FK fix.
    email_result = await db.execute(select(Email).where(Email.id == email_id_int))
    email = email_result.scalars().first()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    return {
        "source": "emails",
        "id": email.id,
        "account_email": email.recruiter_email,
        "message_id": email.gmail_message_id,
        "thread_id": email.gmail_thread_id,
        "subject": email.subject,
        "from_address": email.sender_email,
        "from_name": email.sender_name,
        "to_addresses": email.to_addresses,
        "cc_addresses": email.cc_addresses,
        "bcc_addresses": email.bcc_addresses,
        "reply_to": email.reply_to_address,
        "body_text": email.body_text,
        "body_html": email.body_html,
        "body": _build_display_body(email.body_text, email.body_html),
        "date": email.received_at,
        "is_read": email.is_read,
        "is_starred": email.is_starred,
        "has_attachments": email.has_attachments,
        "attachments": email.attachment_details,
        "labels": email.gmail_labels,
        "raw_headers": email.raw_headers,
        "fetched_at": email.fetched_at,
        "processed": email.parse_status in ("PARSED", "SKIPPED"),
        "parse_status": email.parse_status,
    }


@router.post(
    "/api/admin/raw-emails/{email_id}/reparse",
    summary="Actually re-run an email through the parser and refresh its requirement",
)
async def reparse_email(
    email_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Previously this endpoint only flipped gmail_emails.processed = false —
    nothing ever consumed that flag, so "reparse" silently did nothing.

    Real behavior now:
    1. Load the raw email — gmail_emails first (id sent by the frontend),
       falling back to the emails table.
    2. Ensure a corresponding `emails` row exists (creating it if this raw
       email was never run through the pipeline), so we have a real
       emails.id to use as the FK.
    3. Re-run parser + cleaner on the raw text.
    4. If a Requirement is already linked to this email, update it in place
       (this IS a re-parse of the same email, so we intentionally bypass
       the duplicate check rather than silently no-op on unchanged dedup keys).
       Otherwise create a new Requirement via the normal dedup path.
    5. Mark the raw email as processed/parsed.
    """
    # Was ADMIN-only while every other Gmail admin endpoint (list, single
    # raw-email view) allows ADMIN or RECRUITER — a RECRUITER could open
    # the raw email modal but got a 403 (shown generically as "Failed to
    # queue reparse") clicking Reparse. Aligned with the rest of this file.
    _require_role(current_user, "ADMIN", "RECRUITER")

    # ---- Step 1: load raw source (gmail_emails, falling back to emails) ----
    gmail_row_result = await db.execute(
        text("""
            SELECT id, message_id, thread_id, account_email, subject,
                   from_address, from_name, reply_to, body_text, body_html, date,
                   fetched_at
            FROM gmail_emails WHERE id = :id
        """),
        {"id": email_id}
    )
    gmail_row = gmail_row_result.mappings().first()

    if gmail_row:
        gmail_message_id = gmail_row["message_id"]
        subject = gmail_row["subject"] or ""
        body_text = gmail_row["body_text"] or ""
        body_html = gmail_row["body_html"] or ""
        headers = {"from": gmail_row["from_address"], "reply_to": gmail_row["reply_to"]}
        source_gmail_emails_id = gmail_row["id"]
        # Cap sender-clock-skewed Date headers at our own fetch time -- this
        # also covers the in-place update branch below, which writes
        # received_date directly without going through save_requirement().
        received_date = clamp_received_date(gmail_row["date"], gmail_row["fetched_at"])
        gmail_msg = {
            "id": gmail_message_id,
            "thread_id": gmail_row["thread_id"],
            "recruiter_email": gmail_row["account_email"],
            "from_email": gmail_row["from_address"],
            "from_name": gmail_row["from_name"],
            "subject": subject,
            "plain_text_body": body_text,
            "html_body": body_html,
            "reply_to_email": gmail_row["reply_to"],
            "received_at": gmail_row["date"],
        }
    else:
        # Not in gmail_emails — check the emails table directly
        email_result = await db.execute(select(Email).where(Email.id == email_id))
        email = email_result.scalars().first()
        if not email:
            raise HTTPException(status_code=404, detail="Email not found")
        gmail_message_id = email.gmail_message_id
        subject = email.subject or ""
        body_text = email.body_text or ""
        body_html = email.body_html or ""
        headers = {"from": email.sender_email, "reply_to": email.reply_to_address}
        source_gmail_emails_id = None
        gmail_msg = None
        received_date = email.received_at

    try:
        # ---- Step 2: ensure a real emails row exists, get its id ----
        if gmail_msg is not None:
            email_result = await db.execute(
                select(Email).where(Email.gmail_message_id == gmail_message_id)
            )
            email = email_result.scalars().first()
            if not email:
                # Never went through the pipeline before — create it now.
                # source_gmail_emails_id is the real gmail_emails.id (the FK
                # target for requirements.raw_email_id) — must be passed through
                # explicitly, or process_email() would fall back to NULL and
                # this requirement would end up unlinked from its raw email.
                #
                # create_requirements=False: this call exists ONLY to create
                # the missing `emails` bookkeeping row above — the actual
                # parse + Requirement save for THIS reparse happens explicitly
                # below (Step 3), which assumes at most one Requirement row
                # per raw_email_id and updates it in place. An email can now
                # yield multiple Requirement rows via parse_requirements()
                # (see parser.py) — letting this call ALSO create rows would
                # leave any extra ones orphaned: uncounted here, and never
                # revisited by a future reparse of "this" row.
                save_result = await process_email(
                    db, gmail_msg, raw_email_id=source_gmail_emails_id, create_requirements=False
                )
                email_result = await db.execute(
                    select(Email).where(Email.gmail_message_id == gmail_message_id)
                )
                email = email_result.scalars().first()

        # BUG FIX: was `real_email_id = email.id` with no None check — if
        # gmail_message_id is missing/NULL on the source gmail_emails row
        # (happens for some manually-inserted or legacy rows), the lookup
        # above can come back empty even after process_email runs, and this
        # crashed with an unhandled AttributeError ('NoneType' has no
        # attribute 'id') — an opaque 500 with zero detail, and nothing
        # logged anywhere, exactly what showed up in the Network tab with
        # no explanation. Now raises a clear, diagnosable error instead.
        if email is None:
            raise ValueError(
                f"Could not create or locate an emails row for gmail_emails.id={email_id} "
                f"(gmail_message_id={gmail_message_id!r}). This usually means the source "
                f"row has no message_id set."
            )
        real_email_id = email.id

        # requirements.raw_email_id's real FK constraint points at
        # gmail_emails.id, NOT emails.id (confirmed via pg_constraint — see
        # requirements_sync.py header comment for the full story). Using
        # emails.id here was the same bug that used to break the sync job:
        # it either violates the FK outright (500) or, worse, silently
        # matches the wrong gmail_emails row on small overlapping ids.
        # Prefer the true gmail_emails.id; only fall back to emails.id for
        # the legacy case where this email never had a gmail_emails row.
        fk_raw_email_id = source_gmail_emails_id if source_gmail_emails_id is not None else real_email_id

        # ---- Step 3: re-run parser + cleaner on the raw text ----
        body = body_text or html_to_text(body_html)
        parsed = parse_requirement(subject, body, headers)
        cleaned_jd = clean_requirement_text(body)

        # ---- Step 4: update existing requirement in place, or create one ----
        existing_req_result = await db.execute(
            select(Requirement).where(Requirement.raw_email_id == fk_raw_email_id)
        )
        existing_req = existing_req_result.scalars().first()

        vendor_email = parsed.get("vendor_email", "unknown@unknown.com")
        role = parsed.get("role", "UNKNOWN")
        jd_hash = create_jd_hash(cleaned_jd)
        dedup_key = build_dedup_key(vendor_email, role, jd_hash)

        if existing_req:
            existing_req.role = role
            existing_req.vendor = parsed.get("vendor")
            existing_req.vendor_email = vendor_email
            existing_req.vendor_contact = parsed.get("vendor_contact")
            existing_req.client = parsed.get("client")
            existing_req.location = parsed.get("location")
            existing_req.work_mode = parsed.get("work_mode")
            existing_req.employment_types = parsed.get("employment_types", ["UNKNOWN"])
            existing_req.rate = parsed.get("rate")
            existing_req.duration = parsed.get("duration")
            existing_req.job_description = cleaned_jd
            existing_req.jd_hash = jd_hash
            existing_req.dedup_key = dedup_key
            # experience/skills aren't real columns on Requirement (they live
            # inside parsed_fields) — assigning them directly was a no-op that
            # silently dropped the data; parsed_fields below carries them.
            existing_req.parsed_fields = parsed
            existing_req.parse_confidence = parsed.get("parse_confidence", 0.0)
            if received_date and not existing_req.received_date:
                existing_req.received_date = received_date
            await db.commit()
            requirement_status = "updated"
            requirement_id = existing_req.id
        else:
            result = await save_requirement(
                db=db, parsed=parsed, cleaned_jd=cleaned_jd, raw_email_id=fk_raw_email_id,
                received_date=received_date,
            )
            requirement_status = result["status"]
            requirement_id = result["id"]

        # ---- Step 5: mark processed/parsed on whichever raw source we used ----
        # BUG FIX ("Reparse a 'Parsed - Dup' email -> briefly shows Parsed,
        # then corrects itself back to Parsed - Dup a little later" --
        # confirmed real case, and worse than a display glitch): this
        # unconditionally hardcoded status_desc = 'Pending', regardless of
        # what requirement_status (available right above, from Step 4) had
        # just determined. 'Pending' isn't a display bug in isolation --
        # it's requirements_sync.py's own SELECT criteria for "still needs
        # processing" (`status_desc IS NULL OR = 'Pending' OR = 'Failed'`,
        # combined with `NOT EXISTS (... WHERE raw_email_id = ge.id)`,
        # which a correctly-identified duplicate always satisfies since it
        # has no Requirement of its own). So every reparse -- duplicate or
        # not -- silently made this email eligible to be picked up and
        # reprocessed AGAIN by the next background sync cycle: a second,
        # completely invisible, wasted AI call for every single reparse.
        # For a duplicate, THAT second pass is what actually set the
        # correct 'Parsed - Dup' -- the "self-correction" the person saw
        # was this hidden extra reprocessing quietly doing the job Step 5
        # should have done immediately. Mapping requirement_status directly
        # to the same status_desc convention requirements_sync.py's own
        # final_status logic already uses ("duplicate" -> "Parsed - Dup",
        # everything else -> "Parsed") sets the right value the first time
        # and removes it from the background loop's pending queue for good.
        status_desc = "Parsed - Dup" if requirement_status == "duplicate" else "Parsed"
        if source_gmail_emails_id is not None:
            await db.execute(
                text("UPDATE gmail_emails SET processed = true, status_desc = :status_desc WHERE id = :id"),
                {"id": source_gmail_emails_id, "status_desc": status_desc}
            )
        email.parse_status = "PARSED"
        await db.commit()
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        print(f"[reparse_email] FAILED for email_id={email_id}: {e}")
        from error_logger import log_db_error
        await log_db_error(
            stage="reparse_email",
            error=e,
            source_type="gmail_email",
            source_id=str(email_id),
        )
        raise HTTPException(status_code=500, detail=f"Reparse failed: {e}")

    # BUG FIX: nothing here ever ran matching, so a manually reparsed
    # requirement's match count stayed at 0 the same way auto-synced
    # ones did (see requirements_sync.py) until an admin separately
    # clicked "Rematch"/"Match All". Trigger it here too so reparse
    # always leaves the requirement with a real match count.
    if requirement_id is not None:
        try:
            # Single engine — one call refreshes RequirementConsultantMatch
            # directly; there's no second table left to catch up separately.
            from phase4 import match_requirement
            await match_requirement(db, requirement_id)
        except Exception as match_err:
            print(f"[reparse_email] auto-match FAILED for requirement_id={requirement_id}: {match_err}")
            from error_logger import log_db_error
            await log_db_error(
                stage="reparse_email_automatch",
                error=match_err,
                source_type="requirement",
                source_id=requirement_id,
            )

    logger.info(
        "Reparsed email_id=%s (emails.id=%s) -> requirement_status=%s requirement_id=%s by user=%s",
        email_id, real_email_id, requirement_status, requirement_id, current_user.email,
    )

    return {
        "success": True,
        "message": f"Email {email_id} reparsed",
        "requirement_status": requirement_status,
        "requirement_id": str(requirement_id) if requirement_id is not None else None,
    }


# BUG FIX ("date_from shows a full UTC timestamp in the Network tab, and
# the filtered results were wrong" — confirmed: date_from=2026-08-27
# T00:00:00Z, off from the actual start of that CST calendar day): this
# endpoint used to require the CALLER to build a full ISO timestamp
# (date_from.replace("Z", "+00:00") then datetime.fromisoformat(...)) —
# the frontend was doing that by naively appending "T00:00:00Z", silently
# treating the date as UTC midnight when every caller actually means a
# CST (US Central) calendar day. Now accepts a bare "YYYY-MM-DD" (as
# well as still accepting a full timestamp, for any other caller that
# already sends one) and does the CST conversion here instead, matching
# the same interval this admin team already works in everywhere else in
# this app. A bare date_from becomes the start of that day in CST; a
# bare date_to becomes the end of that same day in CST (23:59:59.999999)
# — not the start of it, since date <= :date_to would otherwise exclude
# nearly the entire day. Uses a real IANA zone (America/Chicago) rather
# than a fixed offset so the CST (UTC-6) / CDT (UTC-5) daylight-saving
# switch is handled automatically.
#
# NOTE: this is intentionally CST, not IST — confirmed as the correct
# convention for this endpoint (matches phase8.py's separate, established
# _CST_ZONE for this admin area's other date filters). The caller
# (admin.api.ts's countEmailsIngestedToday()) must send a CST-based date
# key, not an IST one — see that file's own fix for the matching half of
# this. Sending an IST date key here, against this CST interpretation,
# reintroduces a ~10.5 hour window mismatch and undercounts "today" for
# roughly the first third of the IST calendar day.
_CST_ZONE = ZoneInfo("America/Chicago")


def _parse_admin_date_filter(value: str, end_of_day: bool) -> datetime:
    value = value.strip()
    if "T" in value or " " in value:
        # Already a full timestamp — parse as before, unchanged.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    cst_naive = datetime.fromisoformat(value)
    if end_of_day:
        cst_naive = cst_naive.replace(hour=23, minute=59, second=59, microsecond=999999)
    cst_aware = cst_naive.replace(tzinfo=_CST_ZONE)
    return cst_aware.astimezone(timezone.utc)


@router.get(
    "/api/admin/gmail-emails",
    summary="Get all emails from gmail_emails table — all columns included",
)
async def get_gmail_emails(
    page: int = 1,
    page_size: int = 20,
    account_email: Optional[str] = None,
    category: Optional[str] = None,
    processed: Optional[bool] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    # light=true: list view only — skip the heavy body/header/address columns
    # (the View modal fetches the full email via /api/admin/raw-emails/{id}).
    light: bool = False,
    # search_body=true also searches the email body (slow on a large table).
    # Default: subject / from name / from address only — fast.
    search_body: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER")

    page = max(1, page)
    page_size = max(1, min(page_size, 200))

    # Build WHERE clause
    where_clauses = []
    params = {"limit": page_size, "offset": (page - 1) * page_size}

    if account_email:
        where_clauses.append("account_email = :account_email")
        params["account_email"] = account_email
    if category:
        where_clauses.append("category = :category")
        params["category"] = category
    if processed is not None:
        where_clauses.append("processed = :processed")
        params["processed"] = processed
    if search:
        # BUG FIX ("search shows 'no results' for a requirement that
        # genuinely exists" -- confirmed real case): only subject,
        # from_address, and from_name were ever searched -- never the
        # actual email body. That works whenever the search term happens
        # to be repeated in the subject line (common for role names,
        # since many recruiters put the title there), and silently fails
        # for anything only present in the body -- a skill, a client
        # name, a role phrased differently in the body than the subject,
        # or a subject that's just a generic "Urgent Requirement" with no
        # identifying text at all. Adding body_text/body_html so a real
        # match anywhere in the email is actually found.
        #
        # BUG FIX ("no results for SOME requirements, even when the
        # subject text is clearly right there" -- confirmed real case):
        # a subject/body originating from an HTML email frequently
        # contains &nbsp; entities, which decode to U+00A0 (a NON-
        # BREAKING space), not a regular space (U+0020). It renders
        # visually identical to a normal space in any UI, so a person
        # typing a search term with an ordinary keyboard space at that
        # word boundary produces a search string that is byte-for-byte
        # different from the stored text at exactly that point --
        # ILIKE's substring match is exact about whitespace, so it
        # silently fails right there even though the text "obviously"
        # matches to the eye. The same HTML-to-text flattening can also
        # leave runs of multiple consecutive spaces where a person would
        # only type one. Normalizing BOTH sides -- the stored columns
        # (regexp_replace, converting nbsp to a plain space and
        # collapsing whitespace runs) and the incoming search term
        # (same normalization in Python, right below) -- means a person
        # can search with ordinary single spaces and match regardless of
        # what invisible whitespace variant the source HTML happened to
        # decode into.
        #
        # BUG FIX ("Mainframe Automation Engineer" -- an exact, plain-
        # looking SUBJECT, already parsed and visibly sitting in the
        # list -- returned "No matching emails found"): confirmed via a
        # live query test that chr(160) (plain NBSP) was only ONE of
        # several Unicode characters that render as an ordinary-looking
        # (or entirely invisible) space but are byte-for-byte different
        # from U+0020. Subjects that pass through Word/Outlook
        # autocorrect, ATS templates, or copy-pasted job titles commonly
        # carry narrow no-break spaces (U+202F), thin/figure/punctuation
        # spaces (U+2000-U+200A), ideographic space (U+3000), Ogham space
        # mark (U+1680), or genuinely invisible characters like zero-
        # width space (U+200B), zero-width joiner/non-joiner (U+200C/D),
        # word joiner (U+2060), and the zero-width no-break
        # space/BOM (U+FEFF). None of these are matched by Postgres's
        # regex \s (ASCII-only, unlike Python's), so each one slipped
        # straight through the old chr(160)-only fix and broke the
        # search at exactly that word boundary -- same failure mode as
        # the original nbsp bug, just a different character. Folding
        # this whole set to a plain space (not stripping them outright --
        # removing a zero-width character between two words would glue
        # them into one word and reintroduce the same false-negative)
        # before collapsing whitespace runs closes the entire class of
        # "looks like a space, isn't a space" bug at once, on both the
        # column side (SQL) and the search term side (Python, below).
        #
        # BUG FIX ("search shows 'No matching emails found' even though the
        # row is visibly on the page"): the previous fix ran TWO
        # regexp_replace() passes over every column -- including the full
        # body_html -- for every row, twice per request (COUNT + page).
        # Benchmarked at ~10s per query on 5k emails, so ~20s+ per request,
        # past the frontend's 30s fetch timeout once the table grows. The
        # timed-out request left the list empty, which the UI rendered as
        # "No matching emails found".
        #
        # Same whitespace robustness, no per-row regex: the SEARCH TERM is
        # split into words and joined with '%' (e.g. "Java Developer" ->
        # %Java%Developer%). Whatever sits between the words in the stored
        # text -- NBSP, narrow NBSP, zero-width space, double spaces -- is
        # absorbed by the '%' wildcard, so no column normalization is
        # needed. body_html is only searched when body_text is empty
        # (body_text already holds the same content as plain text).
        # Benchmarked at ~0.18s on the same 5k-email dataset.
        _INVISIBLE_SPACE_CHARS = (
            "\u00A0\u1680\u180E\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
            "\u2007\u2008\u2009\u200A\u200B\u200C\u200D\u202F\u205F\u3000"
            "\u2060\uFEFF"
        )
        _search_table = str.maketrans({c: " " for c in _INVISIBLE_SPACE_CHARS})
        # EXACT-PHRASE FIX ("Senior ServiceNow Developer REMOTE" also
        # returned "...Developer 100% Remote", "...FSO Developer || Remote",
        # etc.): the previous %word%word% ILIKE pattern allowed ANY text
        # between the search words. Now a case-insensitive regex (~*) where
        # the words must be separated ONLY by whitespace -- ordinary spaces
        # or any of the invisible/lookalike spaces above (NBSP, narrow NBSP,
        # zero-width space, ...), one or more of them. So the phrase must
        # appear exactly, but invisible-space and double-space variants
        # still match. Every non-alphanumeric character the user types --
        # ( ) + : & | – etc. -- is backslash-escaped so it matches
        # literally, not as regex syntax. pg_trgm GIN indexes support ~*,
        # so the existing ix_gmail_emails_*_trgm indexes are still used.
        _ws_class = (
            r"[[:space:]\u00A0\u1680\u180E\u2000-\u200D\u202F\u205F\u3000\u2060\uFEFF]+"
        )
        _words = [
            re.sub(r"([^0-9A-Za-z])", r"\\\1", w)
            for w in search.translate(_search_table).split()
        ]
        # MERGE RESOLUTION (HEAD vs 1196aa0): kept HEAD's indexed whole-field
        # regex match for subject / from_address / from_name. 1196aa0's
        # regexp_replace(...) ILIKE :search clause was dropped: it could not
        # use the pg_trgm indexes (full-table regex on 253k rows), and ANDed
        # with this clause it compared a regex pattern via ILIKE, so every
        # search would have returned nothing. Its search_body opt-in is kept
        # below: when search_body=true, the body is ALSO searched for the
        # phrase anywhere in body_text (unanchored -- a body never equals the
        # search). No index covers the body, so this is slow on a large
        # table; it only runs when explicitly requested. Default (false) is
        # fast: subject / sender only.
        if _words:
            _clause = (
                "(subject ~* :search"
                " OR from_address ~* :search"
                " OR from_name ~* :search"
            )
            if search_body:
                _clause += " OR body_text ~* :search_phrase"
                params["search_phrase"] = _ws_class.join(_words)
            where_clauses.append(_clause + ")")
            # WHOLE-FIELD MATCH: anchored with ^...$ so the entire subject
            # (or sender address / name) must equal the search -- nothing
            # before or after it. Leading/trailing whitespace (incl. the
            # invisible variants) is tolerated. "Alteryx Developer" matches
            # only a subject that is exactly "Alteryx Developer", not
            # "BI with Alteryx Developer - Charlotte, NC".
            _ws_opt = _ws_class[:-1] + "*"
            params["search"] = "^" + _ws_opt + _ws_class.join(_words) + _ws_opt + "$"
    if date_from:
        where_clauses.append("date >= :date_from")
        try:
            params["date_from"] = _parse_admin_date_filter(date_from, end_of_day=False)
        except ValueError:
            params["date_from"] = date_from
    if date_to:
        where_clauses.append("date <= :date_to")
        try:
            params["date_to"] = _parse_admin_date_filter(date_to, end_of_day=True)
        except ValueError:
            params["date_to"] = date_to

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    # Count total
    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM gmail_emails {where_sql}"),
        params
    )
    total = count_result.scalar_one()

    # BUG FIX ("new mail not showing immediately"): this sorted by ge.date
    # — the sender's own "Date:" header, copied through as-is from the
    # source email. That value is out of our control and has already
    # shown up skewed (the Gmail admin page displayed a "received" email
    # dated hours in the future — see fmtDate's own history). A newly-
    # ingested email with an off header date can sort anywhere in this
    # list instead of at the top, so it doesn't appear "immediately" even
    # though the row landed in the table the moment it was fetched.
    # ge.fetched_at is OUR OWN timestamp, stamped by this app at ingestion
    # time — it's reliably monotonic, so ordering by it guarantees the
    # most-recently-ingested mail is always first. Falls back to ge.date
    # only for legacy rows that predate fetched_at being populated.
    if light:
        select_cols = """
            ge.id, ge.account_email, ge.subject, ge.from_address, ge.from_name,
                   ge.date, ge.fetched_at, ge.category, ge.processed,
                   ge.status_desc, ge.reason,"""
    else:
        select_cols = """
            ge.id, ge.account_id, ge.account_email, ge.message_id, ge.uid, ge.folder,
                   ge.subject, ge.from_address, ge.from_name, ge.to_addresses, ge.cc_addresses,
                   ge.bcc_addresses, ge.reply_to, ge.body_text, ge.body_html, ge.date,
                   ge.is_read, ge.is_starred, ge.has_attachments, ge.attachments, ge.labels,
                   ge.thread_id, ge.raw_headers, ge.fetched_at, ge.category, ge.priority,
                   ge.processed, ge.classified_at, ge.classifier_tier, ge.job_posting_id,
                   ge.status_desc, ge.reason,"""

    result = await db.execute(
        text(f"""
            SELECT {select_cols}
                   EXISTS (
                       SELECT 1 FROM requirements r WHERE r.raw_email_id = ge.id
                   ) AS has_requirement
            FROM gmail_emails ge
            {where_sql}
            ORDER BY COALESCE(ge.fetched_at, ge.date) DESC
            LIMIT :limit OFFSET :offset
        """),
        params
    )
    rows = result.mappings().all()

    return {
        "data": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total else 0
    }


# ---------------------------------------------------------------------------
# Gmail Accounts endpoints — reads from gmail_accounts table
# All columns included as per table structure
# ---------------------------------------------------------------------------

@router.get(
    "/api/admin/gmail-accounts",
    summary="Get all Gmail accounts — all columns included",
)
async def get_gmail_accounts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")
    result = await db.execute(
        text("""
            SELECT id, email, label, full_name, phone,
                   skills, resume_url, imap_host, imap_port,
                   active, last_synced, last_uid,
                   sync_errors, created_at, updated_at
            FROM gmail_accounts
            ORDER BY id
        """)
    )
    rows = result.mappings().all()
    return {"data": [dict(row) for row in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Gmail Sync Logs endpoints — reads from gmail_sync_logs table
# ---------------------------------------------------------------------------

@router.get(
    "/api/admin/gmail-sync-logs",
    summary="Get Gmail sync logs",
)
async def get_gmail_sync_logs(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")

    count_result = await db.execute(text("SELECT COUNT(*) FROM gmail_sync_logs"))
    total = count_result.scalar_one()

    result = await db.execute(
        text("""
            SELECT id, account_id, account_email, started_at,
                   finished_at, emails_found, emails_saved,
                   status, error_msg, duration_ms
            FROM gmail_sync_logs
            ORDER BY started_at DESC
            LIMIT :limit OFFSET :offset
        """),
        {"limit": page_size, "offset": (page - 1) * page_size}
    )
    rows = result.mappings().all()

    return {
        "data": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total else 0
    }