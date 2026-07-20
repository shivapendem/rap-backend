from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from database import get_db
from models import Email, Application, EmailQueue, Consultant, User
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
    app_query = select(func.count()).select_from(Application)
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
    # We join Application -> Consultant -> User
    stats_query = (
        select(
            User.id.label("user_id"),
            User.full_name.label("user_name"),
            func.count(Application.id).label("app_count")
        )
        .join(Consultant, Consultant.user_id == User.id)
        .join(Application, Application.consultant_id == Consultant.id)
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
        # For emails sent, we'll do a subquery or secondary query per user for simplicity, 
        # or we could just map 'emails_sent' to the 'app_count' for now since usually 1 app = 1 email,
        # but let's do a fast secondary query per user since admin table sizes are small.
        user_queue_query = (
            select(func.count())
            .select_from(EmailQueue)
            .join(Consultant, EmailQueue.consultant_id == Consultant.id)
            .where(Consultant.user_id == row.user_id)
            .where(EmailQueue.status == 'SENT')
        )
        if start_dt:
            user_queue_query = user_queue_query.where(EmailQueue.created_at >= start_dt)
        if end_dt:
            user_queue_query = user_queue_query.where(EmailQueue.created_at <= end_dt)
        
        user_emails = (await db.execute(user_queue_query)).scalar_one()

        applications_per_user.append(UserReportStat(
            user_id=row.user_id,
            user_name=row.user_name or "Unknown User",
            applications_sent=row.app_count,
            emails_sent=user_emails
        ))

    return AdminReportResponse(
        emails_processed=emails_processed,
        jobs_applied=jobs_applied,
        emails_sent=emails_sent,
        applications_per_user=applications_per_user
    )
