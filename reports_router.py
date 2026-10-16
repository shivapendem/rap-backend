from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from database import get_db
from models import Email, Application, EmailQueue, User, Consultant, Requirement
from auth import get_current_user
from pydantic import BaseModel
from typing import List, Optional
import datetime
from collections import Counter
from zoneinfo import ZoneInfo

router = APIRouter(prefix="/api/reports", tags=["Reports"])

class UserReportStat(BaseModel):
    user_id: int
    user_name: str
    user_role: str
    applications_sent: int
    emails_sent: int
    # Most recent send (application or queued email) inside the selected
    # date range. Optional so older clients/rows are unaffected.
    last_sent_at: Optional[datetime.datetime] = None
    # Earliest SENT application inside the selected date range. This is the
    # primary sort key of the User Activity table (see _sort_user_activity).
    first_sent_at: Optional[datetime.datetime] = None
    # True for the logged-in caller's own row — the frontend tags it "You"
    # and the table lists it first.
    is_current_user: bool = False


# User Activity ordering: staff are grouped by role first
# (Admin, then Recruiter, then Consultant), and inside each role the user whose
# FIRST application in the selected period went out earliest comes first.
# Not by count, not by name, not by Last Sent.
_ROLE_PRIORITY = {"ADMIN": 0, "RECRUITER": 1, "CONSULTANT": 2}


def _sort_user_activity(rows: List["UserReportStat"]) -> List["UserReportStat"]:
    """Deterministic order: role priority -> earliest first_sent_at -> user_id.

    Users with no first_sent_at (no application activity) go after those with
    activity inside their own role; unknown roles go after the three known
    ones. user_id is the final tie-breaker so the order never flips between
    refreshes.
    """
    _no_time = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

    def _key(u: "UserReportStat"):
        ts = u.first_sent_at
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return (
            _ROLE_PRIORITY.get((u.user_role or "").upper(), len(_ROLE_PRIORITY)),
            ts is None,
            ts or _no_time,
            u.user_id,
        )

    # Logged-in user's own row always first, then the role/time order above.
    rows = sorted(rows, key=_key)
    return [u for u in rows if u.is_current_user] + [u for u in rows if not u.is_current_user]


class DailyActivity(BaseModel):
    # CST calendar day (YYYY-MM-DD) and SENT applications on that day, scoped
    # to the caller exactly like jobs_applied. Days with 0 are omitted; the
    # frontend fills the gaps.
    date: str
    applications: int


class VendorCount(BaseModel):
    vendor: str
    applications: int


class StatusCounts(BaseModel):
    sent: int = 0
    pending: int = 0
    failed: int = 0


