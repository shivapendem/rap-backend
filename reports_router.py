from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from database import get_db
from models import Email, Application, EmailQueue, User
from auth import get_current_user
from pydantic import BaseModel
from typing import List, Optional
import datetime

router = APIRouter(prefix="/api/reports", tags=["Reports"])

class UserReportStat(BaseModel):
    user_id: int
    user_name: str
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
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Only admins can view reports")

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

    # 1. Emails Processed
    email_query = select(func.count()).select_from(Email).where(Email.parse_status == 'PARSED')
    if start_dt:
        email_query = email_query.where(Email.received_at >= start_dt)
    if end_dt:
        email_query = email_query.where(Email.received_at <= end_dt)
    
    emails_processed = (await db.execute(email_query)).scalar_one()

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
    
    jobs_applied = (await db.execute(app_query)).scalar_one()

    # 3. Emails Sent (EmailQueue)
    queue_query = select(func.count()).select_from(EmailQueue).where(EmailQueue.status == 'SENT')
    if start_dt:
        queue_query = queue_query.where(EmailQueue.created_at >= start_dt)
    if end_dt:
        queue_query = queue_query.where(EmailQueue.created_at <= end_dt)
    
    emails_sent = (await db.execute(queue_query)).scalar_one()

    # 4. User Stats (Applications per user)
    # BUG FIX: was joining Application -> Consultant -> User via
    # Consultant.user_id == User.id — that attributes every application to
    # the CONSULTANT it was sent on behalf of, not to the staff member
    # (recruiter/admin) who actually sent it. For a "User Activity" report
    # meant to show staff productivity, that's the wrong entity entirely —
    # most consultants never appear here since they don't send their own
    # applications, which is exactly why this table looked empty even with
    # real application data in the system. Application.recruiter_id (set
    # in phase7.py's confirm_send whenever a RECRUITER sends) is the real
    # sender attribution — use that instead.
    #
    # Caveat: admin-sent confirm-sends and email-queue-sourced applications
    # don't set recruiter_id (no reliable "who queued this" field exists
    # on EmailQueue either), so this table only reflects recruiter
    # confirm-send activity, not the full system total shown in the cards
    # above. Real gap, not invented data — flagging rather than guessing.
    stats_query = (
        select(
            User.id.label("user_id"),
            User.full_name.label("user_name"),
            func.count(Application.id).label("app_count"),
        )
        .join(Application, Application.recruiter_id == User.id)
        .where(Application.status == "SENT")
        .group_by(User.id, User.full_name)
    )
    if start_dt:
        stats_query = stats_query.where(Application.sent_at >= start_dt)
    if end_dt:
        stats_query = stats_query.where(Application.sent_at <= end_dt)

    result = await db.execute(stats_query)
    user_stats = result.all()

    applications_per_user = []
    for row in user_stats:
        # BUG FIX: emails_sent per user used to query EmailQueue joined via
        # Consultant.user_id — same wrong-entity bug as above, and doubly
        # broken since EmailQueue has no column at all recording who
        # queued/composed an item (verified against models.py — no
        # created_by/user_id-of-sender field exists). There is no reliable
        # way to attribute a queued email to a staff user with the current
        # schema, so this is left at 0 rather than reporting a number that
        # looks real but isn't — same "not tracked by backend" gap already
        # documented in applications.api.ts for sentBy elsewhere in this app.
        applications_per_user.append(UserReportStat(
            user_id=row.user_id,
            user_name=row.user_name or "Unknown User",
            applications_sent=row.app_count,
            emails_sent=0
        ))

    return AdminReportResponse(
        emails_processed=emails_processed,
        jobs_applied=jobs_applied,
        emails_sent=emails_sent,
        applications_per_user=applications_per_user
    )