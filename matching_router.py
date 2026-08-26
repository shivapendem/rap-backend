from typing import Optional
from datetime import datetime, timezone, timedelta
import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from models import User, Consultant, Requirement, JobMatch, ConsultantExperience
from database import get_db, AsyncSessionLocal
from auth import get_current_user
from phase4 import score_match
import re

router = APIRouter()
logger = logging.getLogger(__name__)

# BUG FIX ("Run Engine" timing out even at a raised client timeout): a
# single HTTP request/response cycle is fundamentally the wrong shape for
# this — it scores every open requirement against every active
# consultant, which only gets larger as the dataset grows, so any fixed
# timeout (30s, then 300s) is just a number that eventually gets crossed
# again. Rather than guess at a number big enough, the run now happens
# in a background task with its own DB session — the HTTP response
# returns immediately, and the frontend polls /run/status for progress
# instead of holding one long-lived connection open. No new
# infrastructure needed (no Celery/Redis — matches the existing
# "substitutes for a background worker" pattern already used elsewhere
# in this codebase, e.g. phase3.py's consultant-profile-update
# auto-rematch). In-memory state is fine for this single-process,
# admin-triggered, non-critical-path operation — it doesn't need to
# survive a restart, and only one run is ever in flight at a time.
_matching_run_state: dict = {
    "status": "idle",  # idle | running | completed | failed
    "started_at": None,
    "finished_at": None,
    "total_requirements": 0,
    "processed_requirements": 0,
    "new_matches": 0,
    "error": None,
}

import numpy as np
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


