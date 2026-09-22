from typing import Optional
from datetime import datetime, timezone, timedelta
import logging
import math
import re

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from models import User, Consultant, Requirement, RequirementConsultantMatch, RecruiterConsultant
from database import get_db, AsyncSessionLocal
from auth import get_current_user
from phase4 import match_requirement

router = APIRouter()
logger = logging.getLogger(__name__)

# Single engine now — this router no longer scores anything itself. Every
# endpoint here either reads RequirementConsultantMatch (the one table the
# matching engine writes) or triggers phase4.match_requirement() per
# requirement. There is no second table, no second pass, no TF-IDF/sklearn
# fallback path — that all lived in the deleted Pipeline B
# (matching_router_engine.py / JobMatch), which this file used to mirror.

# Bulk "Run Engine" still runs as a background task with polling rather
# than one long HTTP request — same reasoning as before: scoring every
# open requirement against every active consultant only gets larger as
# the dataset grows, so a fixed request timeout eventually gets crossed
# again regardless of the number chosen. In-memory state is fine for this
# single-process, admin-triggered, non-critical-path operation.
_matching_run_state: dict = {
    "status": "idle",  # idle | running | completed | failed
    "started_at": None,
    "finished_at": None,
    "total_requirements": 0,
    "processed_requirements": 0,
    "new_matches": 0,
    "error": None,
}


