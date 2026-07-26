from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from models import User, Consultant, Requirement, JobMatch
from database import get_db
from auth import get_current_user
import re

router = APIRouter()

import numpy as np
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

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

    cons_res = await db.execute(select(Consultant).where(Consultant.status == "ACTIVE"))
    consultants = cons_res.scalars().all()

    existing_res = await db.execute(select(JobMatch.requirement_id, JobMatch.consultant_id))
    existing_pairs = {(row[0], row[1]) for row in existing_res.all()}

    if not consultants or not requirements:
        return {"success": True, "new_matches": 0}

    # Prepare consultant documents
    cons_docs = []
    cons_ids = []
    for cons in consultants:
        cons_text = f"{cons.primary_skills or ''} {cons.secondary_skills or ''} {cons.preferred_roles or ''} {cons.base_resume_text or ''}"
        cons_docs.append(cons_text)
        cons_ids.append(cons.id)

    new_matches = 0
    if SKLEARN_AVAILABLE:
        try:
            # Fit TF-IDF on consultants ONCE for the entire run
            vectorizer = TfidfVectorizer(stop_words='english', lowercase=True)
            cons_matrix = vectorizer.fit_transform(cons_docs)
            feature_names = vectorizer.get_feature_names_out()

            for req in requirements:
                req_skills = ""
                if isinstance(req.parsed_fields, dict):
                    skills_list = req.parsed_fields.get("skills")
                    if isinstance(skills_list, list):
                        req_skills = " ".join(str(s) for s in skills_list)
                req_text = f"{req.role or ''} {req_skills} {req.job_description or ''}"
                if not req_text.strip():
                    continue

                # Transform requirement using the pre-fitted vocabulary
                req_vector = vectorizer.transform([req_text])
                
                # Calculate cosine similarity against all consultants at once
                cosine_sim = cosine_similarity(req_vector, cons_matrix)[0]

                for idx, cons_id in enumerate(cons_ids):
                    score = float(cosine_sim[idx]) * 100
                    if score > 15.0:
                        if (req.id, cons_id) in existing_pairs:
                            continue
                        
                        req_arr = req_vector.toarray()[0]
                        cons_arr = cons_matrix[idx].toarray()[0]
                        intersection_weights = np.minimum(req_arr, cons_arr)
                        top_indices = intersection_weights.argsort()[-5:][::-1]
                        top_terms = [feature_names[i] for i in top_indices if intersection_weights[i] > 0]
                        reasoning = f"Strong semantic match ({score:.1f}%). Key overlapping features: {', '.join(top_terms)}"
                        
                        new_match = JobMatch(
                            requirement_id=req.id,
                            consultant_id=cons_id,
                            match_score=score,
                            match_reasoning=reasoning,
                            status="PENDING"
                        )
                        db.add(new_match)
                        existing_pairs.add((req.id, cons_id))
                        new_matches += 1

        except Exception as e:
            print(f"[JobMatch] TF-IDF batch vectorization failed: {e}")

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
    target_status = status.upper() if status else "PENDING"

    stmt = (
        select(
            JobMatch.id,
            JobMatch.requirement_id,
            Requirement.role.label("requirement_title"),
            func.coalesce(Requirement.client, Requirement.vendor).label("requirement_company"),
            JobMatch.consultant_id,
            Consultant.full_name.label("consultant_name"),
            Consultant.email.label("consultant_email"),
            JobMatch.match_score,
            JobMatch.match_reasoning,
            JobMatch.status,
            JobMatch.created_at
        )
        .join(Requirement, JobMatch.requirement_id == Requirement.id)
        .join(Consultant, JobMatch.consultant_id == Consultant.id)
        .where(JobMatch.status == target_status)
    )

    if consultant_id:
        c_ids = [int(cid.strip()) for cid in consultant_id.split(',') if cid.strip().isdigit()]
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

    output = [
        {
            "id": row["id"],
            "requirement_id": row["requirement_id"],
            "requirement_title": row["requirement_title"],
            "requirement_company": row["requirement_company"],
            "consultant_id": row["consultant_id"],
            "consultant_name": row["consultant_name"],
            "consultant_email": row["consultant_email"],
            "match_score": float(row["match_score"]) if row["match_score"] is not None else None,
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