class AdminReportResponse(BaseModel):
    emails_processed: int
    jobs_applied: int
    emails_sent: int
    applications_per_user: List[UserReportStat]
    daily_activity: List[DailyActivity] = []
    # Same-length window immediately before the selected range (only when both
    # dates are given) so the dashboard can show a trend. None = no comparison.
    prev_jobs_applied: Optional[int] = None
    prev_emails_sent: Optional[int] = None
    # Applications by status for the caller's scope inside the range.
    status_counts: StatusCounts = StatusCounts()
    # Vendors with the most SENT applications inside the range (top 5).
    top_vendors: List[VendorCount] = []

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

    # 2b. Daily activity — same scope/filters as jobs_applied, bucketed per
    # CST day in Python (portable across Postgres/SQLite).
    _cst = ZoneInfo("America/Chicago")
    day_query = select(Application.sent_at).where(
        Application.status == "SENT", Application.sent_at.is_not(None)
    )
    if start_dt:
        day_query = day_query.where(Application.sent_at >= start_dt)
    if end_dt:
        day_query = day_query.where(Application.sent_at <= end_dt)
    if current_user.role == "RECRUITER":
        day_query = day_query.where(Application.recruiter_id == current_user.id)
    elif current_user.role == "CONSULTANT":
        day_query = day_query.where(Application.consultant_id == (own_consultant_id or -1))
    _day_counts: Counter = Counter()
    for (ts,) in (await db.execute(day_query)).all():
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        _day_counts[ts.astimezone(_cst).date().isoformat()] += 1
    daily_activity = [DailyActivity(date=d, applications=n) for d, n in sorted(_day_counts.items())]

    def _scope_apps(q):
        if current_user.role == "RECRUITER":
            return q.where(Application.recruiter_id == current_user.id)
        if current_user.role == "CONSULTANT":
            return q.where(Application.consultant_id == (own_consultant_id or -1))
        return q

    # 2c. Trend: the window of the same length right before the range.
    prev_jobs_applied = None
    prev_emails_sent = None
    if start_dt and end_dt and end_dt > start_dt:
        _span = end_dt - start_dt
        _p_start, _p_end = start_dt - _span, start_dt
        prev_jobs_applied = (await db.execute(_scope_apps(
            select(func.count()).select_from(Application).where(
                Application.status == "SENT",
                Application.sent_at >= _p_start,
                Application.sent_at < _p_end,
            )
        ))).scalar_one()
        _pq = select(func.count()).select_from(EmailQueue).where(
            EmailQueue.status == "SENT",
            EmailQueue.created_at >= _p_start,
            EmailQueue.created_at < _p_end,
        )
        if current_user.role == "RECRUITER":
            _pq = _pq.where(EmailQueue.sent_by_user_id == current_user.id)
        elif current_user.role == "CONSULTANT":
            _pq = _pq.where(EmailQueue.consultant_id == (own_consultant_id or -1))
        prev_emails_sent = (await db.execute(_pq)).scalar_one()

    # 2d. Status breakdown (created_at because PENDING/FAILED have no sent_at).
    _sq = select(Application.status, func.count()).select_from(Application).group_by(Application.status)
    if start_dt:
        _sq = _sq.where(Application.created_at >= start_dt)
    if end_dt:
        _sq = _sq.where(Application.created_at <= end_dt)
    _by_status = {(st or "").upper(): n for st, n in (await db.execute(_scope_apps(_sq))).all()}
    status_counts = StatusCounts(
        sent=_by_status.get("SENT", 0),
        pending=_by_status.get("PENDING", 0),
        failed=_by_status.get("FAILED", 0),
    )

    # 2e. Top vendors by SENT applications.
    _vq = (
        select(Requirement.vendor, func.count(Application.id).label("n"))
        .join(Requirement, Requirement.id == Application.requirement_id)
        .where(Application.status == "SENT", Requirement.vendor.is_not(None))
        .group_by(Requirement.vendor)
        .order_by(func.count(Application.id).desc())
        .limit(5)
    )
    if start_dt:
        _vq = _vq.where(Application.sent_at >= start_dt)
    if end_dt:
        _vq = _vq.where(Application.sent_at <= end_dt)
    top_vendors = [
        VendorCount(vendor=v, applications=n)
        for v, n in (await db.execute(_scope_apps(_vq))).all()
        if (v or "").strip()
    ]

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

    # 4. User Stats (Applications per user).
    # ADMIN: every sender. RECRUITER / CONSULTANT: only their own row
    # ("Your Activity") — counted by applications/emails THEY sent
    # (Application.recruiter_id / EmailQueue.sent_by_user_id hold the sender
    # for every role), so a consultant's row excludes recruiter-sent ones.
    # BUG FIX: this block used to run for ADMIN only, so the recruiter and
    # consultant Reporting pages always showed an empty "Your Activity".
    # Users with 0 sent applications in the period stay hidden (inner join).
    is_admin = current_user.role == "ADMIN"
    applications_per_user: List[UserReportStat] = []
    if current_user.role in ("ADMIN", "RECRUITER", "CONSULTANT"):
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
                func.max(Application.sent_at).label("last_app_sent"),
                func.min(Application.sent_at).label("first_app_sent"),
            )
            .join(Application, Application.recruiter_id == User.id)
            .where(Application.status == "SENT")
            .group_by(User.id, User.full_name, User.role)
        )
        if not is_admin:
            stats_query = stats_query.where(User.id == current_user.id)
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
                func.max(EmailQueue.updated_at).label("last_email_sent"),
            )
            .where(EmailQueue.status == "SENT", EmailQueue.sent_by_user_id.is_not(None))
            .group_by(EmailQueue.sent_by_user_id)
        )
        if not is_admin:
            emails_query = emails_query.where(EmailQueue.sent_by_user_id == current_user.id)
        if start_dt:
            emails_query = emails_query.where(EmailQueue.created_at >= start_dt)
        if end_dt:
            emails_query = emails_query.where(EmailQueue.created_at <= end_dt)
        _email_rows = (await db.execute(emails_query)).all()
        emails_by_user = {row.user_id: row.email_count for row in _email_rows}
        last_email_by_user = {row.user_id: row.last_email_sent for row in _email_rows}

        for row in user_stats:
            applications_per_user.append(UserReportStat(
                user_id=row.user_id,
                user_name=row.user_name or "Unknown User",
                user_role=row.user_role or "",
                applications_sent=row.app_count,
                emails_sent=emails_by_user.get(row.user_id, 0),
                last_sent_at=max(
                    (t for t in (row.last_app_sent, last_email_by_user.get(row.user_id)) if t is not None),
                    default=None,
                ),
                first_sent_at=row.first_app_sent,
                is_current_user=(row.user_id == current_user.id),
            ))

        # Role group first, then earliest first application in the period.
        applications_per_user = _sort_user_activity(applications_per_user)

    return AdminReportResponse(
        emails_processed=emails_processed,
        jobs_applied=jobs_applied,
        emails_sent=emails_sent,
        applications_per_user=applications_per_user,
        daily_activity=daily_activity,
        prev_jobs_applied=prev_jobs_applied,
        prev_emails_sent=prev_emails_sent,
        status_counts=status_counts,
        top_vendors=top_vendors,
    )