# phase8.py
# ---------------------------------------------------------------------------
# Phase 8 — Admin Monitoring & Observability
#
# Endpoints: audit logs, error queue, manual review, AI usage/cost tracking,
# dashboard stats, system health, duplicates view, applications view.
#
# Auth: uses your existing auth.py SECRET_KEY/ALGORITHM via decode_access_token.
# Admin-only — every route requires role == "ADMIN".
#
# Does NOT touch your existing User/Consultant/Requirement/Application tables
# for business logic — only reads/writes its own 6 monitoring tables.
# ---------------------------------------------------------------------------

import csv
import io
import math
from datetime import datetime, timezone, date, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, text
from sqlalchemy.future import select as fselect

from database import get_db
from auth import decode_access_token
from models import (
    AuditLog,
    ProcessingError,
    ManualReviewQueue,
    AIUsageLog,
    AppSetting,
)
from phase8_audit_service import log_action, build_metadata_preview
from phase8_ai_usage_service import get_budget_threshold, set_budget_threshold, estimate_cost, get_claude_rate_limits
from phase8_retry_service import attempt_retry
from phase8_cache import cache_get, cache_set, check_redis_health

router = APIRouter(prefix="/api/v1/admin", tags=["Phase 8 - Admin Monitoring"])


# ---------------------------------------------------------------------------
# Auth dependency — built on your existing auth.py, Bearer-token style
# (separate from your cookie-based get_current_user, since Swagger/admin
# tools typically send "Authorization: Bearer <token>")
# ---------------------------------------------------------------------------

from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


async def require_admin(request: Request, token: Optional[str] = Depends(oauth2_scheme)) -> dict:
    """
    Accepts only Bearer token from the Authorization header.
    Returns the decoded JWT payload as a dict: {"sub": email, "role": ...}.
    """
    if not token:
        # Fallback to check header manually in case Swagger/OAuth2PasswordBearer doesn't catch it
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_access_token(token)
    if payload.get("role") != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    return payload


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AuditLogRowDTO(BaseModel):
    id: str
    actor_name: Optional[str] = None
    actor_role: Optional[str] = None
    action: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    metadata_preview: str = ""
    ip_address: Optional[str] = None
    created_at: str


class PaginatedAuditLogsDTO(BaseModel):
    data: List[AuditLogRowDTO]
    total: int
    page: int
    page_size: int
    total_pages: int


class CSVExportResponseDTO(BaseModel):
    success: bool
    message: str


class ProcessingErrorRowDTO(BaseModel):
    id: str
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    error_stage: str
    error_message: str
    status: str
    retry_count: int
    occurred_at: str
    resolved_at: Optional[str] = None


class PaginatedErrorsDTO(BaseModel):
    data: List[ProcessingErrorRowDTO]
    total: int
    page: int
    page_size: int
    total_pages: int


class ReviewQueueRowDTO(BaseModel):
    id: str
    error_id: str
    status: str
    review_notes: Optional[str] = None
    created_at: str


class PaginatedReviewDTO(BaseModel):
    data: List[ReviewQueueRowDTO]
    total: int
    page: int
    page_size: int
    total_pages: int


class ReviewActionRequest(BaseModel):
    notes: Optional[str] = None
    correction_data: Optional[dict] = None


class ClaudeUsageDTO(BaseModel):
    tokens_limit: int
    tokens_remaining: int
    tokens_used_pct: float
    tokens_reset: str

class AIUsageStatsDTO(BaseModel):
    total_cost_usd: float
    total_calls: int
    budget_usd: float
    budget_used_pct: float


class SetBudgetRequest(BaseModel):
    budget_usd: float = Field(..., gt=0, description="AI spend budget in USD, must be greater than 0")


class AdminStatsDTO(BaseModel):
    total_audit_events: int
    open_errors: int
    pending_reviews: int
    total_ai_cost_usd: float

class ClassifierHealthDTO(BaseModel):
    unclassified_count: int
    processed_last_5_min: int
    last_classified_at: Optional[datetime] = None
    minutes_since_last_cycle: Optional[float] = None
    status: str  # "healthy" | "stuck" | "no_recent_activity"

class HealthIndicator(BaseModel):
    name: str
    status: str
    detail: Optional[str] = None


class SystemHealthDTO(BaseModel):
    status: str
    indicators: List[HealthIndicator]


class DuplicateGroupDTO(BaseModel):
    dedup_key: str
    role: str
    vendor_email: Optional[str] = None
    count: int
    first_seen: str
    last_seen: str
    raw_email_ids: List[str] = []


