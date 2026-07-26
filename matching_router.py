from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
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
    """
    query = select(JobMatch)
    
    if status:
        query = query.where(JobMatch.status == status.upper())
    else:
        query = query.where(JobMatch.status == "PENDING")

    if consultant_id:
        c_ids = [int(cid.strip()) for cid in consultant_id.split(',') if cid.strip().isdigit()]
        if c_ids:
            query = query.where(JobMatch.consultant_id.in_(c_ids))

    if current_user.role == "CONSULTANT":
        cons_res = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        cons = cons_res.scalars().first()
        if not cons:
            return {"matches": []}
        query = query.where(JobMatch.consultant_id == cons.id)
    elif current_user.role == "RECRUITER":
        # BUG FIX: this branch didn't exist — recruiters fell through with
        # no filter at all and saw every consultant's pending matches
        # system-wide, not just their own assigned ones (this file's own
        # comment already flagged it as an MVP gap). Same scoping already
        # applied to the Applications tracker for recruiters — apply it
        # here too so "Pending Applications" only shows what's actually
        # theirs to act on.
        from models import RecruiterConsultant
        assigned_res = await db.execute(
            select(RecruiterConsultant.consultant_id).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.is_active == True,
            )
        )
        assigned_ids = [row[0] for row in assigned_res.all()]
        query = query.where(JobMatch.consultant_id.in_(assigned_ids))
    
    # Limit to top 200 most recent pending matches to prevent memory bloat/slow rendering
    query = query.order_by(JobMatch.created_at.desc()).limit(200)
    result = await db.execute(query)
    matches = result.scalars().all()

    # BUG FIX: this used to run two separate SELECTs per match (one for
    # its Requirement, one for its Consultant) — an N+1 query pattern.
    # With the matching-engine status filter fixed elsewhere in this file,
    # a single "Run Engine" click can now legitimately produce hundreds of
    # matches across every open requirement, and the admin view (which
    # sees every match system-wide, unfiltered) hits the worst case. That
    # turned into hundreds of sequential round-trips per request, which is
    # exactly what was timing out / hanging as "Loading matches..." never
    # resolving. Batch both lookups into two queries total, regardless of
    # how many matches there are.
    if not matches:
        return {"matches": []}

    req_ids = {m.requirement_id for m in matches}
    cons_ids = {m.consultant_id for m in matches}

    reqs_by_id = {
        r.id: r for r in (await db.execute(
            select(Requirement).where(Requirement.id.in_(req_ids))
        )).scalars().all()
    }
    cons_by_id = {
        c.id: c for c in (await db.execute(
            select(Consultant).where(Consultant.id.in_(cons_ids))
        )).scalars().all()
    }

    output = []
    for match in matches:
        req = reqs_by_id.get(match.requirement_id)
        cons = cons_by_id.get(match.consultant_id)

        if req and cons:
            # BUG FIX: was req.job_title (doesn't exist — real field is
            # `role`) and req.client_name or req.vendor_name (real fields
            # are `client` and `vendor`). Would have crashed this endpoint
            # with AttributeError the moment any real JobMatch row existed.
            output.append({
                "id": match.id,
                "requirement_id": req.id,
                "requirement_title": req.role,
                "requirement_company": req.client or req.vendor,
                "consultant_id": cons.id,
                "consultant_name": cons.full_name,
                "consultant_email": cons.email,
                "match_score": match.match_score,
                "match_reasoning": match.match_reasoning,
                "status": match.status,
                "created_at": match.created_at
            })

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