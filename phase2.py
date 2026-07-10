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
from dedup import create_jd_hash, build_dedup_key, save_requirement

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
    ats_match_count: Optional[int] = None
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
        ats_match_count=requirement.ats_match_count,
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

@router.get(
    "/api/admin/raw-emails/{email_id}",
    summary="Get raw email — checks gmail_emails first, falls back to emails table",
)
async def get_raw_email(
    email_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    NOTE: Requirement.raw_email_id is a foreign key into `emails.id`, not
    `gmail_emails.id` — those are two different tables with two different
    ID sequences (see pipeline.py / gmail_reader.py fix). Older requirements
    created before that fix may still carry a gmail_emails.id, so we check
    gmail_emails first for backward compatibility, then fall back to the
    emails table — which is where every correctly-linked raw_email_id
    actually points.
    """
    _require_role(current_user, "ADMIN", "RECRUITER")

    result = await db.execute(
        text("""
            SELECT id, account_id, account_email, message_id, uid, folder,
                   subject, from_address, from_name, to_addresses, cc_addresses,
                   bcc_addresses, reply_to, body_text, body_html, date,
                   is_read, is_starred, has_attachments, attachments, labels,
                   thread_id, raw_headers, fetched_at, category, priority,
                   processed, classified_at, classifier_tier, job_posting_id
            FROM gmail_emails WHERE id = :id
        """),
        {"id": email_id}
    )
    row = result.mappings().first()
    if row:
        return {"source": "gmail_emails", **dict(row)}

    # Fall back to the emails table — this is the correct source for any
    # requirement created after the raw_email_id FK fix.
    email_result = await db.execute(select(Email).where(Email.id == email_id))
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
                   from_address, from_name, reply_to, body_text, body_html, date
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
        received_date = gmail_row["date"]
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
            save_result = await process_email(db, gmail_msg, raw_email_id=source_gmail_emails_id)
            email_result = await db.execute(
                select(Email).where(Email.gmail_message_id == gmail_message_id)
            )
            email = email_result.scalars().first()

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
    if source_gmail_emails_id is not None:
        await db.execute(
            text("UPDATE gmail_emails SET processed = true WHERE id = :id"),
            {"id": source_gmail_emails_id}
        )
    email.parse_status = "PARSED"
    await db.commit()

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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER")

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

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    # Count total
    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM gmail_emails {where_sql}"),
        params
    )
    total = count_result.scalar_one()

    # Get data with ALL columns
    result = await db.execute(
        text(f"""
            SELECT id, account_id, account_email, message_id, uid, folder,
                   subject, from_address, from_name, to_addresses, cc_addresses,
                   bcc_addresses, reply_to, body_text, body_html, date,
                   is_read, is_starred, has_attachments, attachments, labels,
                   thread_id, raw_headers, fetched_at, category, priority,
                   processed, classified_at, classifier_tier, job_posting_id
            FROM gmail_emails
            {where_sql}
            ORDER BY date DESC
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
