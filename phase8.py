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
from zoneinfo import ZoneInfo
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, text
from sqlalchemy.future import select as fselect

from database import get_db
from auth import decode_access_token, get_current_user
from models import (
    Application,
    AuditLog,
    ProcessingError,
    ManualReviewQueue,
    AIUsageLog,
    AppSetting,
)
from phase8_audit_service import log_action, build_metadata_preview
from phase8_ai_usage_service import get_budget_threshold, set_budget_threshold, estimate_cost
from phase8_cache import check_redis_health

router = APIRouter(prefix="/api/v1/admin", tags=["Phase 8 - Admin Monitoring"])

# Admin team's local calendar day for date-only filters below — was IST,
# now CST (US Central). Real IANA zone (not a fixed offset) so CST/CDT
# daylight-saving transitions are handled correctly.
_CST_ZONE = ZoneInfo("America/Chicago")

# The Error Queue groups messages in SQL with split_part, which Postgres has
# and the SQLite dev fallback does not — see errors_grouped.
try:
    from models import _is_postgres as _IS_POSTGRES
except Exception:  # pragma: no cover - defensive, models always defines it
    _IS_POSTGRES = False


# ---------------------------------------------------------------------------
# Auth dependency — built on your existing auth.py, Bearer-token style
# (separate from your cookie-based get_current_user, since Swagger/admin
# tools typically send "Authorization: Bearer <token>")
# ---------------------------------------------------------------------------

from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