async def _run_matching_engine_background():
    """
    Loops open (non-terminal) requirements received in the last 24 hours
    and calls match_requirement() for each — the same single engine used
    by auto-sync, reparse, and the per-requirement admin "Rematch"
    button. New requirements are already auto-matched the moment they're
    synced; this exists to catch anything auto-sync missed, not to redo
    the whole backlog. Keeping it scoped to 24h means it always finishes
    in one run and can't be caught mid-pass by a redeploy/restart.

    Per-requirement isolation: one bad requirement is logged and skipped
    rather than aborting the whole run.
    """
    global _matching_run_state
    try:
        async with AsyncSessionLocal() as db:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            reqs_res = await db.execute(
                select(Requirement.id).where(
                    Requirement.status.notin_(Requirement.TERMINAL_STATUSES),
                    Requirement.received_date >= since,
                )
            )
            requirement_ids = [row[0] for row in reqs_res.all()]
            _matching_run_state["total_requirements"] = len(requirement_ids)

            total_matching = 0
            processed = 0
            for req_id in requirement_ids:
                try:
                    total_matching += await match_requirement(db, req_id)
                except Exception as req_err:
                    await db.rollback()
                    logger.error(
                        "[RequirementConsultantMatch] Skipping requirement_id=%s (failed): %s",
                        req_id, req_err,
                    )
                    try:
                        from error_logger import log_db_error
                        await log_db_error(
                            stage="matching_batch_requirement",
                            error=req_err,
                            source_type="requirement",
                            source_id=req_id,
                        )
                    except Exception:
                        pass
                finally:
                    processed += 1
                    _matching_run_state["processed_requirements"] = processed
                    _matching_run_state["new_matches"] = total_matching

            _matching_run_state.update({
                "status": "completed",
                "new_matches": total_matching,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as exc:
        logger.error("[RequirementConsultantMatch] Bulk run failed: %s", exc)
        _matching_run_state.update({
            "status": "failed",
            "error": str(exc),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })


@router.post("/run")
async def trigger_matching_run(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user)
):
    """
    Triggers the matching engine and returns immediately — the actual
    work happens in the background. Poll GET /run/status for progress.
    Scoped to requirements received in the last 24 hours — new
    requirements are already auto-matched on sync, so this just catches
    anything that slipped through.
    """
    if current_user.role not in ["ADMIN", "RECRUITER"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    if _matching_run_state["status"] == "running":
        return {"success": True, "already_running": True, **_matching_run_state}

    _matching_run_state.update({
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "total_requirements": 0,
        "processed_requirements": 0,
        "new_matches": 0,
        "error": None,
    })
    background_tasks.add_task(_run_matching_engine_background)
    return {"success": True, "started": True, **_matching_run_state}


@router.get("/run/status")
async def get_matching_run_status(
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in ["ADMIN", "RECRUITER"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    return _matching_run_state


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


@router.get("/pending")
async def get_pending_matches(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status: Optional[str] = Query(None),
    consultant_id: Optional[str] = Query(None),
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

    BUG FIX ("search for the requirement shows no results, even though
    it's visibly in the list" -- confirmed real case): this endpoint
    already truncates to the 200 highest-scoring rows (see .limit(200)
    below) so the "Pending Applications" screen doesn't have to paginate.
    The frontend used to apply the search box's text entirely
    client-side, AFTER that cap -- filtering whatever 200 rows happened
    to be fetched, not the real underlying set. Any match that scored
    outside the top 200 was silently never fetched at all, so no amount
    of correct search text could ever find it; it only reappeared once a
    real server-side filter (the candidate dropdown) shrank the
    underlying result set enough for that row to fit inside the cap. The
    `search` param now applies here, in the SQL, alongside the other
    filters and BEFORE the LIMIT -- so a real match is found regardless
    of its score rank, exactly like the other server-side filters
    already are. Matches the same fields the old client-side filter did
    (requirement title/company, candidate name/email), with the same
    invisible-whitespace normalization as the Gmail search fix so a
    copy-pasted/ATS-templated title with a look-alike space character
    doesn't reintroduce that exact bug here too.
    """
    valid_statuses = {"MATCHING", "APPLIED", "REJECTED", "NOT_ELIGIBLE"}
    target_status = status.upper().strip() if status and status.upper().strip() in valid_statuses else "MATCHING"

    normalized_search = None
    if search:
        _search_table = str.maketrans({c: " " for c in _INVISIBLE_SPACE_CHARS})
        normalized_search = re.sub(r"\s+", " ", search.translate(_search_table)).strip()

    def _base_stmt(select_clause):
        stmt = (
            select_clause
            .join(Requirement, RequirementConsultantMatch.requirement_id == Requirement.id)
            .join(Consultant, RequirementConsultantMatch.consultant_id == Consultant.id)
            .join(User, User.id == Consultant.user_id)
            .where(
                RequirementConsultantMatch.status == target_status,
                Consultant.status == "ACTIVE",
                User.is_authorized == True,
            )
        )
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

    stmt = _base_stmt(select(
        RequirementConsultantMatch.id,
        RequirementConsultantMatch.requirement_id,
        Requirement.role.label("requirement_title"),
        func.coalesce(Requirement.client, Requirement.vendor).label("requirement_company"),
        Requirement.vendor_email.label("requirement_vendor_email"),
        RequirementConsultantMatch.consultant_id,
        Consultant.full_name.label("consultant_name"),
        Consultant.email.label("consultant_email"),
        RequirementConsultantMatch.match_score,
        RequirementConsultantMatch.score_breakdown,
        RequirementConsultantMatch.match_reason,
        RequirementConsultantMatch.status,
        RequirementConsultantMatch.resume_status,
        RequirementConsultantMatch.created_at,
    ))
    # Same reasoning as before: order by match quality first, not by when
    # it happened to be scored, so a stronger match always sorts above a
    # weaker one; created_at as a stable tiebreaker for equal scores.
    stmt = stmt.order_by(RequirementConsultantMatch.match_score.desc(), RequirementConsultantMatch.created_at.desc()).limit(200)

    result = await db.execute(stmt)
    rows = result.mappings().all()

    count_stmt = _base_stmt(select(func.count()).select_from(RequirementConsultantMatch))
    total_count = (await db.execute(count_stmt)).scalar_one()

    output = [
        {
            "id": row["id"],
            "requirement_id": row["requirement_id"],
            "requirement_title": row["requirement_title"],
            "requirement_company": row["requirement_company"],
            "requirement_vendor_email": row["requirement_vendor_email"],
            "consultant_id": row["consultant_id"],
            "consultant_name": row["consultant_name"],
            "consultant_email": row["consultant_email"],
            "match_score": _safe_float(row["match_score"]),
            "score_breakdown": row["score_breakdown"] or {},
            "match_reason": row["match_reason"],
            "status": row["status"],
            "resume_status": row["resume_status"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

    return {"matches": output, "total": total_count}


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