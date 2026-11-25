from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, text, and_, literal_column
from database import get_db, DATABASE_URL
from models import Email, Application, EmailQueue, User, Consultant, Requirement
from account_mode import load_account_modes, public_clause, private_clause
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


class HourlyActivity(BaseModel):
    # CST hour of day (0-23) and SENT applications in that hour. Only filled
    # when the selected range is a single CST day (the "Today" view). Hours
    # with 0 are omitted; the frontend fills the gaps.
    hour: int
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
    # "Requirements Received" card: emails_processed = public + private.
    # ADMIN: private = every Private Gmail account (or only the picked
    # recruiters' own Private Gmail when the Recruiter filter is on).
    # RECRUITER: private = their own Private Gmail only.
    # CONSULTANT: both None (card stays a system-wide total).
    emails_processed_public: Optional[int] = None
    emails_processed_private: Optional[int] = None
    # "all" | "own" | "recruiters" — tells the UI how to label Private.
    emails_processed_private_scope: Optional[str] = None
    jobs_applied: int
    emails_sent: int
    applications_per_user: List[UserReportStat]
    daily_activity: List[DailyActivity] = []
    # Same-length window immediately before the selected range (only when both
    # dates are given) so the dashboard can show a trend. None = no comparison.
    prev_jobs_applied: Optional[int] = None
    prev_emails_sent: Optional[int] = None
    # Org-wide parsed inbound emails in the previous window (same rule as
    # emails_processed) so that card can show a trend too.
    prev_emails_processed: Optional[int] = None
    # Same bucketing as daily_activity, for the previous window, so the chart
    # can draw "this period" against "previous period" day by day.
    prev_daily_activity: List[DailyActivity] = []
    # Single-day ranges only: per-hour buckets for the day and the day before.
    hourly_activity: List[HourlyActivity] = []
    prev_hourly_activity: List[HourlyActivity] = []
    # Applications by status for the caller's scope inside the range.
    status_counts: StatusCounts = StatusCounts()
    # Vendors with the most SENT applications inside the range (top 5).
    top_vendors: List[VendorCount] = []