# ---------------------------------------------------------------------------
# BUG FIX: requirements_sync.py's auto-match-on-new-requirement path calls
# `run_matching_for_requirement(db, req_obj, consultants, existing_pairs)`
# after every newly-synced Gmail requirement is saved — but that function
# never existed here. Every auto-match attempt failed with
# "cannot import name 'run_matching_for_requirement' from 'matching_router'"
# and was silently swallowed by requirements_sync.py's own try/except (it
# logs and moves on rather than losing the requirement save), so new
# requirements kept getting created successfully but NEVER got JobMatch
# rows — "Pending Applications" simply never filled in for anything synced
# after this regressed.
#
# This extracts the single-requirement TF-IDF matching logic that used to
# live only inline inside run_matching_engine's loop into its own function,
# so both the bulk /run endpoint below and requirements_sync.py's
# per-requirement auto-match call the exact same matching logic instead of
# keeping two copies in sync by hand.
#
# Does NOT commit — callers own the transaction boundary:
#   - requirements_sync.py commits once right after this call, per synced email.
#   - run_matching_engine (below) commits once after its whole batch loop.
#
# Accepts an optional pre-fitted vectorizer/matrix/feature_names/cons_ids
# so run_matching_engine's bulk loop can fit TF-IDF ONCE across all
# consultants and reuse it for every requirement (as it already did before
# this refactor) instead of re-fitting per requirement, which is what
# actually caused the N+1-style timeout mentioned below. When called with
# just (db, requirement, consultants, existing_pairs) — as
# requirements_sync.py does, once per newly-synced requirement — it fits
# its own vectorizer from scratch, which is fine at that call frequency.
# ---------------------------------------------------------------------------
async def run_matching_for_requirement(
    db: AsyncSession,
    requirement: Requirement,
    consultants: list,
    existing_pairs: set,
    *,
    experiences_by_consultant: dict = None,
) -> int:
    """
    Compute and persist JobMatch rows for ONE requirement against the given
    consultant roster using the robust Phase 4 scoring engine.
    Returns the number of new matches created.

    BUG FIX ("Run Engine doesn't clear out bad matches"): this used to skip
    a consultant outright the moment (requirement_id, consultant_id) was
    already in existing_pairs — meaning any JobMatch row created before a
    scoring-logic fix (the validate_match() truthy-dict gate bug, the
    extract_skills() substring bug, etc.) was PERMANENTLY stuck: "Run
    Engine" would never re-check it, never update it, never remove it,
    no matter how many times you ran it or how much the underlying logic
    changed. Pipeline A (phase4.py's match_requirement()) already
    re-validates and cleans up its own stale rows on every run; this
    brings Pipeline B in line with that. Existing rows are now
    re-validated every run too — marked status="INVALIDATED" (never
    deleted — match history is kept, just dropped out of the active
    Pending list) if they no longer qualify, refreshed with current
    scores if they do (and flipped back to PENDING if they were
    previously invalidated but now pass again) — with one exception: a
    row a human has already acted on (status APPLIED or REJECTED) is
    left untouched, since that's a real decision, not something the
    matching engine owns anymore.
    """
    if not consultants:
        return 0

    if experiences_by_consultant is None:
        # If not passed in (e.g. from single requirement sync), fetch locally
        cons_ids = [c.id for c in consultants]
        exp_res = await db.execute(
            select(ConsultantExperience).where(ConsultantExperience.consultant_id.in_(cons_ids))
        )
        experiences_by_consultant = {}
        for exp in exp_res.scalars().all():
            experiences_by_consultant.setdefault(exp.consultant_id, []).append(exp)

    # Batch-fetch existing JobMatch rows for THIS requirement, keyed by
    # consultant_id, so stale/no-longer-qualifying rows can be found and
    # removed instead of just being silently skipped forever.
    cons_ids_all = [c.id for c in consultants]
    existing_result = await db.execute(
        select(JobMatch).where(
            JobMatch.requirement_id == requirement.id,
            JobMatch.consultant_id.in_(cons_ids_all),
        )
    )
    existing_by_consultant: dict = {m.consultant_id: m for m in existing_result.scalars().all()}

    new_matches = 0

    # PERFORMANCE: requirement_skills is identical for every consultant
    # scored against this one requirement — compute it once here instead
    # of inside the loop below (same optimization already applied to
    # phase4.py's match_requirement()).
    from phase4 import validate_match, _requirement_skills, MATCHING_LOGIC_VERSION, MATCH_THRESHOLD
    requirement_skills = _requirement_skills(requirement)

    for cons in consultants:
        experiences = experiences_by_consultant.get(cons.id, [])
        existing = existing_by_consultant.get(cons.id)

        # Never touch a row a human already acted on — that's a real
        # decision, not the matching engine's to revise.
        if existing and existing.status in ("APPLIED", "REJECTED"):
            existing_pairs.add((requirement.id, cons.id))
            continue

        # PERFORMANCE (BUG FIX: "Run Engine" timing out at 300s): making
        # this function re-validate every EXISTING row on every run —
        # instead of skipping it outright via existing_pairs the way it
        # used to — was the right fix for stale/bad rows never getting
        # caught, but it meant re-scoring the ENTIRE existing dataset on
        # every single click, every time, regardless of whether anything
        # had actually changed. With thousands of requirements now in the
        # system, that's tens of thousands of full re-validations per
        # click. Skip the recomputation for a row already checked under
        # the CURRENT matching logic (from phase4.py's
        # MATCHING_LOGIC_VERSION, stored in matching_info — no schema
        # migration needed) — only a row from before a logic change still
        # pays the full re-check cost, restoring the old fast-skip
        # performance for the common case where nothing has changed.
        if existing and existing.matching_info and existing.matching_info.get("_version") == MATCHING_LOGIC_VERSION:
            existing_pairs.add((requirement.id, cons.id))
            continue

        # STRICT VALIDATION GATE
        # BUG FIX: validate_match() returns a structured dict now
        # ({"eligible": bool, "tier": ..., "stage_failed": ..., ...}), not
        # a plain bool — a dict is always truthy in Python, even
        # {"eligible": False, ...}, so "if not validate_match(...)" was
        # ALWAYS False and this gate silently rejected NOBODY, letting
        # every consultant straight through to scoring regardless of
        # role/employment/work-auth/experience/location eligibility. This
        # is the exact pipeline that feeds Pending Applications, so this
        # one line was the reason a pure-Salesforce consultant could show
        # up matched against a "Full Stack Python Engineer" posting.
        validation = validate_match(requirement, cons, experiences, requirement_skills=requirement_skills)
        if not validation["eligible"]:
            # Match history is mandatory — never delete a row, mark it
            # INVALIDATED instead so it drops out of the active Pending
            # list (get_pending_matches defaults to status=PENDING) while
            # the row and its original reasoning stay in the table.
            if existing and existing.status != "INVALIDATED":
                existing.status = "INVALIDATED"
                existing.match_reasoning = (
                    f"No longer eligible — failed at stage '{validation['stage_failed']}': {validation['reason']}"
                )
                await db.flush()
            continue

        result = score_match(requirement, cons, experiences, requirement_skills=requirement_skills)

        score = result["total"]
        if score > 0:  # Matches are already strictly validated, so just ensure it's > 0 or whatever minimum
            breakdown = result["score_breakdown"]
            flat_info = {
                "title": breakdown["role"]["weighted"],
                "skill": breakdown["skill"]["weighted"],
                "location": breakdown["location"]["weighted"],
                "experience": breakdown["experience"]["weighted"],
                "employment":breakdown["employment"]["weighted"],
                "auth": breakdown["auth"]["weighted"],
                "parsing_model": requirement.parsed_fields.get("parsing_model", "Regex Parser") if requirement.parsed_fields else "Regex Parser",
                "parsing_log": requirement.parsed_fields.get("parsing_log", []) if requirement.parsed_fields else [],
                "_version": MATCHING_LOGIC_VERSION,
            }
            # BUG FIX (NEAR_MISS tagging missing on this pipeline):
            # validation["tier"] was captured above but never read — every
            # eligible match got hardcoded "PENDING" regardless of whether
            # the role match was a confident 85% or a marginal 12% that
            # only survived because the rest of the blended score carried
            # it. Same tier logic Pipeline A (phase4.py's
            # match_requirement()) already applies: NEAR_MISS_CANDIDATE
            # (a soft 10-69% role match) only actually becomes a NEAR_MISS
            # row if the final blended score ALSO misses threshold — if
            # skills/experience/location compensated for the weak role
            # match, it's a legitimate normal pass instead.
            if validation["tier"] == "NEAR_MISS_CANDIDATE" and score < MATCH_THRESHOLD:
                new_status = "NEAR_MISS"
            else:
                new_status = "PENDING"
            if existing:
                existing.match_score = score
                existing.matching_info = flat_info
                existing.match_reasoning = result["match_reason"]
                # A row that was previously INVALIDATED and now qualifies
                # again (requirement or consultant data changed) comes
                # back with a fresh status — PENDING or NEAR_MISS,
                # whichever the current tier calls for. A row that was
                # already PENDING or NEAR_MISS just gets its scores
                # refreshed and status re-evaluated the same way.
                if existing.status in ("INVALIDATED", "PENDING", "NEAR_MISS"):
                    existing.status = new_status
            else:
                new_match = JobMatch(
                    requirement_id=requirement.id,
                    consultant_id=cons.id,
                    match_score=score,
                    matching_info=flat_info,
                    match_reasoning=result["match_reason"],
                    status=new_status,
                )
                db.add(new_match)
                new_matches += 1
            existing_pairs.add((requirement.id, cons.id))
        elif existing and existing.status != "INVALIDATED":
            # Score dropped to 0 on a rerun (e.g. requirement itself was
            # edited) — same INVALIDATED treatment as the ineligible
            # branch above, never a hard delete.
            existing.status = "INVALIDATED"
            existing.match_reasoning = "No longer eligible — final blended score dropped to 0"
            await db.flush()

    return new_matches