class PaginatedDuplicatesDTO(BaseModel):
    data: List[DuplicateGroupDTO]
    total: int
    page: int
    page_size: int
    total_pages: int


class ApplicationSentRowDTO(BaseModel):
    id: str
    timestamp: str
    consultant_name: str
    consultant_id: str
    requirement_id: str
    role: str
    vendor_email: str
    ats_score: Optional[float] = None
    resume_id: Optional[str] = None
    status: str
    candidate_id: Optional[str] = None
    job_posting_id: Optional[str] = None
    match_score: Optional[float] = None
    applied_at: Optional[str] = None


class PaginatedApplicationsDTO(BaseModel):
    data: List[ApplicationSentRowDTO]
    total: int
    page: int
    page_size: int
    total_pages: int


# ---------------------------------------------------------------------------
# Audit Logs
# ---------------------------------------------------------------------------

@router.get("/audit-logs", response_model=PaginatedAuditLogsDTO)
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    filters = []
    if action:
        filters.append(AuditLog.action == action)
    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)
    if date_from:
        filters.append(AuditLog.created_at >= datetime.fromisoformat(date_from))
    if date_to:
        filters.append(AuditLog.created_at <= datetime.fromisoformat(date_to + "T23:59:59+00:00"))
    if search:
        filters.append(AuditLog.actor_name.ilike(f"%{search}%"))

    base_filter = and_(*filters) if filters else True

    total = (await db.execute(select(func.count()).select_from(AuditLog).where(base_filter))).scalar_one()

    rows = (await db.execute(
        select(AuditLog).where(base_filter).order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    data = [
        AuditLogRowDTO(
            id=str(r.id),
            actor_name=r.actor_name,
            actor_role=r.actor_role,
            action=r.action,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            metadata_preview=build_metadata_preview(r.meta),
            ip_address=r.ip_address,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]
    return PaginatedAuditLogsDTO(
        data=data, total=total, page=page, page_size=page_size,
        total_pages=math.ceil(total / page_size) or 1,
    )


class AuditLogFacetsDTO(BaseModel):
    actions: List[str]
    entity_types: List[str]


@router.get("/audit-logs/facets", response_model=AuditLogFacetsDTO)
async def get_audit_log_facets(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """
    Real distinct action/entity_type values currently present in
    audit_logs — used to build the Action Type / Entity Type filter
    options dynamically.

    BUG FIX: the frontend's Action Type filter chips and Entity Type
    dropdown were a hardcoded guessed list (LOGIN, LOGOUT, GENERATE, SEND,
    CONNECT, DISCONNECT, MATCH, ERROR, PARSE, ARCHIVE / User, Requirement,
    Consultant, Resume, Email, OAuth, System) that doesn't match what this
    codebase actually logs anywhere (the real values written by
    log_action() are things like USER_CREATED, USER_UPDATED, USER_DELETED,
    USER_STATUS_CHANGED, CONSULTANT_ASSIGNED, ERROR_RETRY,
    REVIEW_APPROVED, REVIEW_REJECTED / User, Consultant, ManualReviewQueue,
    ProcessingError). Since the backend filter does an exact match
    (`AuditLog.action == action`), clicking any of the guessed chips
    filtered for a value that had never once been logged — always
    returning zero rows, which is exactly why the filter looked broken.
    """
    actions_result = await db.execute(
        select(AuditLog.action).where(AuditLog.action.isnot(None)).distinct().order_by(AuditLog.action)
    )
    entity_types_result = await db.execute(
        select(AuditLog.entity_type).where(AuditLog.entity_type.isnot(None)).distinct().order_by(AuditLog.entity_type)
    )
    return AuditLogFacetsDTO(
        actions=[row[0] for row in actions_result.all()],
        entity_types=[row[0] for row in entity_types_result.all()],
    )


# ---------------------------------------------------------------------------
# Applications management
# ---------------------------------------------------------------------------

from models import Application, Requirement, Consultant

@router.get("/applications", response_model=PaginatedApplicationsDTO)
async def list_applications(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    consultant_id: Optional[str] = None,
    requirement_id: Optional[str] = None,
    status: Optional[str] = None,
    sort_dir: str = Query("desc"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
) -> PaginatedApplicationsDTO:
    """Return paginated applications with optional filters."""
    filters = []
    if consultant_id:
        try:
            filters.append(Application.consultant_id == int(consultant_id))
        except ValueError:
            pass
    if requirement_id:
        try:
            filters.append(Application.requirement_id == int(requirement_id))
        except ValueError:
            pass
    if status:
        filters.append(Application.status == status)
    base_filter = and_(*filters) if filters else True

    total = (await db.execute(select(func.count()).select_from(Application).where(base_filter))).scalar_one()
    
    q = select(Application, Requirement, Consultant) \
        .outerjoin(Requirement, Requirement.id == Application.requirement_id) \
        .outerjoin(Consultant, Consultant.id == Application.consultant_id) \
        .where(base_filter)

    order = Application.created_at.desc() if sort_dir == "desc" else Application.created_at.asc()
    
    results = (await db.execute(
        q.order_by(order)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()

    data = [
        ApplicationSentRowDTO(
            id=str(app.id),
            timestamp=app.created_at.isoformat() if app.created_at else "",
            consultant_name=cons.full_name if cons else str(app.consultant_id),
            consultant_id=str(app.consultant_id),
            requirement_id=str(app.requirement_id),
            role=req.role if req else "UNKNOWN",
            vendor_email=app.vendor_email or "",
            ats_score=float(app.ats_score_at_send) if app.ats_score_at_send else None,
            resume_id=str(app.generated_resume_id) if app.generated_resume_id else None,
            status=app.status,
            candidate_id=app.candidate_id,
            job_posting_id=str(app.job_posting_id) if app.job_posting_id else None,
            match_score=float(app.match_score) if app.match_score else None,
            applied_at=app.applied_at.isoformat() if app.applied_at else None,
        )
        for app, req, cons in results
    ]
    return PaginatedApplicationsDTO(
        data=data,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) or 1,
    )

@router.post("/applications", response_model=ApplicationSentRowDTO)
async def create_application(
    payload: ApplicationSentRowDTO,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
) -> ApplicationSentRowDTO:
    """Create a new application record. UI supplies required fields via payload."""
    new_app = Application(
        consultant_id=payload.consultant_id,
        requirement_id=payload.requirement_id,
        vendor_email=payload.vendor_email,
        status=payload.status,
        ats_score_at_send=payload.ats_score,
    )
    db.add(new_app)
    await db.commit()
    await db.refresh(new_app)
    return ApplicationSentRowDTO(
        id=str(new_app.id),
        timestamp=new_app.created_at.isoformat() if new_app.created_at else "",
        consultant_name=payload.consultant_name,
        consultant_id=new_app.consultant_id,
        requirement_id=new_app.requirement_id,
        role=payload.role,
        vendor_email=new_app.vendor_email,
        ats_score=None,
        resume_id=None,
        status=new_app.status,
    )


@router.get("/audit-logs/export")
async def export_audit_logs_csv(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(5000))).scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "actor_name", "actor_role", "action", "entity_type", "entity_id", "created_at"])
    for r in rows:
        writer.writerow([r.id, r.actor_name, r.actor_role, r.action, r.entity_type, r.entity_id,
                          r.created_at.isoformat() if r.created_at else ""])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_logs.csv"},
    )


# ---------------------------------------------------------------------------
# Error Queue
# ---------------------------------------------------------------------------

@router.get("/errors", response_model=PaginatedErrorsDTO)
async def list_errors(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    status: Optional[str] = None,
    error_stage: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    filters = []
    if status:
        filters.append(ProcessingError.status == status)
    if error_stage:
        filters.append(ProcessingError.error_stage == error_stage)
    base_filter = and_(*filters) if filters else True

    total = (await db.execute(select(func.count()).select_from(ProcessingError).where(base_filter))).scalar_one()
    rows = (await db.execute(
        select(ProcessingError).where(base_filter).order_by(ProcessingError.occurred_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    data = [
        ProcessingErrorRowDTO(
            id=str(r.id), source_type=r.source_type, source_id=r.source_id,
            error_stage=r.error_stage, error_message=r.error_message, status=r.status,
            retry_count=r.retry_count or 0,
            occurred_at=r.occurred_at.isoformat() if r.occurred_at else "",
            resolved_at=r.resolved_at.isoformat() if r.resolved_at else None,
        )
        for r in rows
    ]
    return PaginatedErrorsDTO(data=data, total=total, page=page, page_size=page_size,
                               total_pages=math.ceil(total / page_size) or 1)


@router.post("/errors/{error_id}/retry")
async def retry_error(
    error_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    result = await db.execute(select(ProcessingError).where(ProcessingError.id == error_id))
    error = result.scalars().first()
    if not error:
        raise HTTPException(status_code=404, detail="Error not found")

    outcome = await attempt_retry(db, error)
    await log_action(db, "ERROR_RETRY",
        actor_user_id=current_user.get("sub"), actor_name=current_user.get("sub", ""),
        actor_role=current_user.get("role", ""), entity_type="ProcessingError", entity_id=str(error_id),
        metadata=outcome)
    await db.commit()
    return outcome


@router.post("/errors/{error_id}/close")
async def close_error(
    error_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    result = await db.execute(select(ProcessingError).where(ProcessingError.id == error_id))
    error = result.scalars().first()
    if not error:
        raise HTTPException(status_code=404, detail="Error not found")
    error.status = "CLOSED"
    error.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    return {"success": True, "message": "Error closed."}


# ---------------------------------------------------------------------------
# Manual Review Queue
# ---------------------------------------------------------------------------

@router.get("/review-queue", response_model=PaginatedReviewDTO)
async def list_review_queue(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    filters = [ManualReviewQueue.status == status] if status else []
    base_filter = and_(*filters) if filters else True

    total = (await db.execute(select(func.count()).select_from(ManualReviewQueue).where(base_filter))).scalar_one()
    rows = (await db.execute(
        select(ManualReviewQueue).where(base_filter).order_by(ManualReviewQueue.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    data = [
        ReviewQueueRowDTO(
            id=str(r.id), error_id=str(r.error_id), status=r.status,
            review_notes=r.review_notes,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]
    return PaginatedReviewDTO(data=data, total=total, page=page, page_size=page_size,
                               total_pages=math.ceil(total / page_size) or 1)


@router.post("/review-queue/{review_id}/approve")
async def approve_review(
    review_id: int,
    body: ReviewActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    result = await db.execute(select(ManualReviewQueue).where(ManualReviewQueue.id == review_id))
    review = result.scalars().first()
    if not review:
        raise HTTPException(status_code=404, detail="Review item not found")
    review.status = "APPROVED"
    review.review_notes = body.notes
    review.correction_data = body.correction_data
    await log_action(db, "REVIEW_APPROVED",
        actor_user_id=current_user.get("sub"), actor_name=current_user.get("sub", ""),
        actor_role=current_user.get("role", ""), entity_type="ManualReviewQueue", entity_id=str(review_id))
    await db.commit()
    return {"success": True, "message": "Review approved."}


@router.post("/review-queue/{review_id}/reject")
async def reject_review(
    review_id: int,
    body: ReviewActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    result = await db.execute(select(ManualReviewQueue).where(ManualReviewQueue.id == review_id))
    review = result.scalars().first()
    if not review:
        raise HTTPException(status_code=404, detail="Review item not found")
    review.status = "REJECTED"
    review.review_notes = body.notes
    await log_action(db, "REVIEW_REJECTED",
        actor_user_id=current_user.get("sub"), actor_name=current_user.get("sub", ""),
        actor_role=current_user.get("role", ""), entity_type="ManualReviewQueue", entity_id=str(review_id))
    await db.commit()
    return {"success": True, "message": "Review rejected."}


# ---------------------------------------------------------------------------
# AI Usage / Cost Tracking
# ---------------------------------------------------------------------------

@router.get("/ai-usage/stats", response_model=AIUsageStatsDTO)
async def ai_usage_stats(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    total_cost = (await db.execute(select(func.coalesce(func.sum(AIUsageLog.estimated_cost), 0)))).scalar_one()
    total_calls = (await db.execute(select(func.count()).select_from(AIUsageLog))).scalar_one()
    budget = await get_budget_threshold(db)
    used_pct = (float(total_cost) / budget * 100) if budget > 0 else 0.0
    return AIUsageStatsDTO(
        total_cost_usd=round(float(total_cost), 4),
        total_calls=total_calls,
        budget_usd=budget,
        budget_used_pct=round(used_pct, 2),
    )


@router.put("/ai-usage/budget", response_model=AIUsageStatsDTO)
async def update_ai_budget(
    body: SetBudgetRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    updated_by = current_user.get("sub")
    await set_budget_threshold(db, body.budget_usd, updated_by=updated_by)

    total_cost = (await db.execute(select(func.coalesce(func.sum(AIUsageLog.estimated_cost), 0)))).scalar_one()
    total_calls = (await db.execute(select(func.count()).select_from(AIUsageLog))).scalar_one()
    used_pct = (float(total_cost) / body.budget_usd * 100) if body.budget_usd > 0 else 0.0

    return AIUsageStatsDTO(
        total_cost_usd=round(float(total_cost), 4),
        total_calls=total_calls,
        budget_usd=body.budget_usd,
        budget_used_pct=round(used_pct, 2),
    )

@router.get("/ai-usage/claude", response_model=ClaudeUsageDTO)
async def get_claude_usage(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin)
):
    """Get the latest recorded Claude API rate limit information."""
        
    limits = await get_claude_rate_limits(db)
    limit = limits["tokens_limit"]
    remaining = limits["tokens_remaining"]
    
    used_pct = 0.0
    if limit > 0:
        used_pct = ((limit - remaining) / limit) * 100.0
        
    return ClaudeUsageDTO(
        tokens_limit=limit,
        tokens_remaining=remaining,
        tokens_used_pct=round(used_pct, 2),
        tokens_reset=limits["tokens_reset"]
    )


@router.get("/ai-usage/daily")
async def ai_usage_daily(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(AIUsageLog).where(AIUsageLog.created_at >= since).order_by(AIUsageLog.created_at.asc())
    )).scalars().all()

    daily: dict = {}
    for r in rows:
        day = r.created_at.date().isoformat() if r.created_at else "unknown"
        daily.setdefault(day, 0.0)
        daily[day] += float(r.estimated_cost or 0)

    return [{"date": d, "cost_usd": round(c, 4)} for d, c in sorted(daily.items())]


# ---------------------------------------------------------------------------
# Dashboard Stats & System Health
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=AdminStatsDTO)
async def admin_stats(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    total_audit = (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    open_errors = (await db.execute(
        select(func.count()).select_from(ProcessingError).where(ProcessingError.status == "OPEN")
    )).scalar_one()
    pending_reviews = (await db.execute(
        select(func.count()).select_from(ManualReviewQueue).where(ManualReviewQueue.status == "OPEN")
    )).scalar_one()
    total_ai_cost = (await db.execute(select(func.coalesce(func.sum(AIUsageLog.estimated_cost), 0)))).scalar_one()

    return AdminStatsDTO(
        total_audit_events=total_audit,
        open_errors=open_errors,
        pending_reviews=pending_reviews,
        total_ai_cost_usd=round(float(total_ai_cost), 4),
    )


@router.get("/classifier-health", response_model=ClassifierHealthDTO)
async def classifier_health(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    unclassified_count = (await db.execute(
        text("SELECT COUNT(*) FROM gmail_emails WHERE category IS NULL OR category = 'unclassified'")
    )).scalar_one()

    processed_last_5_min = (await db.execute(
        text("SELECT COUNT(*) FROM gmail_emails WHERE classified_at > now() - interval '5 minutes'")
    )).scalar_one()

    last_classified_at = (await db.execute(
        text("SELECT MAX(classified_at) FROM gmail_emails WHERE classified_at IS NOT NULL")
    )).scalar_one()

    minutes_since_last_cycle = None
    if last_classified_at:
        if last_classified_at.tzinfo is None:
            last_classified_at = last_classified_at.replace(tzinfo=timezone.utc)
        minutes_since_last_cycle = (datetime.now(timezone.utc) - last_classified_at).total_seconds() / 60

    if minutes_since_last_cycle is not None and minutes_since_last_cycle > 5:
        status = "no_recent_activity"
    elif processed_last_5_min == 0 and unclassified_count > 100:
        status = "stuck"
    else:
        status = "healthy"

    return ClassifierHealthDTO(
        unclassified_count=unclassified_count,
        processed_last_5_min=processed_last_5_min,
        last_classified_at=last_classified_at,
        minutes_since_last_cycle=minutes_since_last_cycle,
        status=status,
    )


@router.get("/system-health", response_model=SystemHealthDTO)
async def system_health(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    indicators = []

    try:
        await db.execute(select(1))
        indicators.append(HealthIndicator(name="database", status="ok"))
    except Exception as e:
        indicators.append(HealthIndicator(name="database", status="error", detail=str(e)))

    redis_ok = await check_redis_health()
    indicators.append(HealthIndicator(name="redis", status="ok" if redis_ok else "unavailable"))

    overall = "ok" if all(i.status == "ok" for i in indicators if i.name == "database") else "degraded"
    return SystemHealthDTO(status=overall, indicators=indicators)


# ---------------------------------------------------------------------------
# Duplicates view (reads PARSE audit events with is_duplicate flag)
# ---------------------------------------------------------------------------

@router.get("/duplicates", response_model=PaginatedDuplicatesDTO)
async def list_duplicates(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), _: dict = Depends(require_admin),
):
    result = await db.execute(
        select(AuditLog).where(AuditLog.action == "PARSE").order_by(AuditLog.created_at.desc())
    )
    dup_rows = [r for r in result.scalars().all() if (r.meta or {}).get("is_duplicate")]

    groups: dict = {}
    for r in dup_rows:
        meta = r.meta or {}
        key = meta.get("dedup_key", r.entity_id or "unknown")
        if key not in groups:
            groups[key] = {"dedup_key": key, "role": meta.get("role", "UNKNOWN"),
                           "vendor_email": meta.get("vendor_email"),
                           "raw_email_ids": [], "first_seen": r.created_at, "last_seen": r.created_at}
        groups[key]["raw_email_ids"].append(r.entity_id or "")
        if r.created_at < groups[key]["first_seen"]: groups[key]["first_seen"] = r.created_at
        if r.created_at > groups[key]["last_seen"]:  groups[key]["last_seen"] = r.created_at

    group_list = sorted(groups.values(), key=lambda g: g["last_seen"], reverse=True)
    total = len(group_list)
    page_data = group_list[(page - 1) * page_size: page * page_size]

    return PaginatedDuplicatesDTO(
        data=[DuplicateGroupDTO(
            dedup_key=g["dedup_key"], role=g["role"], vendor_email=g["vendor_email"],
            count=len(g["raw_email_ids"]),
            first_seen=g["first_seen"].isoformat() if hasattr(g["first_seen"], "isoformat") else str(g["first_seen"]),
            last_seen=g["last_seen"].isoformat() if hasattr(g["last_seen"], "isoformat") else str(g["last_seen"]),
            raw_email_ids=g["raw_email_ids"][:10],
        ) for g in page_data],
        total=total, page=page, page_size=page_size, total_pages=math.ceil(total / page_size) or 1,
    )


# ---------------------------------------------------------------------------
# Applications Sent view (reads SEND audit events)
# ---------------------------------------------------------------------------

@router.get("/applications-feed", response_model=PaginatedApplicationsDTO)
async def list_applications_feed(
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    sort_dir: str = Query("desc"), consultant_id: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_db), _: dict = Depends(require_admin),
):
    filters = [AuditLog.action.in_(["SEND", "APPLICATION_SENT"])]
    if date_from: filters.append(AuditLog.created_at >= datetime.fromisoformat(date_from))
    if date_to: filters.append(AuditLog.created_at <= datetime.fromisoformat(date_to + "T23:59:59+00:00"))
    # BUG FIX: consultant_id was being filtered in Python AFTER the DB
    # already paginated (.offset()/.limit()) on the unfiltered set — total
    # counted every send regardless of consultant, and any given page could
    # come back empty even when the filtered consultant had matching rows
    # elsewhere in the full result set. Push the filter into the JSONB
    # query itself so counting and pagination both reflect the actual
    # filtered set.
    if consultant_id:
        filters.append(AuditLog.meta["consultant_id"].astext == consultant_id)

    total = (await db.execute(select(func.count()).select_from(AuditLog).where(and_(*filters)))).scalar_one()
    order = AuditLog.created_at.desc() if sort_dir == "desc" else AuditLog.created_at.asc()
    rows = (await db.execute(
        select(AuditLog).where(and_(*filters)).order_by(order)
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    data = []
    for r in rows:
        meta = r.meta or {}
        data.append(ApplicationSentRowDTO(
            id=str(r.id), timestamp=r.created_at.isoformat() if r.created_at else "",
            consultant_name=meta.get("consultant_name", r.actor_name or ""),
            consultant_id=meta.get("consultant_id", r.actor_user_id or ""),
            requirement_id=meta.get("requirement_id", r.entity_id or ""),
            role=meta.get("role", "UNKNOWN"),
            vendor_email=meta.get("recipient", meta.get("vendor_email", "")),
            ats_score=meta.get("ats_score"), resume_id=meta.get("resume_id"),
            status=meta.get("delivery_status", "SENT"),
        ))
    return PaginatedApplicationsDTO(data=data, total=total, page=page,
        page_size=page_size, total_pages=math.ceil(total / page_size) or 1)