async def require_admin(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Accepts only a Bearer token from the Authorization header.

    SECURITY FIX: this used to trust the "role" claim inside the JWT alone,
    so a demoted, deactivated or even deleted admin kept full admin access
    until their token expired (24h) — and could create new admins with it.
    The role is now re-read from the database on every request through
    get_current_user (the same check every other router uses), which also
    rejects deactivated users and inactive consultants.

    Still returns {"sub": email, "role": ...} so every endpoint that reads
    current_user.get("sub") / .get("role") keeps working unchanged.
    """
    # get_current_user reads the Authorization header itself and raises 401
    # ("Not authenticated" / "Invalid token" / "Session expired" /
    # "User not found or inactive") exactly like the rest of the API.
    user = await get_current_user(request, db)
    if user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    return {"sub": user.email, "role": user.role}


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
    occurred_at: str
    resolved_at: Optional[str] = None
    # Exposed so the Error Queue can show state, the real stack trace and how
    # many times a row has already been retried. All are existing columns on
    # processing_errors that this endpoint simply never returned.
    status: Optional[str] = None
    stack_trace: Optional[str] = None
    retry_count: int = 0
    category: Optional[str] = None


class ErrorGroupDTO(BaseModel):
    """One cause, with every occurrence of it folded into a single row."""
    signature: str
    error_stage: str
    category: str
    count: int
    last_occurred: str
    sample_message: str
    sample_id: str
    source_type: Optional[str] = None


class ErrorGroupsDTO(BaseModel):
    data: List[ErrorGroupDTO]
    total_groups: int
    total_errors: int
    # True when the grouping was computed over a capped sample instead of the
    # whole table (the SQLite dev path, which has no split_part).
    approximate: bool = False


class ErrorSummaryDTO(BaseModel):
    total: int
    by_category: dict


class ErrorActionResultDTO(BaseModel):
    success: bool
    affected: int = 0
    requeued: int = 0
    message: str = ""


class ErrorBulkBody(BaseModel):
    action: str                       # "retry" | "dismiss"
    ids: Optional[List[str]] = None
    signature: Optional[str] = None   # with error_stage, acts on a whole group
    error_stage: Optional[str] = None
    date_filter: Optional[str] = None


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


class OpenAIUsageDTO(BaseModel):
    tokens_limit: str
    tokens_remaining: str
    tokens_used_pct: float
    tokens_reset: str

class AIUsageStatsDTO(BaseModel):
    total_cost_usd: float
    total_calls: int
    budget_usd: float
    budget_used_pct: float
    # AI-USAGE FIX: optional extras (defaults keep older clients working)
    total_tokens: int = 0
    resume_generation_tokens: int = 0
    resume_generation_cost_usd: float = 0.0
    resume_generation_count: int = 0
    period: str = "month_to_date"


class SetBudgetRequest(BaseModel):
    budget_usd: float = Field(..., gt=0, description="AI spend budget in USD, must be greater than 0")


class EmailIntakeDayDTO(BaseModel):
    day: str
    Parsed: int = 0
    Parsed_NR: int = Field(0, alias="Parsed - NR")
    Parsed_Dup: int = Field(0, alias="Parsed - Dup")
    Failed: int = 0
    Pending: int = 0

    class Config:
        populate_by_name = True

class ErrorRateDTO(BaseModel):
    error_rate_pct: float
    trend: str
    prev_pct: float

class AIUsageLogRowDTO(BaseModel):
    id: str
    timestamp: str
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    consultant_id: Optional[str] = None
    consultant_name: Optional[str] = None
    requirement_id: Optional[str] = None

class PaginatedAIUsageDTO(BaseModel):
    data: list[AIUsageLogRowDTO]
    total: int
    page: int
    page_size: int
    total_pages: int

class CostPerConsultantDTO(BaseModel):
    consultant_id: str
    consultant_name: str
    total_cost_usd: float
    total_resumes_generated: int
    avg_cost_per_resume_usd: float

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
    # BUG FIX: client and gmail_message_id were UI table columns with no
    # backing field at all — always blank regardless of real data.
    # sent_by_name/role were completely missing too (frontend hardcoded
    # sentBy: ""). All populated below from real columns/joins.
    client: Optional[str] = None
    gmail_message_id: Optional[str] = None
    sent_by_name: Optional[str] = None
    sent_by_role: Optional[str] = None
    # Real file name of the attached resume (Resume column pill).
    resume_filename: Optional[str] = None
    # BUG FIX: "Applications Sent (7d)" undercounted real sends — the
    # frontend's date filter (admin.api.ts) already checked `sent_at`
    # first, before falling back to `timestamp` (created_at) or
    # `applied_at`, but this endpoint never returned sent_at at all. That
    # matters because created_at is set once at row creation and never
    # changes, and applied_at is preserved as-is on updates
    # (`existing_app.applied_at or now`) — neither refreshes when an
    # existing consultant+requirement Application row gets re-sent. Only
    # Application.sent_at is genuinely updated to "now" on every real
    # send, so it's the only field that correctly reflects recent activity
    # when re-applying to an existing match (which is the common case with
    # a large pre-existing match backlog).
    sent_at: Optional[str] = None
    # BUG FIX: resume_id alone (generated_resume_id) missed every
    # application sent via the email-queue/Apply flow, which never links
    # a GeneratedResume — only a raw attachment path. resume_available
    # reflects either source so the Resume column shows correctly for all
    # of them, not just the ATS-gated recruiter confirm-send flow.
    resume_available: bool = False
    # Shown in the tracker row and its email popup.
    consultant_email: Optional[str] = None
    error_message: Optional[str] = None


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
    # Multi-select: comma-separated, e.g. action=USER_UPDATED,USER_CREATED
    action_list = [a.strip() for a in (action or "").split(",") if a.strip()]
    entity_list = [e.strip() for e in (entity_type or "").split(",") if e.strip()]
    if action_list:
        filters.append(AuditLog.action.in_(action_list))
    if entity_list:
        filters.append(AuditLog.entity_type.in_(entity_list))
    if date_from:
        # BUG FIX: date_to below appends "T23:59:59+00:00" so the compared
        # value is tz-aware, matching AuditLog.created_at's tz-aware
        # TIMESTAMP column — date_from was parsed bare, giving a naive
        # datetime compared against a tz-aware column. Match date_to's
        # handling so both ends of the range are interpreted the same way.
        filters.append(AuditLog.created_at >= datetime.fromisoformat(date_from + "T00:00:00+00:00"))
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

from models import ApplicationsDetailView

@router.get("/applications", response_model=PaginatedApplicationsDTO)
async def list_applications(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    consultant_id: Optional[str] = None,
    requirement_id: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    sort_dir: str = Query("desc"),
    created_from: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
) -> PaginatedApplicationsDTO:
    """Return paginated applications with optional filters."""
    filters = []
    # DASHBOARD FIX: optional date filter so the dashboard can get a real
    # count. "YYYY-MM-DD" = start of that day in US Central time.
    if created_from:
        try:
            _cf = created_from.strip()
            if len(_cf) == 10:
                _cf_dt = datetime.strptime(_cf, "%Y-%m-%d").replace(tzinfo=ZoneInfo("America/Chicago"))
            else:
                _cf_dt = datetime.fromisoformat(_cf.replace("Z", "+00:00"))
            filters.append(ApplicationsDetailView.created_at >= _cf_dt)
        except ValueError:
            pass
    if consultant_id:
        try:
            ids = [int(x.strip()) for x in consultant_id.split(",") if x.strip()]
            if ids:
                filters.append(ApplicationsDetailView.consultant_id.in_(ids))
        except ValueError:
            pass
    if requirement_id:
        try:
            filters.append(ApplicationsDetailView.requirement_id == int(requirement_id))
        except ValueError:
            pass
    if status:
        filters.append(ApplicationsDetailView.status == status)
    if search and search.strip():
        term = f"%{search.strip()}%"
        filters.append(
            or_(
                ApplicationsDetailView.consultant_name.ilike(term),
                ApplicationsDetailView.requirement_role.ilike(term),
                ApplicationsDetailView.requirement_vendor_email.ilike(term),
                ApplicationsDetailView.requirement_client.ilike(term),
            )
        )
    base_filter = and_(*filters) if filters else True

    count_q = select(func.count(ApplicationsDetailView.application_id)).select_from(ApplicationsDetailView).where(base_filter)
    total = (await db.execute(count_q)).scalar_one()

    q = select(ApplicationsDetailView).where(base_filter)
    order = ApplicationsDetailView.created_at.desc() if sort_dir == "desc" else ApplicationsDetailView.created_at.asc()

    results = (await db.execute(
        q.order_by(order)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all()

    # consultant email + failure reason come straight from their own tables
    # (one extra query for this page) so the v_applications_detail view
    # doesn't need a migration.
    _ids = [r.application_id for r in results]
    _extra = {}
    if _ids:
        from models import Application as _App, Consultant as _Cons
        _rows = (await db.execute(
            select(_App.id, _App.error_message, _Cons.email)
            .join(_Cons, _Cons.id == _App.consultant_id)
            .where(_App.id.in_(_ids))
        )).all()
        _extra = {r[0]: (r[1], r[2]) for r in _rows}

    _resume_ids = [r.generated_resume_id for r in results if r.generated_resume_id]
    _resume_names = {}
    if _resume_ids:
        from models import GeneratedResume as _GR
        _resume_names = {
            r[0]: r[1]
            for r in (await db.execute(
                select(_GR.id, _GR.filename).where(_GR.id.in_(_resume_ids))
            )).all()
        }

    def _resume_name(row):
        name = _resume_names.get(row.generated_resume_id) if row.generated_resume_id else None
        if name:
            return name
        if row.resume_attachment_path:
            from email_queue import original_filename_from_ref
            return original_filename_from_ref(row.resume_attachment_path)
        return None

    data = [
        ApplicationSentRowDTO(
            resume_filename=_resume_name(row),
            consultant_email=(_extra.get(row.application_id) or (None, None))[1],
            error_message=((_extra.get(row.application_id) or (None, None))[0] if row.status == "FAILED" else None),
            id=str(row.application_id),
            timestamp=row.created_at.isoformat() if row.created_at else "",
            consultant_name=row.consultant_name if row.consultant_name else str(row.consultant_id),
            consultant_id=str(row.consultant_id),
            requirement_id=str(row.requirement_id),
            role=row.requirement_role if row.requirement_role else "UNKNOWN",
            vendor_email=row.vendor_email or "",
            ats_score=float(row.ats_score_at_send) if row.ats_score_at_send else None,
            resume_id=str(row.generated_resume_id) if row.generated_resume_id else None,
            status=row.status or "QUEUED",
            candidate_id=None,
            job_posting_id=None,
            match_score=None,
            applied_at=row.applied_at.isoformat() if row.applied_at else None,
            client=row.requirement_client,
            gmail_message_id=row.gmail_message_id,
            sent_by_name=row.recruiter_name or row.consultant_name,
            sent_by_role="RECRUITER" if row.recruiter_id else ("CONSULTANT" if row.consultant_id else None),
            sent_at=row.sent_at.isoformat() if row.sent_at else None,
            # BUG FIX: this only ever checked generated_resume_id, despite
            # this DTO field's own docstring above saying it should
            # reflect either source. generated_resume_id is only set by
            # the ATS-gated recruiter confirm-send flow — every
            # application sent through the ordinary Apply / email-queue
            # flow (the vast majority) attaches its resume via
            # resume_attachment_path instead and NEVER gets a
            # generated_resume_id, so the Resume column showed "—" for
            # nearly every row in the tracker regardless of whether a
            # resume actually went out. Requires resume_attachment_path
            # to be added to the v_applications_detail view — see the
            # MIGRATION REQUIRED note on that column in models.py.
            resume_available=bool(row.generated_resume_id or row.resume_attachment_path),
        )
        for row in results
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


def _csv_cst(value) -> str:
    """UTC timestamp -> 'YYYY-MM-DD HH:MM:SS CDT/CST' in US Central."""
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_CST_ZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


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
        # CST fix: was r.created_at.isoformat() (UTC) -- the CSV now shows
        # the same US Central time the Audit Logs screen shows.
        writer.writerow([r.id, r.actor_name, r.actor_role, r.action, r.entity_type, r.entity_id,
                          _csv_cst(r.created_at)])
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
    date_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query("OPEN", description="OPEN | CLOSED | ALL"),
    category: Optional[str] = None,
    error_stage: Optional[str] = None,
    signature: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    # The category chips (Email, Database, ...) map to real stage names via
    # this cache. It used to be filled only by /errors/groups, so on a fresh
    # server the chips filtered nothing. Always refresh it first.
    await _refresh_stage_cache(db)
    filters = []
    # Only unresolved errors by default. The queue used to return dismissed
    # and resolved rows too, which is most of why the count ran to tens of
    # thousands; "ALL" keeps the old behaviour for anyone who wants it.
    if status and status.upper() != "ALL":
        filters.append(ProcessingError.status == status.upper())
    if error_stage:
        filters.append(ProcessingError.error_stage == error_stage)
    if category:
        stages = _stages_for_category(category)
        if stages is not None:
            filters.append(ProcessingError.error_stage.in_(stages))
    if signature:
        filters.append(ProcessingError.error_message.like(signature.rstrip() + "%"))
    if search and search.strip():
        term = f"%{search.strip()}%"
        filters.append(or_(
            ProcessingError.error_message.ilike(term),
            ProcessingError.error_stage.ilike(term),
            ProcessingError.source_id.ilike(term),
        ))
    # Date range (US Central calendar days, both ends included).
    filters.extend(_range_filters(date_from, date_to))
    if date_filter:
        try:
            d = date.fromisoformat(date_filter)
            # BUG FIX ("selecting the 11th shows the 12th's data"): this
            # used to treat date_filter as a UTC calendar day, but the
            # table renders occurred_at in the browser's local time
            # (ErrorTable.tsx: new Date(...).toLocaleString() with no
            # explicit UTC), and this app's admins are CST-based (US
            # Central time). Interpret the selected date as a CST/CDT
            # calendar day instead, matching what's actually shown in
            # the table. Uses a real zoneinfo timezone rather than a
            # fixed offset so the CST (UTC-6) / CDT (UTC-5) daylight-
            # saving switch is handled automatically instead of being
            # off by an hour for roughly half the year.
            start_local = datetime.combine(d, datetime.min.time(), tzinfo=_CST_ZONE)
            start_dt = start_local.astimezone(timezone.utc)
            end_dt = (start_local + timedelta(days=1)).astimezone(timezone.utc)
            filters.append(ProcessingError.occurred_at >= start_dt)
            filters.append(ProcessingError.occurred_at < end_dt)
        except ValueError:
            pass
            
    base_filter = and_(*filters) if filters else True

    total = (await db.execute(select(func.count()).select_from(ProcessingError).where(base_filter))).scalar_one()
    rows = (await db.execute(
        select(ProcessingError).where(base_filter).order_by(ProcessingError.occurred_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    data = [
        ProcessingErrorRowDTO(
            id=str(r.id), source_type=r.source_type, source_id=r.source_id,
            error_stage=r.error_stage, error_message=r.error_message,
            occurred_at=r.occurred_at.isoformat() if r.occurred_at else "",
            resolved_at=r.resolved_at.isoformat() if r.resolved_at else None,
            status=r.status, stack_trace=r.stack_trace, retry_count=r.retry_count or 0,
            category=_category_for_stage(r.error_stage),
        )
        for r in rows
    ]
    return PaginatedErrorsDTO(data=data, total=total, page=page, page_size=page_size,
                               total_pages=math.ceil(total / page_size) or 1)





# ---------------------------------------------------------------------------
# Error Queue: grouping, summary and actions
# ---------------------------------------------------------------------------

# Every error_stage the pipeline writes, bucketed into the handful of
# categories the Error Queue screen filters by.
_CATEGORY_RULES = [
    ("Email", ("email", "send", "gmail")),
    ("Parsing", ("parse", "parser", "extract", "ingest")),
    ("Matching", ("match",)),
    ("Resume", ("resume", "generate")),
    ("Database", ("db", "database", "migration", "sql")),
]


def _category_for_stage(stage: Optional[str]) -> str:
    st = (stage or "").lower()
    for name, needles in _CATEGORY_RULES:
        if any(n in st for n in needles):
            return name
    return "Other"


def _stages_for_category(category: str):
    """None = do not filter (unknown category)."""
    wanted = (category or "").strip().lower()
    if not wanted or wanted == "all":
        return None
    for name, _ in _CATEGORY_RULES:
        if name.lower() == wanted:
            return None if name == "Other" else _STAGE_CACHE.get(name)
    return None


# Filled lazily per request from the stages actually present in the table, so
# a new stage name starts being categorised without a code change.
_STAGE_CACHE: dict = {}


async def _refresh_stage_cache(db: AsyncSession) -> None:
    rows = (await db.execute(select(ProcessingError.error_stage).distinct())).scalars().all()
    buckets: dict = {}
    for st in rows:
        buckets.setdefault(_category_for_stage(st), []).append(st)
    _STAGE_CACHE.clear()
    _STAGE_CACHE.update(buckets)


def _range_filters(date_from: Optional[str], date_to: Optional[str]):
    """occurred_at between two US Central calendar days, both included."""
    out = []
    try:
        if date_from:
            d0 = date.fromisoformat(date_from)
            out.append(ProcessingError.occurred_at >= datetime.combine(d0, datetime.min.time(), tzinfo=_CST_ZONE).astimezone(timezone.utc))
        if date_to:
            d1 = date.fromisoformat(date_to)
            end_local = datetime.combine(d1, datetime.min.time(), tzinfo=_CST_ZONE) + timedelta(days=1)
            out.append(ProcessingError.occurred_at < end_local.astimezone(timezone.utc))
    except ValueError:
        pass
    return out


def _date_filters(date_filter: Optional[str]):
    out = []
    if date_filter:
        try:
            d = date.fromisoformat(date_filter)
            start_local = datetime.combine(d, datetime.min.time(), tzinfo=_CST_ZONE)
            out.append(ProcessingError.occurred_at >= start_local.astimezone(timezone.utc))
            out.append(ProcessingError.occurred_at < (start_local + timedelta(days=1)).astimezone(timezone.utc))
        except ValueError:
            pass
    return out


def _signature_of(message: str) -> str:
    """The part of a message before its first bracket, which is where the
    per-row detail (an address, an id) lives. 'Email not enabled for this
    user (a@b.com)' and the next 41,229 like it collapse to one signature."""
    return (message or "").split("(")[0].strip()


@router.get("/errors/summary", response_model=ErrorSummaryDTO)
async def errors_summary(
    date_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query("OPEN"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
) -> ErrorSummaryDTO:
    """Counts per category, for the chips above the list."""
    filters = _date_filters(date_filter) + _range_filters(date_from, date_to)
    if status and status.upper() != "ALL":
        filters.append(ProcessingError.status == status.upper())
    base = and_(*filters) if filters else True

    rows = (await db.execute(
        select(ProcessingError.error_stage, func.count().label("n"))
        .where(base).group_by(ProcessingError.error_stage)
    )).all()

    by_cat: dict = {}
    total = 0
    for stage, n in rows:
        by_cat[_category_for_stage(stage)] = by_cat.get(_category_for_stage(stage), 0) + int(n)
        total += int(n)
    return ErrorSummaryDTO(total=total, by_category=by_cat)


@router.get("/errors/groups", response_model=ErrorGroupsDTO)
async def errors_grouped(
    date_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query("OPEN"),
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
) -> ErrorGroupsDTO:
    """
    The Error Queue's default view: one row per cause instead of one row per
    occurrence. Tens of thousands of identical "Email not enabled for this
    user (...)" rows are a single group with a count, so the screen shows
    what is actually wrong rather than a wall of repeats.

    The grouping runs in SQL on Postgres (split_part on the first bracket).
    On the SQLite dev path there is no split_part, so it groups in Python
    over the most recent 5,000 rows and reports approximate = true.
    """
    await _refresh_stage_cache(db)

    filters = _date_filters(date_filter) + _range_filters(date_from, date_to)
    if status and status.upper() != "ALL":
        filters.append(ProcessingError.status == status.upper())
    if category and category.lower() != "all":
        stages = _stages_for_category(category)
        if stages is not None:
            filters.append(ProcessingError.error_stage.in_(stages))
    if search and search.strip():
        term = f"%{search.strip()}%"
        filters.append(or_(
            ProcessingError.error_message.ilike(term),
            ProcessingError.error_stage.ilike(term),
        ))
    base = and_(*filters) if filters else True

    total_errors = (await db.execute(
        select(func.count()).select_from(ProcessingError).where(base)
    )).scalar_one()

    groups = []
    approximate = False

    if _IS_POSTGRES:
        sig = func.btrim(func.split_part(ProcessingError.error_message, "(", 1))
        rows = (await db.execute(
            select(
                sig.label("sig"),
                ProcessingError.error_stage,
                func.count().label("n"),
                func.max(ProcessingError.occurred_at).label("last_at"),
                func.min(ProcessingError.error_message).label("sample"),
                func.min(ProcessingError.id).label("sample_id"),
                func.min(ProcessingError.source_type).label("source_type"),
            )
            .where(base)
            .group_by(sig, ProcessingError.error_stage)
            .order_by(func.count().desc())
            .limit(limit)
        )).all()
        for sg, stage, n, last_at, sample, sample_id, src in rows:
            groups.append(ErrorGroupDTO(
                signature=sg or (sample or "")[:120], error_stage=stage,
                category=_category_for_stage(stage), count=int(n),
                last_occurred=last_at.isoformat() if last_at else "",
                sample_message=sample or "", sample_id=str(sample_id), source_type=src,
            ))
    else:
        approximate = True
        recent = (await db.execute(
            select(ProcessingError).where(base)
            .order_by(ProcessingError.occurred_at.desc()).limit(5000)
        )).scalars().all()
        acc: dict = {}
        for r in recent:
            key = (_signature_of(r.error_message), r.error_stage)
            cur = acc.get(key)
            if cur is None:
                acc[key] = {"n": 1, "last": r.occurred_at, "sample": r.error_message,
                            "id": r.id, "src": r.source_type}
            else:
                cur["n"] += 1
                if r.occurred_at and (cur["last"] is None or r.occurred_at > cur["last"]):
                    cur["last"] = r.occurred_at
        for (sg, stage), v in sorted(acc.items(), key=lambda kv: -kv[1]["n"])[:limit]:
            groups.append(ErrorGroupDTO(
                signature=sg, error_stage=stage, category=_category_for_stage(stage),
                count=v["n"], last_occurred=v["last"].isoformat() if v["last"] else "",
                sample_message=v["sample"], sample_id=str(v["id"]), source_type=v["src"],
            ))

    return ErrorGroupsDTO(data=groups, total_groups=len(groups),
                          total_errors=int(total_errors), approximate=approximate)


async def _requeue_email_for(db: AsyncSession, err) -> bool:
    """A failed send is the one stage that can genuinely be run again: put its
    email back in the queue and let the existing worker send it. Returns True
    only when a queue item was actually re-queued."""
    if _category_for_stage(err.error_stage) != "Email":
        return False
    from models import EmailQueue
    ident = err.source_id
    if not ident or not str(ident).isdigit():
        return False
    item = (await db.execute(
        select(EmailQueue).where(EmailQueue.id == int(ident))
    )).scalars().first()
    if not item or item.status != "FAILED":
        return False
    item.status = "QUEUED"
    item.status_text = None
    item.scheduled_at = datetime.now(timezone.utc)
    return True


@router.get("/errors/{error_id}")
async def get_error_detail(
    error_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    """One error with its Entity Context filled in. The error rows rarely carry
    raw_email_id / requirement_id / consultant_id themselves, so they are
    followed from the thing that failed: an email-queue item leads to its
    requirement, consultant and the raw email the requirement came from."""
    err = (await db.execute(
        select(ProcessingError).where(ProcessingError.id == error_id)
    )).scalars().first()
    if not err:
        raise HTTPException(status_code=404, detail="Error not found")

    raw_email_id = err.raw_email_id
    requirement_id = err.requirement_id
    consultant_id = err.consultant_id
    extra: dict = {}
    if isinstance(err.additional_context, dict):
        extra.update({str(k): str(v) for k, v in err.additional_context.items() if v is not None})

    try:
        ident = err.source_id
        if (err.source_type or "").lower() == "email_queue" and ident and str(ident).isdigit():
            from models import EmailQueue
            item = (await db.execute(select(EmailQueue).where(EmailQueue.id == int(ident)))).scalars().first()
            if item:
                extra.setdefault("Queue item", str(item.id))
                if item.from_email:
                    extra.setdefault("From email", item.from_email)
                if item.to_email:
                    extra.setdefault("To email", item.to_email)
                if item.subject:
                    extra.setdefault("Subject", item.subject)
                consultant_id = consultant_id or (str(item.consultant_id) if item.consultant_id is not None else None)
                requirement_id = requirement_id or (str(item.requirement_id) if item.requirement_id is not None else None)
        if consultant_id and str(consultant_id).isdigit():
            from models import Consultant as _Cons
            c = (await db.execute(select(_Cons).where(_Cons.id == int(consultant_id)))).scalars().first()
            if c and c.full_name:
                extra.setdefault("Consultant", c.full_name)
        if requirement_id and str(requirement_id).isdigit():
            from models import Requirement as _Req
            rq = (await db.execute(select(_Req).where(_Req.id == int(requirement_id)))).scalars().first()
            if rq:
                raw_email_id = raw_email_id or (str(getattr(rq, "raw_email_id", None)) if getattr(rq, "raw_email_id", None) is not None else None)
                title = getattr(rq, "title", None) or getattr(rq, "job_title", None)
                if title:
                    extra.setdefault("Requirement", str(title))
    except Exception:
        # Context is a convenience: never let a lookup hide the error itself.
        pass

    return {
        "id": str(err.id),
        "source_type": err.source_type,
        "source_id": err.source_id,
        "error_stage": err.error_stage,
        "error_message": err.error_message,
        "occurred_at": err.occurred_at.isoformat() if err.occurred_at else "",
        "resolved_at": err.resolved_at.isoformat() if err.resolved_at else None,
        "status": err.status,
        "stack_trace": err.stack_trace,
        "retry_count": err.retry_count or 0,
        "category": _category_for_stage(err.error_stage),
        "raw_email_id": str(raw_email_id) if raw_email_id else None,
        "requirement_id": str(requirement_id) if requirement_id else None,
        "consultant_id": str(consultant_id) if consultant_id else None,
        "additional": extra,
    }


@router.post("/errors/{error_id}/retry", response_model=ErrorActionResultDTO)
async def retry_error(
    error_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
) -> ErrorActionResultDTO:
    """
    Records a retry attempt and, for a failed send, actually re-queues the
    email. Other stages have no automatic re-run yet, so the response says so
    rather than pretending the work was redone.
    """
    err = (await db.execute(select(ProcessingError).where(ProcessingError.id == error_id))).scalars().first()
    if not err:
        raise HTTPException(status_code=404, detail="Error not found")

    requeued = await _requeue_email_for(db, err)
    err.retry_count = (err.retry_count or 0) + 1
    err.last_retry_at = datetime.now(timezone.utc)
    if requeued:
        err.status = "CLOSED"
        err.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    return ErrorActionResultDTO(
        success=True, affected=1, requeued=1 if requeued else 0,
        message="Email put back in the send queue." if requeued
        else "Attempt recorded. This stage has no automatic re-run, so fix the cause and the next run will pick it up.",
    )


@router.post("/errors/{error_id}/close", response_model=ErrorActionResultDTO)
async def close_error(
    error_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
) -> ErrorActionResultDTO:
    """Dismiss one error: it leaves the queue, nothing is reprocessed."""
    err = (await db.execute(select(ProcessingError).where(ProcessingError.id == error_id))).scalars().first()
    if not err:
        raise HTTPException(status_code=404, detail="Error not found")
    err.status = "CLOSED"
    err.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    return ErrorActionResultDTO(success=True, affected=1, message="Error dismissed.")


@router.post("/errors/bulk", response_model=ErrorActionResultDTO)
async def bulk_error_action(
    body: ErrorBulkBody,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
) -> ErrorActionResultDTO:
    """
    Retry or dismiss many errors at once: either an explicit list of ids, or
    a whole group (its signature plus stage), which is what "Retry all" on a
    group card sends.
    """
    action = (body.action or "").lower()
    if action not in ("retry", "dismiss"):
        raise HTTPException(status_code=422, detail="action must be 'retry' or 'dismiss'")

    filters = [ProcessingError.status == "OPEN"]
    if body.ids:
        ids = [int(i) for i in body.ids if str(i).isdigit()][:5000]
        if not ids:
            raise HTTPException(status_code=422, detail="No valid error ids")
        filters.append(ProcessingError.id.in_(ids))
    elif body.signature is not None:
        filters.append(ProcessingError.error_message.like(body.signature.rstrip() + "%"))
        if body.error_stage:
            filters.append(ProcessingError.error_stage == body.error_stage)
        filters += _date_filters(body.date_filter)
    else:
        raise HTTPException(status_code=422, detail="Pass ids, or a signature")

    # Capped so one click can never run away with the whole table.
    rows = (await db.execute(
        select(ProcessingError).where(and_(*filters)).limit(5000)
    )).scalars().all()

    now = datetime.now(timezone.utc)
    requeued = 0
    for err in rows:
        if action == "retry":
            if await _requeue_email_for(db, err):
                requeued += 1
                err.status = "CLOSED"
                err.resolved_at = now
            err.retry_count = (err.retry_count or 0) + 1
            err.last_retry_at = now
        else:
            err.status = "CLOSED"
            err.resolved_at = now
    await db.commit()

    if action == "dismiss":
        msg = f"Dismissed {len(rows)} errors."
    elif requeued:
        msg = f"Re-queued {requeued} of {len(rows)} emails. The rest have no automatic re-run."
    else:
        msg = f"Recorded a retry on {len(rows)} errors. This stage has no automatic re-run."
    return ErrorActionResultDTO(success=True, affected=len(rows), requeued=requeued, message=msg)


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

@router.get("/ai-usage/logs", response_model=PaginatedAIUsageDTO)
async def ai_usage_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    sort_by: Optional[str] = Query("timestamp"),
    sort_dir: Optional[str] = Query("desc"),
    purpose: Optional[str] = None,
    consultant_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    filters = []
    if purpose:
        filters.append(AIUsageLog.purpose == purpose)
    if consultant_id:
        filters.append(AIUsageLog.consultant_id == consultant_id)
        
    base_filter = and_(*filters) if filters else True

    total = (await db.execute(select(func.count()).select_from(AIUsageLog).where(base_filter))).scalar_one()
    
    order_col = AIUsageLog.created_at
    if sort_by == "estimated_cost_usd":
        order_col = AIUsageLog.estimated_cost
    elif sort_by == "input_tokens":
        order_col = AIUsageLog.input_tokens
    
    if sort_dir == "asc":
        order_clause = order_col.asc()
    else:
        order_clause = order_col.desc()
        
    rows = (await db.execute(
        select(AIUsageLog).where(base_filter).order_by(order_clause)
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    data = [
        AIUsageLogRowDTO(
            id=str(r.id),
            timestamp=r.created_at.isoformat() if r.created_at else "",
            purpose=r.purpose,
            model=r.model,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            estimated_cost_usd=round(float(r.estimated_cost), 6) if r.estimated_cost else 0.0,
            consultant_id=r.consultant_id,
            consultant_name=r.consultant_name,
            requirement_id=r.requirement_id
        )
        for r in rows
    ]
    return PaginatedAIUsageDTO(data=data, total=total, page=page, page_size=page_size,
                               total_pages=math.ceil(total / page_size) or 1)

@router.get("/ai-usage/consultants", response_model=list[CostPerConsultantDTO])
async def ai_usage_consultants(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    query = (
        select(
            AIUsageLog.consultant_id,
            AIUsageLog.consultant_name,
            func.sum(AIUsageLog.estimated_cost).label("total_cost"),
            func.count().label("total_calls")
        )
        .where(AIUsageLog.consultant_id != None)
        .group_by(AIUsageLog.consultant_id, AIUsageLog.consultant_name)
    )
    
    res = await db.execute(query)
    rows = res.all()
    
    data = []
    for row in rows:
        c_id, c_name, t_cost, t_calls = row
        t_cost = float(t_cost or 0)
        data.append(CostPerConsultantDTO(
            consultant_id=c_id,
            consultant_name=c_name or c_id,
            total_cost_usd=round(t_cost, 4),
            total_resumes_generated=t_calls,
            avg_cost_per_resume_usd=round(t_cost / t_calls, 4) if t_calls > 0 else 0.0
        ))
    
    return sorted(data, key=lambda x: x.total_cost_usd, reverse=True)

@router.get("/ai-usage/stats", response_model=AIUsageStatsDTO)
async def ai_usage_stats(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    import openai_usage_client as ouc

    month_ts = ouc.month_start_ts()
    try:
        cost_buckets = await ouc.fetch_costs_buckets(month_ts)
        usage_buckets = await ouc.fetch_usage_buckets(month_ts)
    except ouc.OpenAIUsageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"OpenAI usage API unavailable: {e}")

    summary = ouc.summarize(usage_buckets or [], cost_buckets=cost_buckets)

    # Claude resume-generation usage this month (our own ai_usage_logs;
    # 'email_parsing' rows are excluded -- they are OpenAI, counted above).
    month_start = datetime.fromtimestamp(month_ts, timezone.utc)
    resume_tokens, resume_cost, resume_count = (await db.execute(
        select(
            func.coalesce(func.sum(AIUsageLog.input_tokens + AIUsageLog.output_tokens), 0),
            func.coalesce(func.sum(AIUsageLog.estimated_cost), 0),
            func.count(AIUsageLog.id),
        ).where(AIUsageLog.created_at >= month_start, AIUsageLog.purpose != "email_parsing")
    )).one()

    budget = await get_budget_threshold(db)
    total_cost = summary["cost_usd"]
    used_pct = (total_cost / budget * 100) if budget > 0 else 0.0
    return AIUsageStatsDTO(
        total_cost_usd=round(total_cost, 4),
        total_calls=summary["requests"],
        budget_usd=budget,
        budget_used_pct=round(used_pct, 2),
        total_tokens=summary["tokens"],
        resume_generation_tokens=int(resume_tokens or 0),
        resume_generation_cost_usd=round(float(resume_cost or 0), 4),
        resume_generation_count=int(resume_count or 0),
    )


@router.put("/ai-usage/budget", response_model=AIUsageStatsDTO)
async def update_ai_budget(
    body: SetBudgetRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    updated_by = current_user.get("sub")
    await set_budget_threshold(db, body.budget_usd, updated_by=updated_by)
    return AIUsageStatsDTO(total_cost_usd=0.0, total_calls=0, budget_usd=body.budget_usd, budget_used_pct=0.0)

def format_large_number(num: int) -> str:
    if num >= 1_000_000_000:
        return f"{num / 1_000_000_000:.1f}B".replace(".0B", "B")
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M".replace(".0M", "M")
    if num >= 1_000:
        return f"{num / 1_000:.1f}k".replace(".0k", "k")
    return str(num)

@router.get("/ai-usage/openai", response_model=OpenAIUsageDTO)
async def get_openai_usage(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin)
):
    import os
    import openai_usage_client as ouc

    if not os.getenv("OPENAI_ADMIN_API_KEY"):
        return OpenAIUsageDTO(tokens_limit="0", tokens_remaining="0", tokens_used_pct=0.0, tokens_reset="End of Month")

    total_tokens = 0
    try:
        usage_buckets = await ouc.fetch_usage_buckets(ouc.month_start_ts())
        total_tokens = ouc.summarize(usage_buckets or [])["tokens"]
    except Exception:
        pass

    limit = int(os.getenv("OPENAI_MONTHLY_TOKEN_LIMIT", "300000000"))
    remaining = max(0, limit - total_tokens)
    used_pct = (total_tokens / limit) * 100.0 if limit > 0 else 0.0

    return OpenAIUsageDTO(
        tokens_limit=format_large_number(limit),
        tokens_remaining=format_large_number(remaining),
        tokens_used_pct=round(used_pct, 2),
        tokens_reset="End of Month"
    )

@router.get("/ai-usage/daily")
async def ai_usage_daily(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    import openai_usage_client as ouc

    start_ts = ouc.days_start_ts(days)
    try:
        cost_buckets = await ouc.fetch_costs_buckets(start_ts)
    except ouc.OpenAIUsageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"OpenAI costs API unavailable: {e}")

    openai_daily = ouc.daily_costs_from_api(cost_buckets or [])

    start_dt = datetime.fromtimestamp(start_ts, timezone.utc)
    day_col = func.date(func.timezone("UTC", AIUsageLog.created_at))
    rows = (await db.execute(
        select(day_col, func.coalesce(func.sum(AIUsageLog.estimated_cost), 0))
        .where(AIUsageLog.created_at >= start_dt, AIUsageLog.purpose != "email_parsing")
        .group_by(day_col)
    )).all()
    resume_daily = {str(d): float(c or 0) for d, c in rows}

    if cost_buckets is None and not resume_daily:
        return []
    all_days = sorted(set(openai_daily) | set(resume_daily))
    return [
        {"date": d, "cost_usd": openai_daily.get(d, 0.0), "resume_cost_usd": resume_daily.get(d, 0.0)}
        for d in all_days
    ]

# ---------------------------------------------------------------------------
# Dashboard Stats & System Health
# ---------------------------------------------------------------------------

@router.get("/email-intake-chart", response_model=list[EmailIntakeDayDTO])
async def email_intake_chart(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    from datetime import datetime, timedelta
    import zoneinfo
    
    tz = zoneinfo.ZoneInfo("America/Chicago")
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    
    # 7 days including today (today and 6 previous days)
    start_date = today - timedelta(days=6)
    
    # Aggregate directly in SQL by CST day instead of transferring tens of thousands of rows
    res = await db.execute(
        text("""
            SELECT 
                to_char(COALESCE(fetched_at, date) AT TIME ZONE 'America/Chicago', 'YYYY-MM-DD') AS day_key,
                COUNT(*) FILTER (WHERE status_desc = 'Parsed') AS parsed,
                COUNT(*) FILTER (WHERE status_desc = 'Parsed - NR') AS parsed_nr,
                COUNT(*) FILTER (WHERE status_desc = 'Parsed - Dup') AS parsed_dup,
                COUNT(*) FILTER (WHERE status_desc = 'Failed') AS failed,
                COUNT(*) FILTER (WHERE status_desc = 'Pending') AS pending,
                COUNT(*) FILTER (WHERE status_desc IS NULL AND processed = true) AS fallback_parsed,
                COUNT(*) FILTER (WHERE status_desc IS NULL AND (processed IS NULL OR processed = false)) AS fallback_pending
            FROM gmail_emails
            WHERE COALESCE(fetched_at, date) >= :start
            GROUP BY 1
        """),
        {"start": start_date.astimezone(zoneinfo.ZoneInfo("UTC"))}
    )
    rows = res.mappings().all()
    
    days = {}
    for i in range(7):
        d = (today - timedelta(days=6 - i))
        days[d.strftime("%Y-%m-%d")] = EmailIntakeDayDTO(
            day=d.strftime("%a")
        )
    
    for row in rows:
        day_key = row.get("day_key")
        if day_key not in days:
            continue
            
        bucket = days[day_key]
        bucket.Parsed = int(row.get("parsed") or 0) + int(row.get("fallback_parsed") or 0)
        bucket.Parsed_NR = int(row.get("parsed_nr") or 0)
        bucket.Parsed_Dup = int(row.get("parsed_dup") or 0)
        bucket.Failed = int(row.get("failed") or 0)
        bucket.Pending = int(row.get("pending") or 0) + int(row.get("fallback_pending") or 0)

    return list(days.values())

@router.get("/error-rate", response_model=ErrorRateDTO)
async def error_rate(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    from datetime import datetime, timedelta
    import zoneinfo
    
    tz = zoneinfo.ZoneInfo("America/Chicago")
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    
    seven_days_ago = today - timedelta(days=7)
    fourteen_days_ago = today - timedelta(days=14)
    
    # Current window (7 days ago to today)
    curr_emails_res = await db.execute(
        text("SELECT COUNT(*) FROM gmail_emails WHERE COALESCE(fetched_at, date) >= :start AND COALESCE(fetched_at, date) < :end"),
        {"start": seven_days_ago.astimezone(zoneinfo.ZoneInfo("UTC")), "end": today.astimezone(zoneinfo.ZoneInfo("UTC"))}
    )
    current_emails = curr_emails_res.scalar_one() or 0
    
    curr_errors_res = await db.execute(
        text("SELECT COUNT(*) FROM processing_errors WHERE occurred_at >= :start AND occurred_at < :end"),
        {"start": seven_days_ago.astimezone(zoneinfo.ZoneInfo("UTC")), "end": today.astimezone(zoneinfo.ZoneInfo("UTC"))}
    )
    current_errors = curr_errors_res.scalar_one() or 0
    
    # Previous window (14 days ago to 7 days ago)
    prev_emails_res = await db.execute(
        text("SELECT COUNT(*) FROM gmail_emails WHERE COALESCE(fetched_at, date) >= :start AND COALESCE(fetched_at, date) < :end"),
        {"start": fourteen_days_ago.astimezone(zoneinfo.ZoneInfo("UTC")), "end": seven_days_ago.astimezone(zoneinfo.ZoneInfo("UTC"))}
    )
    prev_emails = prev_emails_res.scalar_one() or 0
    
    prev_errors_res = await db.execute(
        text("SELECT COUNT(*) FROM processing_errors WHERE occurred_at >= :start AND occurred_at < :end"),
        {"start": fourteen_days_ago.astimezone(zoneinfo.ZoneInfo("UTC")), "end": seven_days_ago.astimezone(zoneinfo.ZoneInfo("UTC"))}
    )
    prev_errors = prev_errors_res.scalar_one() or 0
    
    error_rate_pct = (current_errors / current_emails * 100) if current_emails > 0 else 0.0
    prev_pct = (prev_errors / prev_emails * 100) if prev_emails > 0 else 0.0
    
    if abs(error_rate_pct - prev_pct) < 0.1:
        trend = "stable"
    elif error_rate_pct > prev_pct:
        trend = "up"
    else:
        trend = "down"
        
    return ErrorRateDTO(
        error_rate_pct=error_rate_pct,
        trend=trend,
        prev_pct=prev_pct
    )

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
    # DASHBOARD FIX: was a LIFETIME sum shown as "AI Cost This Month".
    # Now current calendar month, Claude/resume usage only -- the frontend
    # adds the OpenAI month-to-date cost from /ai-usage/stats, so
    # 'email_parsing' rows are excluded here to avoid counting them twice.
    _month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total_ai_cost = (await db.execute(
        select(func.coalesce(func.sum(AIUsageLog.estimated_cost), 0))
        .where(AIUsageLog.created_at >= _month_start, AIUsageLog.purpose != "email_parsing")
    )).scalar_one()

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
        # DASHBOARD FIX: the sync itself sets category='unclassified' on
        # every "not a requirement" email, so the old count only ever grew.
        # Now: unclassified emails that are still waiting to be processed.
        text("SELECT COUNT(*) FROM gmail_emails WHERE (category IS NULL OR category = 'unclassified') AND (status_desc IS NULL OR status_desc = 'Pending')")
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
    # Same date_from tz-consistency fix as list_audit_logs above.
    if date_from: filters.append(AuditLog.created_at >= datetime.fromisoformat(date_from + "T00:00:00+00:00"))
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