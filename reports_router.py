from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from database import get_db
from models import Email, Application, EmailQueue, User, Consultant
from auth import get_current_user
from pydantic import BaseModel
from typing import List, Optional
import datetime

router = APIRouter(prefix="/api/reports", tags=["Reports"])

class UserReportStat(BaseModel):
    user_id: int
    user_name: str
    user_role: str
    applications_sent: int
    emails_sent: int

class AdminReportResponse(BaseModel):
    emails_processed: int
    jobs_applied: int
    emails_sent: int
    applications_per_user: List[UserReportStat]

@router.get("/", response_model=AdminReportResponse)
async def get_admin_reports(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    # BUG FIX: was ADMIN-only. The React app's Reporting page is now
    # routed for all three roles (admin/recruiter/consultant each have
    # their own Reporting screen), so recruiters and consultants hitting
    # this endpoint got a hard 403 instead of their own numbers. Opened
    # up to all three roles; scoping to "your own activity only" for
    # non-admins happens per-query below instead of at the door.
    if current_user.role not in ("ADMIN", "RECRUITER", "CONSULTANT"):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Parse dates if provided
    start_dt = None
    end_dt = None
    if start_date:
        try:
            start_dt = datetime.datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        except ValueError:
            pass

    # 1. Emails Processed — inbound parsing volume has no per-consultant
    # equivalent (emails aren't addressed to consultants), so this stays
    # an org-wide figure for every role. The frontend already labels it
    # "system-wide" when shown on a scoped (recruiter/consultant) view.
    email_query = select(func.count()).select_from(Email).where(Email.parse_status == 'PARSED')
    if start_dt:
        email_query = email_query.where(Email.received_at >= start_dt)
    if end_dt:
        email_query = email_query.where(Email.received_at <= end_dt)
    emails_processed = (await db.execute(email_query)).scalar_one()

    # Resolve the current user's own Consultant row up front (only exists
    # for CONSULTANT accounts) — used to scope the two queries below.
    own_consultant_id = None
    if current_user.role == "CONSULTANT":
        cons_res = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        own_consultant = cons_res.scalars().first()
        own_consultant_id = own_consultant.id if own_consultant else None

    # 2. Jobs Applied (Applications)
    # BUG FIX: was counting every Application row regardless of status
    # (PENDING/FAILED included) — "Jobs Applied" should mean applications
    # that actually went out, matching how "Emails Sent" below already
    # filters EmailQueue.status == 'SENT'.
    app_query = select(func.count()).select_from(Application).where(Application.status == "SENT")
    if start_dt:
        app_query = app_query.where(Application.sent_at >= start_dt)
    if end_dt:
        app_query = app_query.where(Application.sent_at <= end_dt)
    if current_user.role == "RECRUITER":
        app_query = app_query.where(Application.recruiter_id == current_user.id)
    elif current_user.role == "CONSULTANT":
        # No Consultant row for this user yet (shouldn't normally happen)
        # — force zero rows rather than accidentally returning org-wide.
        app_query = app_query.where(Application.consultant_id == (own_consultant_id or -1))
    jobs_applied = (await db.execute(app_query)).scalar_one()

    # 3. Emails Sent (EmailQueue)
    queue_query = select(func.count()).select_from(EmailQueue).where(EmailQueue.status == 'SENT')
    if start_dt:
        queue_query = queue_query.where(EmailQueue.created_at >= start_dt)
    if end_dt:
        queue_query = queue_query.where(EmailQueue.created_at <= end_dt)
    if current_user.role == "RECRUITER":
        queue_query = queue_query.where(EmailQueue.sent_by_user_id == current_user.id)
    elif current_user.role == "CONSULTANT":
        queue_query = queue_query.where(EmailQueue.consultant_id == (own_consultant_id or -1))
    emails_sent = (await db.execute(queue_query)).scalar_one()

    # 4. User Stats (Applications per user) — ADMIN only. Recruiters and
    # consultants already get their own scoped totals in the two cards
    # above; a one-row "you vs. nobody else" breakdown table adds nothing
    # for them, so the frontend hides this section entirely when scoped.
    applications_per_user: List[UserReportStat] = []
    if current_user.role == "ADMIN":
        # BUG FIX: was joining Application -> Consultant -> User via
        # Consultant.user_id == User.id — that attributes every application
        # to the CONSULTANT it was sent on behalf of, not to the staff
        # member (recruiter/admin) who actually sent it. Application.recruiter_id
        # (set in phase7.py's confirm_send whenever a RECRUITER sends) is
        # the real sender attribution — use that instead.
        stats_query = (
            select(
                User.id.label("user_id"),
                User.full_name.label("user_name"),
                User.role.label("user_role"),
                func.count(Application.id).label("app_count"),
            )
            .join(Application, Application.recruiter_id == User.id)
            .where(Application.status == "SENT")
            .group_by(User.id, User.full_name, User.role)
        )
        if start_dt:
            stats_query = stats_query.where(Application.sent_at >= start_dt)
        if end_dt:
            stats_query = stats_query.where(Application.sent_at <= end_dt)

        result = await db.execute(stats_query)
        user_stats = result.all()

        # Emails sent per user — now attributable via EmailQueue.sent_by_user_id,
        # which didn't exist when this table always reported 0 for every user.
        emails_query = (
            select(
                EmailQueue.sent_by_user_id.label("user_id"),
                func.count(EmailQueue.id).label("email_count"),
            )
            .where(EmailQueue.status == "SENT", EmailQueue.sent_by_user_id.is_not(None))
            .group_by(EmailQueue.sent_by_user_id)
        )
        if start_dt:
            emails_query = emails_query.where(EmailQueue.created_at >= start_dt)
        if end_dt:
            emails_query = emails_query.where(EmailQueue.created_at <= end_dt)
        emails_by_user = {row.user_id: row.email_count for row in (await db.execute(emails_query)).all()}

        for row in user_stats:
            applications_per_user.append(UserReportStat(
                user_id=row.user_id,
                user_name=row.user_name or "Unknown User",
                user_role=row.user_role or "",
                applications_sent=row.app_count,
                emails_sent=emails_by_user.get(row.user_id, 0)
            ))

    return AdminReportResponse(
        emails_processed=emails_processed,
        jobs_applied=jobs_applied,
        emails_sent=emails_sent,
        applications_per_user=applications_per_user
    )
