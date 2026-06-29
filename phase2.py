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
#
# Note: GET /api/requirements (list, paginated, filterable) already exists
# in main.py — not duplicated here.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, case, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, Requirement
from auth import get_current_user
from pipeline import process_email
from parser import parse_requirement

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
    """
    Registered before /api/requirements/{requirement_id} at the router level
    is not required here since this is a distinct literal path ('stats' is not
    a valid {requirement_id} value that would collide at the FastAPI routing layer
    only if declared in the same router in the wrong order) — kept first for clarity.
    """
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
    """
    Admin-only manual trigger. In production this same process_email() function
    is intended to be called by a background Gmail-polling worker (not yet built —
    that's Developer 4's task per the doc). This endpoint exists so the pipeline
    can be exercised and verified without that worker existing yet.
    """
    _require_role(current_user, "ADMIN")

    # model_dump() includes keys with value None for unset Optional fields, which
    # defeats gmail_reader.py's dict.get(key, default) fallback pattern (.get only
    # applies its default when the key is MISSING, not when present-but-None).
    # Strip None values here so downstream Phase 2 logic files behave as designed.
    gmail_msg = {k: v for k, v in payload.model_dump().items() if v is not None}

    result = await process_email(db, gmail_msg)

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
    """
    Admin-only diagnostic endpoint — runs parser.parse_requirement() directly,
    no DB writes. Useful for validating regex patterns against real vendor
    email samples before trusting the pipeline end-to-end.
    """
    _require_role(current_user, "ADMIN")

    headers = {}
    if payload.from_header:
        headers["from"] = payload.from_header
    if payload.reply_to:
        headers["reply_to"] = payload.reply_to

    parsed = parse_requirement(subject=payload.subject, body=payload.body, headers=headers)
    return parsed


# ---------------------------------------------------------------------------
# Raw Email endpoints — reads from gmail_emails table
# ---------------------------------------------------------------------------

from sqlalchemy import text

@router.get(
    "/admin/raw-emails/{email_id}",
    summary="Get raw email from gmail_emails table",
)
async def get_raw_email(
    email_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER")
    result = await db.execute(
        text("SELECT * FROM gmail_emails WHERE id = :id"),
        {"id": email_id}
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Email not found")
    return dict(row)


@router.post(
    "/admin/raw-emails/{email_id}/reparse",
    summary="Mark email for reprocessing",
)
async def reparse_email(
    email_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")
    await db.execute(
        text("UPDATE gmail_emails SET processed = false WHERE id = :id"),
        {"id": email_id}
    )
    await db.commit()
    return {"success": True, "message": f"Email {email_id} queued for reparse"}


@router.get(
    "/admin/gmail-emails",
    summary="Get all emails from gmail_emails table for admin view",
)
async def get_gmail_emails(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER")
    import math
    count_result = await db.execute(text("SELECT COUNT(*) FROM gmail_emails"))
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    result = await db.execute(
        text("""
            SELECT id, account_email, subject, from_address,
                   from_name, date, is_read, category,
                   priority, processed, folder
            FROM gmail_emails
            ORDER BY date DESC
            LIMIT :limit OFFSET :offset
        """),
        {"limit": page_size, "offset": offset}
    )
    rows = result.mappings().all()
    return {
        "data": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size)
    }