@router.get("/", response_model=AdminReportResponse)
async def get_admin_reports(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    # Admin-only filters (comma-separated ids). consultant_ids = consultants
    # the applications were made for; recruiter_ids = users who sent them.
    consultant_ids: Optional[str] = Query(None),
    recruiter_ids: Optional[str] = Query(None),
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
    emails_processed = 0
    try:
        ge_clauses = [
            "(status_desc IN ('Parsed', 'Parsed - NR', 'Parsed - Dup')"
            " OR (status_desc IS NULL AND processed = true))"
        ]
        ge_params = {}
        if start_dt:
            ge_clauses.append("COALESCE(fetched_at, date) >= :start_dt")
            ge_params["start_dt"] = start_dt
        if end_dt:
            ge_clauses.append("COALESCE(fetched_at, date) <= :end_dt")
            ge_params["end_dt"] = end_dt
        ge_where = " AND ".join(ge_clauses)
        emails_processed = (
            await db.execute(text(f"SELECT COUNT(*) FROM gmail_emails WHERE {ge_where}"), ge_params)
        ).scalar_one() or 0
    except Exception:
        await db.rollback()
        email_query = select(func.count()).select_from(Email).where(Email.parse_status == 'PARSED')
        if start_dt:
            email_query = email_query.where(Email.received_at >= start_dt)
        if end_dt:
            email_query = email_query.where(Email.received_at <= end_dt)
        emails_processed = (await db.execute(email_query)).scalar_one() or 0

    # Resolve the current user's own Consultant row up front (only exists
    # for CONSULTANT accounts) — used to scope the two queries below.
    own_consultant_id = None
    if current_user.role == "CONSULTANT":
        cons_res = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        own_consultant = cons_res.scalars().first()
        own_consultant_id = own_consultant.id if own_consultant else None

    def _ids(raw: Optional[str]) -> List[int]:
        return [int(p) for p in (raw or "").split(",") if p.strip().isdigit()]

    # Reporting page Consultant / Recruiter dropdowns — admin only; the
    # cards and the User Activity table both follow them.
    f_cons = _ids(consultant_ids) if current_user.role == "ADMIN" else []
    f_recs = _ids(recruiter_ids) if current_user.role == "ADMIN" else []

    def _scope_apps(q):
        if current_user.role == "RECRUITER":
            q = q.where(Application.recruiter_id == current_user.id)
        elif current_user.role == "CONSULTANT":
            q = q.where(Application.consultant_id == (own_consultant_id or -1))
        if f_cons:
            q = q.where(Application.consultant_id.in_(f_cons))
        if f_recs:
            q = q.where(Application.recruiter_id.in_(f_recs))
        return q

    def _scope_queue(q):
        if current_user.role == "RECRUITER":
            q = q.where(EmailQueue.sent_by_user_id == current_user.id)
        elif current_user.role == "CONSULTANT":
            q = q.where(EmailQueue.consultant_id == (own_consultant_id or -1))
        if f_cons:
            q = q.where(EmailQueue.consultant_id.in_(f_cons))
        if f_recs:
            q = q.where(EmailQueue.sent_by_user_id.in_(f_recs))
        return q

    # 1b. Requirements Received — counts REQUIREMENTS only (one row per
    # de-duplicated requirement), not every parsed email (non-requirement
    # emails, application replies etc. are not counted). Split into
    # Public + Private with the same rule the Requirements screen uses
    # (account_mode: the Gmail account's mode decides). Overrides
    # emails_processed for ADMIN / RECRUITER; CONSULTANT keeps the old
    # system-wide parsed-email figure.
    emails_processed_public = None
    emails_processed_private = None
    emails_processed_private_scope = None
    if current_user.role in ("ADMIN", "RECRUITER"):
        _modes = await load_account_modes(db)
        _req_day = func.coalesce(Requirement.received_date, Requirement.created_at)
        _req_inbox = func.lower(func.trim(func.coalesce(Requirement.account_email, "")))

        def _req_count(*conds):
            q = select(func.count()).select_from(Requirement).where(*conds)
            if start_dt:
                q = q.where(_req_day >= start_dt)
            if end_dt:
                q = q.where(_req_day <= end_dt)
            return q

        emails_processed_public = (await db.execute(_req_count(
            public_clause(Requirement.mode, Requirement.account_email, _modes)
        ))).scalar_one()

        _priv = private_clause(Requirement.mode, Requirement.account_email, _modes)
        if current_user.role == "ADMIN" and not f_recs:
            emails_processed_private_scope = "all"
            emails_processed_private = (await db.execute(_req_count(_priv))).scalar_one()
        else:
            if current_user.role == "RECRUITER":
                _own = [current_user.email or ""]
                emails_processed_private_scope = "own"
            else:
                _own = (await db.execute(select(User.email).where(User.id.in_(f_recs)))).scalars().all()
                emails_processed_private_scope = "recruiters"
            _own_norm = sorted({(e or "").lower().strip() for e in _own if (e or "").strip()})
            emails_processed_private = (
                (await db.execute(_req_count(_priv, _req_inbox.in_(_own_norm)))).scalar_one()
                if _own_norm else 0
            )
        emails_processed = emails_processed_public + emails_processed_private

    # 2. Jobs Applied (Applications) + 2c. previous-window trend, in ONE
    # round-trip. "Jobs Applied" = applications that actually went out
    # (status SENT), matching how "Emails Sent" filters EmailQueue.status.
    # Each window is a FILTER aggregate over one index range scan on
    # (recruiter_id|consultant_id, status, sent_at).
    has_prev = bool(start_dt and end_dt and end_dt > start_dt)
    _p_start = _p_end = None
    prev_emails_processed = None
    prev_daily_activity: List[DailyActivity] = []
    hourly_activity: List[HourlyActivity] = []
    prev_hourly_activity: List[HourlyActivity] = []

    if has_prev:
        _span = end_dt - start_dt
        _p_start, _p_end = start_dt - _span, start_dt
        try:
            prev_ge_clauses = [
                "(status_desc IN ('Parsed', 'Parsed - NR', 'Parsed - Dup')"
                " OR (status_desc IS NULL AND processed = true))",
                "COALESCE(fetched_at, date) >= :p_start",
                "COALESCE(fetched_at, date) < :p_end",
            ]
            prev_ge_params = {"p_start": _p_start, "p_end": _p_end}
            prev_ge_where = " AND ".join(prev_ge_clauses)
            prev_emails_processed = (
                await db.execute(text(f"SELECT COUNT(*) FROM gmail_emails WHERE {prev_ge_where}"), prev_ge_params)
            ).scalar_one() or 0
        except Exception:
            await db.rollback()
            prev_emails_processed = (await db.execute(
                select(func.count()).select_from(Email).where(
                    Email.parse_status == 'PARSED',
                    Email.received_at >= _p_start,
                    Email.received_at < _p_end,
                )
            )).scalar_one() or 0

    cur_cond = []
    if start_dt:
        cur_cond.append(Application.sent_at >= start_dt)
    if end_dt:
        cur_cond.append(Application.sent_at <= end_dt)

    sent_cols = [func.count().filter(and_(*cur_cond)) if cur_cond else func.count()]
    if has_prev:
        sent_cols.append(func.count().filter(and_(
            Application.sent_at >= _p_start, Application.sent_at < _p_end,
        )))
    sent_q = select(*sent_cols).select_from(Application).where(Application.status == "SENT")
    lower_bound = _p_start if has_prev else start_dt
    if lower_bound:
        sent_q = sent_q.where(Application.sent_at >= lower_bound)
    if end_dt:
        sent_q = sent_q.where(Application.sent_at <= end_dt)
    sent_row = (await db.execute(_scope_apps(sent_q))).one()
    jobs_applied = int(sent_row[0] or 0)
    prev_jobs_applied = int(sent_row[1] or 0) if has_prev else None

    # 2b. Daily activity + Hourly activity (when single-day range)
    _cst = ZoneInfo("America/Chicago")
    _day_counts: Counter = Counter()
    _cur_hours: Counter = Counter()
    _prev_hours: Counter = Counter()
    _prev_days: Counter = Counter()
    _single_day = has_prev and (_span <= datetime.timedelta(days=1))
    _start_aware = (start_dt if (start_dt and start_dt.tzinfo) else (start_dt.replace(tzinfo=datetime.timezone.utc) if start_dt else None))

    if DATABASE_URL.startswith("postgresql") and not has_prev:
        # Constants are inlined (literal_column) so the SELECT and GROUP BY
        # expressions are textually identical — separate bind params would
        # make Postgres reject the GROUP BY.
        day_col = func.to_char(
            func.timezone(literal_column("'America/Chicago'"), Application.sent_at),
            literal_column("'YYYY-MM-DD'"),
        )
        day_query = (
            select(day_col, func.count())
            .where(Application.status == "SENT", Application.sent_at.is_not(None))
            .group_by(day_col)
        )
        if start_dt:
            day_query = day_query.where(Application.sent_at >= start_dt)
        if end_dt:
            day_query = day_query.where(Application.sent_at <= end_dt)
        for d, n in (await db.execute(_scope_apps(day_query))).all():
            _day_counts[d] = int(n)
        daily_activity = [DailyActivity(date=d, applications=n) for d, n in sorted(_day_counts.items())]
    else:
        # Fetch timestamps covering both windows (if has_prev) or current window
        series_q = select(Application.sent_at).where(
            Application.status == "SENT", Application.sent_at.is_not(None)
        )
        if lower_bound:
            series_q = series_q.where(Application.sent_at >= lower_bound)
        if end_dt:
            series_q = series_q.where(Application.sent_at <= end_dt)
        for (ts,) in (await db.execute(_scope_apps(series_q))).all():
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            local = ts.astimezone(_cst)
            if _start_aware and ts < _start_aware:
                _prev_days[local.date().isoformat()] += 1
                if _single_day:
                    _prev_hours[local.hour] += 1
            else:
                _day_counts[local.date().isoformat()] += 1
                if _single_day:
                    _cur_hours[local.hour] += 1
        daily_activity = [DailyActivity(date=d, applications=n) for d, n in sorted(_day_counts.items())]
        if has_prev:
            prev_daily_activity = [DailyActivity(date=d, applications=n) for d, n in sorted(_prev_days.items())]
            if _single_day:
                hourly_activity = [HourlyActivity(hour=h, applications=n) for h, n in sorted(_cur_hours.items())]
                prev_hourly_activity = [HourlyActivity(hour=h, applications=n) for h, n in sorted(_prev_hours.items())]

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
        # Failed applications being replayed (QUEUED) still count as failed.
        failed=_by_status.get("FAILED", 0) + _by_status.get("QUEUED", 0),
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

    # 3. Emails Sent (EmailQueue) + previous-window trend in ONE round-trip.
    eq_cur = []
    if start_dt:
        eq_cur.append(EmailQueue.created_at >= start_dt)
    if end_dt:
        eq_cur.append(EmailQueue.created_at <= end_dt)
    eq_cols = [func.count().filter(and_(*eq_cur)) if eq_cur else func.count()]
    if has_prev:
        eq_cols.append(func.count().filter(and_(
            EmailQueue.created_at >= _p_start, EmailQueue.created_at < _p_end,
        )))
    queue_query = select(*eq_cols).select_from(EmailQueue).where(EmailQueue.status == 'SENT')
    eq_lower = _p_start if has_prev else start_dt
    if eq_lower:
        queue_query = queue_query.where(EmailQueue.created_at >= eq_lower)
    if end_dt:
        queue_query = queue_query.where(EmailQueue.created_at <= end_dt)
    queue_query = _scope_queue(queue_query)
    eq_row = (await db.execute(queue_query)).one()
    emails_sent = int(eq_row[0] or 0)
    prev_emails_sent = int(eq_row[1] or 0) if has_prev else None

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
        if f_cons:
            stats_query = stats_query.where(Application.consultant_id.in_(f_cons))
        if f_recs:
            stats_query = stats_query.where(User.id.in_(f_recs))
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
        if f_cons:
            emails_query = emails_query.where(EmailQueue.consultant_id.in_(f_cons))
        if f_recs:
            emails_query = emails_query.where(EmailQueue.sent_by_user_id.in_(f_recs))
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
        emails_processed_public=emails_processed_public,
        emails_processed_private=emails_processed_private,
        emails_processed_private_scope=emails_processed_private_scope,
        jobs_applied=jobs_applied,
        emails_sent=emails_sent,
        applications_per_user=applications_per_user,
        daily_activity=daily_activity,
        prev_jobs_applied=prev_jobs_applied,
        prev_emails_sent=prev_emails_sent,
        prev_emails_processed=prev_emails_processed,
        prev_daily_activity=prev_daily_activity,
        hourly_activity=hourly_activity,
        prev_hourly_activity=prev_hourly_activity,
        status_counts=status_counts,
        top_vendors=top_vendors,
    )