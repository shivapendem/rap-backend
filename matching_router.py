from typing import Optional
from datetime import datetime, timezone, timedelta
import logging
import math
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, exists, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import aliased

from models import User, Consultant, Requirement, RequirementConsultantMatch, RecruiterConsultant, Application
from database import get_db
from auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

# Single engine now — this router no longer scores anything itself. Every
# endpoint here either reads RequirementConsultantMatch (the one table the
# matching engine writes) or marks requirements/emails for the cron worker
# to match. No background task runs in the backend.


@router.post("/run")
async def trigger_matching_run(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Queues open requirements received in the last 24 hours for matching
    by updating their status in the requirements table. Actual matching is
    handled exclusively by the cron worker without touching gmail_emails.status_desc.
    """
    if current_user.role not in ["ADMIN", "RECRUITER"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    result = await db.execute(
        text("""
            UPDATE requirements
            SET status = 'REMATCH_REQUESTED'
            WHERE status NOT IN ('CLOSED', 'REJECTED', 'REMATCH_REQUESTED', 'REMATCHING')
              AND received_date >= NOW() - INTERVAL '24 hours'
            RETURNING id
        """)
    )
    queued_count = len(result.all())
    await db.commit()

    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "success": True,
        "started": True,
        "status": "running" if queued_count > 0 else "completed",
        "started_at": now_iso,
        "finished_at": None if queued_count > 0 else now_iso,
        "total_requirements": queued_count,
        "processed_requirements": 0,
        "new_matches": 0,
        "error": None,
        "cancel_requested": False,
    }


@router.post("/run/cancel")
async def cancel_matching_run(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancels pending 'REMATCH_REQUESTED' requirements by restoring their status to 'NEW'."""
    if current_user.role not in ["ADMIN", "RECRUITER"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    result = await db.execute(
        text(
            "UPDATE requirements SET status = 'NEW' "
            "WHERE status = 'REMATCH_REQUESTED' RETURNING id"
        )
    )
    cancelled_count = len(result.all())
    await db.commit()
    return {"success": True, "cancelled": cancelled_count > 0}


@router.get("/run/status")
async def get_matching_run_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["ADMIN", "RECRUITER"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    pending_res = await db.execute(
        text(
            "SELECT COUNT(*) FROM requirements "
            "WHERE status IN ('REMATCH_REQUESTED', 'REMATCHING')"
        )
    )
    pending = pending_res.scalar() or 0
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "status": "running" if pending > 0 else "completed",
        "started_at": None,
        "finished_at": None if pending > 0 else now_iso,
        "total_requirements": pending,
        "processed_requirements": 0,
        "new_matches": 0,
        "error": None,
        "cancel_requested": False,
    }


def _safe_float(val):
    if val is None:
        return None
    try:
        f = float(val)
        return f if not (math.isnan(f) or math.isinf(f)) else None
    except (ValueError, TypeError):
        return None


# Same invisible/lookalike-space character set as phase2.py's Gmail
# search fix (get_gmail_emails) — kept identical rather than re-derived so
# the two never drift apart. See that file for the full rationale: these
# are Unicode characters (narrow no-break space, zero-width space, word
# joiner, etc.) that render as an ordinary space, or nothing at all, but
# aren't matched by a plain ILIKE substring comparison or by Postgres's
# (ASCII-only) \s.
_INVISIBLE_SPACE_CHARS = (
    "\u00A0\u1680\u180E\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200A\u200B\u200C\u200D\u202F\u205F\u3000"
    "\u2060\uFEFF"
)
_INVISIBLE_SPACE_SQL_CLASS = (
    "[\\u00A0\\u1680\\u180E\\u2000-\\u200D\\u202F\\u205F\\u3000\\u2060\\uFEFF]"
)


def _normalize_ws_col(col):
    """Same normalization as phase2.py's Gmail search, applied to a
    SQLAlchemy column expression instead of a raw SQL fragment: fold every
    invisible/lookalike space character to a plain space, then collapse
    whitespace runs, so a stored value can be substring-matched regardless
    of which whitespace variant it actually contains."""
    return func.regexp_replace(
        func.regexp_replace(col, _INVISIBLE_SPACE_SQL_CLASS, " ", "g"),
        r"\s+", " ", "g",
    )


# Pending / Rejected / Not Eligible tabs only list matches from the last N days.
PENDING_WINDOW_DAYS = 4


@router.get("/pending")
async def get_pending_matches(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status: Optional[str] = Query(None),
    consultant_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(100, ge=1, le=200),
    search: Optional[str] = Query(None),
):
    """
    Matches for the current user's view, with optional status filter.
    Single query against RequirementConsultantMatch — the one table the
    matching engine writes, so this can never disagree with the
    Requirements page's per-requirement count again.

    status values: MATCHING (default) | APPLIED | REJECTED | NOT_ELIGIBLE.
    Near Miss no longer exists anywhere — a soft/borderline role match is
    rejected at validate_match()'s gate itself, so every MATCHING row is
    a confident match by construction.

    BUG FIX ("Pending Applications not showing all pending applications"):
    this used to hard-code .limit(200) with no offset/page parameter at
    all — `total` was always reported accurately, but the actual rows
    were silently capped at 200 regardless of how many real matches
    existed, with no way to ever see the rest. Real pagination now:
    100 per page by default (matches the Requirements page's own
    pagination), page/pageSize both controllable by the caller.

    BUG FIX ("search for the requirement shows no results, even though
    it's visibly in the list" -- confirmed real case): the frontend used
    to apply the search box's text entirely client-side, AFTER the page
    was already fetched -- filtering whatever rows happened to be on the
    current page, not the real underlying set. Any match on a different
    page was silently never fetched at all, so no amount of correct
    search text could ever find it. The `search` param now applies here,
    in the SQL, alongside the other filters and BEFORE pagination -- so
    a real match is found regardless of which page it would otherwise
    fall on, exactly like the other server-side filters already are.
    Matches the same fields the old client-side filter did (requirement
    title/company, candidate name/email), with the same
    invisible-whitespace normalization as the Gmail search fix so a
    copy-pasted/ATS-templated title with a look-alike space character
    doesn't reintroduce that exact bug here too.
    """
    valid_statuses = {"MATCHING", "APPLIED", "REJECTED", "NOT_ELIGIBLE"}
    # "ALL" = every status (All Applications). Anything unknown/missing
    # still defaults to MATCHING, as before.
    requested_status = status.upper().strip() if status else ""
    show_all_statuses = requested_status == "ALL"
    target_status = requested_status if requested_status in valid_statuses else "MATCHING"

    normalized_search = None
    if search:
        _search_table = str.maketrans({c: " " for c in _INVISIBLE_SPACE_CHARS})
        normalized_search = re.sub(r"\s+", " ", search.translate(_search_table)).strip()

    # A match whose consultant already has a SENT application for that same
    # requirement is APPLIED, whatever the match row still says — this is the
    # per-consultant (requirement + consultant) check, so other consultants
    # on the same requirement are unaffected.
    applied_exists = exists().where(
        Application.requirement_id == RequirementConsultantMatch.requirement_id,
        Application.consultant_id == RequirementConsultantMatch.consultant_id,
        Application.status == "SENT",
    )

    pending_cutoff = datetime.now(timezone.utc) - timedelta(days=PENDING_WINDOW_DAYS)

    def _base_stmt(select_clause):
        stmt = (
            select_clause
            .join(Requirement, RequirementConsultantMatch.requirement_id == Requirement.id)
            .join(Consultant, RequirementConsultantMatch.consultant_id == Consultant.id)
            .join(User, User.id == Consultant.user_id)
            .where(
                Consultant.status == "ACTIVE",
                User.is_authorized == True,
            )
        )
        if not show_all_statuses:
            if target_status in ("MATCHING", "NOT_ELIGIBLE"):
                stmt = stmt.where(RequirementConsultantMatch.status == target_status, ~applied_exists)
                # Pending and Not Eligible show only matches from the last
                # PENDING_WINDOW_DAYS days (older ones are hidden, not deleted).
                stmt = stmt.where(RequirementConsultantMatch.created_at >= pending_cutoff)
            elif target_status == "APPLIED":
                stmt = stmt.where(or_(
                    RequirementConsultantMatch.status == "APPLIED",
                    applied_exists & (RequirementConsultantMatch.status != "REJECTED"),
                ))
            else:
                stmt = stmt.where(RequirementConsultantMatch.status == target_status)
                # Rejected: last PENDING_WINDOW_DAYS days by the date shown on the
                # card ("Rejected on" = updated_at, falling back to created_at).
                if target_status == "REJECTED":
                    stmt = stmt.where(func.coalesce(RequirementConsultantMatch.updated_at, RequirementConsultantMatch.created_at) >= pending_cutoff)
        if consultant_id:
            c_ids = [int(cid.strip()) for cid in consultant_id.split(',') if cid.strip().isdigit()][:100]
            if c_ids:
                stmt = stmt.where(RequirementConsultantMatch.consultant_id.in_(c_ids))
        if normalized_search:
            pattern = f"%{normalized_search}%"
            stmt = stmt.where(or_(
                _normalize_ws_col(Requirement.role).ilike(pattern),
                _normalize_ws_col(func.coalesce(Requirement.client, Requirement.vendor)).ilike(pattern),
                _normalize_ws_col(Consultant.full_name).ilike(pattern),
                _normalize_ws_col(Consultant.email).ilike(pattern),
            ))
        if current_user.role == "CONSULTANT":
            cons_subq = select(Consultant.id).where(Consultant.user_id == current_user.id).scalar_subquery()
            stmt = stmt.where(RequirementConsultantMatch.consultant_id == cons_subq)
        elif current_user.role == "RECRUITER":
            assigned_subq = select(RecruiterConsultant.consultant_id).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.is_active == True,
            ).scalar_subquery()
            stmt = stmt.where(RequirementConsultantMatch.consultant_id.in_(assigned_subq))
        return stmt

    count_stmt = _base_stmt(select(func.count()).select_from(RequirementConsultantMatch))
    total_count = (await db.execute(count_stmt)).scalar_one()

    stmt = _base_stmt(select(
        RequirementConsultantMatch.id,
        RequirementConsultantMatch.requirement_id,
        Requirement.role.label("requirement_title"),
        func.coalesce(Requirement.client, Requirement.vendor).label("requirement_company"),
        Requirement.vendor_email.label("requirement_vendor_email"),
        Requirement.raw_email_id,
        RequirementConsultantMatch.consultant_id,
        Consultant.full_name.label("consultant_name"),
        Consultant.email.label("consultant_email"),
        RequirementConsultantMatch.match_score,
        RequirementConsultantMatch.score_breakdown,
        RequirementConsultantMatch.match_reason,
        RequirementConsultantMatch.status,
        RequirementConsultantMatch.resume_status,
        RequirementConsultantMatch.created_at,
        RequirementConsultantMatch.updated_at,
        applied_exists.label("has_sent_application"),
    ))
    # Same reasoning as before: order by match quality first, not by when
    # it happened to be scored, so a stronger match always sorts above a
    # weaker one; created_at as a stable tiebreaker for equal scores.
    # Newest match always first. Score-first buried every new match under
    # older 100% matches, so page 1 looked like matching had stopped.
    # Score and id are only tie-breakers (same timestamp) for stable paging.
    stmt = (
        stmt.order_by(
            RequirementConsultantMatch.created_at.desc(),
            RequirementConsultantMatch.match_score.desc(),
            RequirementConsultantMatch.id.desc(),
        )
        .offset((page - 1) * pageSize)
        .limit(pageSize)
    )

    result = await db.execute(stmt)
    rows = result.mappings().all()

    # PERF: Batch-load the "N matched" and "N more rejected" counts for the
    # current page's unique requirements in one quick grouped query, rather
    # than running 2 correlated subqueries per row.
    req_ids = list({row["requirement_id"] for row in rows if row["requirement_id"]})
    match_totals: dict[int, int] = {}
    rejected_totals: dict[int, int] = {}
    if req_ids:
        counts_stmt = (
            select(
                RequirementConsultantMatch.requirement_id,
                func.count(RequirementConsultantMatch.id).label("match_total"),
                func.count(RequirementConsultantMatch.id)
                .filter(RequirementConsultantMatch.status == "REJECTED")
                .label("rejected_total"),
            )
            .where(RequirementConsultantMatch.requirement_id.in_(req_ids))
            .group_by(RequirementConsultantMatch.requirement_id)
        )
        counts_res = await db.execute(counts_stmt)
        for c_row in counts_res.all():
            match_totals[c_row.requirement_id] = c_row.match_total
            rejected_totals[c_row.requirement_id] = c_row.rejected_total

    output = [
        {
            "id": row["id"],
            "requirement_id": row["requirement_id"],
            "requirement_title": row["requirement_title"],
            "requirement_company": row["requirement_company"],
            "requirement_vendor_email": row["requirement_vendor_email"],
            "raw_email_id": row["raw_email_id"],
            "consultant_id": row["consultant_id"],
            "consultant_name": row["consultant_name"],
            "consultant_email": row["consultant_email"],
            "match_score": _safe_float(row["match_score"]),
            "score_breakdown": row["score_breakdown"] or {},
            "match_reason": row["match_reason"],
            "status": (
                "APPLIED"
                if row["has_sent_application"] and row["status"] in ("MATCHING", "NOT_ELIGIBLE")
                else row["status"]
            ),
            "resume_status": row["resume_status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "requirement_match_total": match_totals.get(row["requirement_id"], 0),
            "requirement_rejected_total": rejected_totals.get(row["requirement_id"], 0),
        }
        for row in rows
    ]

    return {
        "matches": output,
        "total": total_count,
        "page": page,
        "pageSize": pageSize,
        "totalPages": (total_count + pageSize - 1) // pageSize if total_count else 1,
    }


async def _load_match_for_action(db: AsyncSession, match_id: int, current_user: User) -> RequirementConsultantMatch:
    result = await db.execute(select(RequirementConsultantMatch).where(RequirementConsultantMatch.id == match_id))
    match = result.scalars().first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    if current_user.role == "CONSULTANT":
        cons_check = await db.execute(
            select(Consultant.id).where(
                Consultant.id == match.consultant_id,
                Consultant.user_id == current_user.id,
            )
        )
        if not cons_check.scalars().first():
            raise HTTPException(status_code=404, detail="Match not found")
    elif current_user.role == "RECRUITER":
        assigned_check = await db.execute(
            select(RecruiterConsultant.id).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.consultant_id == match.consultant_id,
                RecruiterConsultant.is_active == True,
            )
        )
        if not assigned_check.scalars().first():
            raise HTTPException(status_code=404, detail="Match not found")

    return match


@router.post("/{match_id}/apply")
async def mark_match_applied(
    match_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Mark a match as applied. Scoped to the caller's own/assigned consultants.

    Also allowed from NOT_ELIGIBLE — the engine's disqualification is a
    signal, not a hard block; a recruiter can still choose to push an
    application through for a match the engine flagged as no longer
    meeting the automatic criteria.
    """
    match = await _load_match_for_action(db, match_id, current_user)

    if match.status not in ("MATCHING", "NOT_ELIGIBLE"):
        raise HTTPException(status_code=400, detail="Only matching or no-longer-eligible rows can be applied")

    match.status = "APPLIED"
    await db.commit()
    return {"success": True}


@router.patch("/{match_id}/reject")
async def reject_match(
    match_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Reject a match, hiding it from the default Pending Applications view."""
    match = await _load_match_for_action(db, match_id, current_user)

    if match.status != "MATCHING":
        raise HTTPException(status_code=400, detail="Only currently-matching rows can be rejected")

    match.status = "REJECTED"
    await db.commit()
    return {"success": True, "message": "Match rejected successfully"}


@router.patch("/{match_id}/restore")
async def restore_match(
    match_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Undo a reject: move a REJECTED match back to MATCHING."""
    match = await _load_match_for_action(db, match_id, current_user)

    if match.status != "REJECTED":
        raise HTTPException(status_code=400, detail="Only rejected rows can be restored")

    match.status = "MATCHING"
    await db.commit()
    return {"success": True, "message": "Match restored successfully"}