async def _run_matching_engine_background():
    """
    The actual matching work, run in the background with its own DB
    session (the request-scoped session from the triggering endpoint is
    gone by the time this executes, since that endpoint already
    returned). Updates _matching_run_state as it progresses so
    /run/status has something meaningful to report.
    """
    global _matching_run_state
    try:
        async with AsyncSessionLocal() as db:
            since = datetime.now(timezone.utc) - timedelta(days=1)
            reqs_res = await db.execute(
                select(Requirement).where(
                    Requirement.status.notin_(["CLOSED", "REJECTED"]),
                    Requirement.created_at >= since,
                )
            )
            requirements = reqs_res.scalars().all()

            cons_res = await db.execute(
                select(Consultant)
                .join(User, Consultant.user_id == User.id)
                .where(
                    Consultant.status == "ACTIVE",
                    User.role == "CONSULTANT",
                    User.is_authorized == True
                )
            )
            consultants = cons_res.scalars().all()

            existing_res = await db.execute(select(JobMatch.requirement_id, JobMatch.consultant_id))
            existing_pairs = {(row[0], row[1]) for row in existing_res.all()}

            _matching_run_state["total_requirements"] = len(requirements)

            if not consultants or not requirements:
                _matching_run_state.update({
                    "status": "completed", "new_matches": 0,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                return

            cons_ids = [c.id for c in consultants]
            exp_res = await db.execute(
                select(ConsultantExperience).where(ConsultantExperience.consultant_id.in_(cons_ids))
            )
            experiences_by_consultant = {}
            for exp in exp_res.scalars().all():
                experiences_by_consultant.setdefault(exp.consultant_id, []).append(exp)

            new_matches = 0
            for req in requirements:
                # BUG FIX: no per-requirement isolation here — one bad
                # requirement raised straight to the outer except, which
                # flipped the WHOLE run to "failed" and stopped every
                # remaining requirement, even the ones after it. Pipeline
                # A (phase4.py's match_all_requirements) already isolates
                # per-requirement with try/except+rollback+continue; this
                # brings Pipeline B's background run in line with that.
                try:
                    new_matches += await run_matching_for_requirement(
                        db, req, consultants, existing_pairs,
                        experiences_by_consultant=experiences_by_consultant
                    )
                except Exception as req_err:
                    await db.rollback()
                    logger.error(
                        "[JobMatch] Skipping requirement_id=%s (failed): %s",
                        req.id, req_err,
                    )
                    try:
                        from error_logger import log_db_error
                        await log_db_error(
                            stage="matching_batch_requirement",
                            error=req_err,
                            source_type="requirement",
                            source_id=req.id,
                        )
                    except Exception:
                        pass
                    continue
                _matching_run_state["processed_requirements"] += 1
                # Commit incrementally rather than one giant transaction
                # at the very end — a crash partway through still keeps
                # everything scored up to that point instead of losing
                # the whole run.
                if _matching_run_state["processed_requirements"] % 50 == 0:
                    await db.commit()

            await db.commit()
            _matching_run_state.update({
                "status": "completed",
                "new_matches": new_matches,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        logger.error("[JobMatch] Background matching run failed: %s", e)
        print(f"[JobMatch] Batch matching failed: {e}")
        try:
            from error_logger import log_db_error
            await log_db_error(stage="matching_batch", error=e)
        except Exception:
            pass
        _matching_run_state.update({
            "status": "failed",
            "error": str(e),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })


@router.post("/run")
async def run_matching_engine(
    current_user: User = Depends(get_current_user)
):
    """
    Triggers the matching engine and returns immediately — the actual
    work happens in the background (see _run_matching_engine_background
    above). Poll GET /run/status for progress/completion instead of
    waiting on this request.
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
    asyncio.create_task(_run_matching_engine_background())
    return {"success": True, "started": True, **_matching_run_state}


@router.get("/run/status")
async def get_matching_run_status(
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in ["ADMIN", "RECRUITER"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    return _matching_run_state

async def run_matching_for_consultant(db: AsyncSession, consultant_id: int) -> int:
    """
    Pipeline B equivalent of phase4.py's match_consultant() — matches ONE
    consultant against every still-open requirement for the JobMatch table.

    COVERAGE GAP FIX (not a matching-condition change): Pipeline B had no
    per-consultant entry point at all before this — only
    run_matching_for_requirement() (one requirement vs many consultants,
    triggered by a new synced email or a manual reparse) and
    run_matching_engine() (every requirement vs every consultant, the
    manual "Run Engine" button). phase3.py's consultant-profile-update
    background task only ever called Pipeline A's match_consultant() —
    so a consultant who updated their profile (including specifically to
    fix whatever was keeping them from matching something) would see
    Pipeline A's admin Requirements match count update immediately, but
    Pending Applications (this table) would never reflect it until either
    a brand-new requirement happened to sync in afterward, or an admin
    manually clicked "Run Engine". This reuses run_matching_for_requirement()
    exactly as written — no matching logic duplicated or changed here,
    only the loop direction (one consultant across many requirements
    instead of one requirement across many consultants).
    """
    cons_result = await db.execute(select(Consultant).where(Consultant.id == consultant_id))
    consultant = cons_result.scalars().first()
    if not consultant or consultant.status != "ACTIVE":
        return 0

    reqs_res = await db.execute(
        select(Requirement).where(Requirement.status.notin_(["CLOSED", "REJECTED"]))
    )
    requirements = reqs_res.scalars().all()
    if not requirements:
        return 0

    exp_res = await db.execute(
        select(ConsultantExperience).where(ConsultantExperience.consultant_id == consultant_id)
    )
    experiences_by_consultant = {consultant_id: exp_res.scalars().all()}

    existing_res = await db.execute(
        select(JobMatch.requirement_id, JobMatch.consultant_id).where(JobMatch.consultant_id == consultant_id)
    )
    existing_pairs = {(row[0], row[1]) for row in existing_res.all()}

    new_matches = 0
    for req in requirements:
        new_matches += await run_matching_for_requirement(
            db, req, [consultant], existing_pairs,
            experiences_by_consultant=experiences_by_consultant,
        )

    await db.commit()
    return new_matches


@router.get("/pending")
async def get_pending_matches(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status: Optional[str] = Query(None),
    consultant_id: Optional[str] = Query(None)
):
    """
    Get all pending job matches for the current user's view, with optional filters.
    Optimized to perform a single high-performance SQL JOIN query across JobMatch,
    Requirement, and Consultant tables.
    """
    # INVALIDATED added alongside the mark-instead-of-delete fix in
    # run_matching_for_requirement() above — a row the matching engine
    # determined no longer qualifies stays in the table (never deleted)
    # but only shows up here if explicitly filtered for; the default
    # (no status param) still resolves to PENDING same as before.
    # NEAR_MISS added alongside the tier-tagging fix, same pattern — a
    # soft role match that the final blended score also didn't clear
    # gets its own status, kept out of the default view so it doesn't
    # mix into the main Pending Applications list, but explicitly
    # filterable/viewable via its own tab/filter.
    valid_statuses = {"PENDING", "NEAR_MISS", "APPLIED", "REJECTED", "INVALIDATED"}
    target_status = status.upper().strip() if status and status.upper().strip() in valid_statuses else "PENDING"

    stmt = (
        select(
            JobMatch.id,
            JobMatch.requirement_id,
            Requirement.role.label("requirement_title"),
            func.coalesce(Requirement.client, Requirement.vendor).label("requirement_company"),
            Requirement.vendor_email.label("requirement_vendor_email"),
            JobMatch.consultant_id,
            Consultant.full_name.label("consultant_name"),
            Consultant.email.label("consultant_email"),
            JobMatch.match_score,
            JobMatch.matching_info,
            JobMatch.match_reasoning,
            JobMatch.status,
            JobMatch.created_at
        )
        .join(Requirement, JobMatch.requirement_id == Requirement.id)
        .join(Consultant, JobMatch.consultant_id == Consultant.id)
        .join(User, User.id == Consultant.user_id)
        .where(
            JobMatch.status == target_status,
            Consultant.status == "ACTIVE",
            User.is_authorized == True,
        )
    )

    if consultant_id:
        c_ids = [int(cid.strip()) for cid in consultant_id.split(',') if cid.strip().isdigit()][:100]
        if c_ids:
            stmt = stmt.where(JobMatch.consultant_id.in_(c_ids))

    if current_user.role == "CONSULTANT":
        cons_subq = select(Consultant.id).where(Consultant.user_id == current_user.id).scalar_subquery()
        stmt = stmt.where(JobMatch.consultant_id == cons_subq)
    elif current_user.role == "RECRUITER":
        from models import RecruiterConsultant
        assigned_subq = select(RecruiterConsultant.consultant_id).where(
            RecruiterConsultant.recruiter_id == current_user.id,
            RecruiterConsultant.is_active == True,
        ).scalar_subquery()
        stmt = stmt.where(JobMatch.consultant_id.in_(assigned_subq))

    stmt = stmt.order_by(JobMatch.created_at.desc()).limit(200)

    result = await db.execute(stmt)
    rows = result.mappings().all()

    import math
    def _safe_float(val):
        if val is None:
            return None
        try:
            f = float(val)
            return f if not (math.isnan(f) or math.isinf(f)) else None
        except (ValueError, TypeError):
            return None

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
            "matching_info": {
                **(row["matching_info"] or {}), 
                "parsing_model": (row["matching_info"] or {}).get("parsing_model", "Regex Parser"),
                "parsing_log": (row["matching_info"] or {}).get("parsing_log", [])
            },
            "match_reasoning": row["match_reasoning"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

    return {"matches": output}

@router.post("/{match_id}/apply")
async def mark_match_applied(
    match_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Mark a match as applied.
    """
    result = await db.execute(select(JobMatch).where(JobMatch.id == match_id))
    match = result.scalars().first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
        
    match.status = "APPLIED"
    await db.commit()
    return {"success": True}
@router.patch("/{match_id}/reject")
async def reject_match(
    match_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Reject a pending match, hiding it from the pending view.
    """
    query = select(JobMatch).where(JobMatch.id == match_id)
    result = await db.execute(query)
    match = result.scalars().first()

    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    if match.status != "PENDING":
        raise HTTPException(status_code=400, detail="Only pending matches can be rejected")

    match.status = "REJECTED"
    await db.commit()

    return {"success": True, "message": "Match rejected successfully"}