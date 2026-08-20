from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from models import User, Consultant, Requirement, JobMatch, ConsultantExperience
from database import get_db
from auth import get_current_user
from phase4 import score_match
import re

router = APIRouter()

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

    new_matches = 0
    from phase4 import validate_match, _effective_role_text, _compute_requirement_skills
    # PERFORMANCE FIX: compute the per-requirement values ONCE here, outside
    # the consultant loop, instead of every consultant re-running the same
    # regex cleanup + skill extraction on the exact same requirement inside
    # validate_match()/score_match(). This is what was actually causing the
    # "Run Matching Engine" timeout after the Bug 2 fix: roleless
    # consultants used to fail validate_match's gate almost instantly and
    # never reach score_match at all — now they correctly pass the gate and
    # run the full scoring path, so the same redundant per-consultant work
    # that was always here got hit far more often on datasets with a lot of
    # consultants missing preferred_roles. Precomputing removes that
    # redundancy regardless of how many consultants pass the gate.
    effective_role = _effective_role_text(requirement)
    requirement_skills = _compute_requirement_skills(requirement)

    for cons in consultants:
        if (requirement.id, cons.id) in existing_pairs:
            continue
            
        experiences = experiences_by_consultant.get(cons.id, [])
        
        # STRICT VALIDATION GATE
        if not validate_match(requirement, cons, experiences, effective_role=effective_role):
            continue

        result = score_match(
            requirement, cons, experiences,
            requirement_skills=requirement_skills, effective_role=effective_role,
        )
        
        score = result["total"]
        if score > 0:  # Matches are already strictly validated, so just ensure it's > 0 or whatever minimum
            breakdown = result["score_breakdown"]
            flat_info = {
                "title": breakdown["role"]["weighted"],
                "skill": breakdown["skill"]["weighted"],
                "location": breakdown["location"]["weighted"],
                "experience": breakdown["experience"]["weighted"],
                "employment":breakdown["employment"]["weighted"],
                "auth": breakdown["auth"]["weighted"]
            }
            new_match = JobMatch(
                requirement_id=requirement.id,
                consultant_id=cons.id,
                match_score=score,
                matching_info=flat_info,
                match_reasoning=result["match_reason"],
                status="PENDING",
            )
            db.add(new_match)
            existing_pairs.add((requirement.id, cons.id))
            new_matches += 1

    return new_matches


@router.post("/run")
async def run_matching_engine(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Run the matching engine to find matches between active Requirements and active Consultants.
    """
    if current_user.role not in ["ADMIN", "RECRUITER"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    reqs_res = await db.execute(
        select(Requirement).where(Requirement.status.notin_(["CLOSED", "REJECTED"]))
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

    if not consultants or not requirements:
        return {"success": True, "new_matches": 0}

    # Batch query ALL experiences for ALL active consultants
    cons_ids = [c.id for c in consultants]
    exp_res = await db.execute(
        select(ConsultantExperience).where(ConsultantExperience.consultant_id.in_(cons_ids))
    )
    experiences_by_consultant = {}
    for exp in exp_res.scalars().all():
        experiences_by_consultant.setdefault(exp.consultant_id, []).append(exp)

    new_matches = 0
    try:
        for req in requirements:
            new_matches += await run_matching_for_requirement(
                db, req, consultants, existing_pairs,
                experiences_by_consultant=experiences_by_consultant
            )
    except Exception as e:
        print(f"[JobMatch] Batch matching failed: {e}")
        from error_logger import log_db_error
        await log_db_error(
            stage="matching_batch",
            error=e,
        )

    await db.commit()
    return {"success": True, "new_matches": new_matches}

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
    valid_statuses = {"PENDING", "APPLIED", "REJECTED"}
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
            "matching_info": row["matching_info"],
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