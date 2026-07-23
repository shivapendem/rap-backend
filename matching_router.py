from fastapi import APIRouter, Depends, HTTPException
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

async def run_matching_for_requirement(db: AsyncSession, req: Requirement) -> int:
    if not SKLEARN_AVAILABLE:
        print("[JobMatch] scikit-learn is not available. Skipping matching.")
        return 0

    new_matches = 0
    
    # Construct Requirement Document
    req_text = f"{req.job_title or ''} {req.skills or ''} {req.job_description or ''} {req.role or ''}"
    if not req_text.strip():
        return 0

    # Fetch active consultants
    cons_res = await db.execute(select(Consultant).where(Consultant.status == "ACTIVE"))
    consultants = cons_res.scalars().all()
    
    if not consultants:
        return 0

    # Construct Consultant Documents
    cons_docs = []
    cons_ids = []
    for cons in consultants:
        cons_text = f"{cons.primary_skills or ''} {cons.secondary_skills or ''} {cons.preferred_roles or ''} {cons.base_resume_text or ''}"
        cons_docs.append(cons_text)
        cons_ids.append(cons.id)
        
    # TF-IDF Vectorization
    vectorizer = TfidfVectorizer(stop_words='english', lowercase=True)
    try:
        # Fit on all documents (requirement + all consultants) to get a shared vocabulary
        all_docs = [req_text] + cons_docs
        tfidf_matrix = vectorizer.fit_transform(all_docs)
        
        req_vector = tfidf_matrix[0:1]
        cons_vectors = tfidf_matrix[1:]
        
        # Calculate Cosine Similarity
        cosine_sim = cosine_similarity(req_vector, cons_vectors)[0]
        
        feature_names = vectorizer.get_feature_names_out()
        
    except Exception as e:
        print(f"[JobMatch] TF-IDF vectorization failed: {e}")
        return 0

    for idx, cons_id in enumerate(cons_ids):
        score = float(cosine_sim[idx]) * 100
        
        if score > 15.0: # 15% similarity threshold for TF-IDF
            # Check if match already exists
            existing_res = await db.execute(
                select(JobMatch).where(
                    JobMatch.requirement_id == req.id,
                    JobMatch.consultant_id == cons_id
                )
            )
            if not existing_res.scalars().first():
                # Extract top overlapping terms for reasoning
                req_arr = req_vector.toarray()[0]
                cons_arr = cons_vectors[idx].toarray()[0]
                
                # Element-wise minimum gives the intersection of weights
                intersection_weights = np.minimum(req_arr, cons_arr)
                top_indices = intersection_weights.argsort()[-5:][::-1] # Top 5
                
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

    # Fetch active requirements
    reqs_res = await db.execute(select(Requirement).where(Requirement.status == "OPEN"))
    requirements = reqs_res.scalars().all()

    new_matches = 0
    for req in requirements:
        matches_found = await run_matching_for_requirement(db, req)
        new_matches += matches_found

    await db.commit()
    return {"success": True, "new_matches": new_matches}

@router.get("/pending")
async def get_pending_matches(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get all pending job matches for the current user's view.
    """
    query = select(JobMatch).where(JobMatch.status == "PENDING")
    
    if current_user.role == "CONSULTANT":
        cons_res = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        cons = cons_res.scalars().first()
        if not cons:
            return {"matches": []}
        query = query.where(JobMatch.consultant_id == cons.id)
    # For recruiter, we could filter by assigned consultants, but for MVP let's return all or just assigned
    
    result = await db.execute(query)
    matches = result.scalars().all()
    
    # We need to return enriched data
    output = []
    for match in matches:
        req_res = await db.execute(select(Requirement).where(Requirement.id == match.requirement_id))
        req = req_res.scalars().first()
        
        cons_res = await db.execute(select(Consultant).where(Consultant.id == match.consultant_id))
        cons = cons_res.scalars().first()
        
        if req and cons:
            output.append({
                "id": match.id,
                "requirement_id": req.id,
                "requirement_title": req.job_title,
                "requirement_company": req.client_name or req.vendor_name,
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
