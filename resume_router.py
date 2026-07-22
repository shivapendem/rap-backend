import os
import uuid
import math
from typing import Optional, List
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Body
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, or_

from database import get_db
from models import User, Resume, ConsultantExperience, Consultant, RecruiterConsultant
from auth import get_current_user
from s3_service import upload_file_to_s3, generate_presigned_url, delete_file_from_s3
from claude_service import generate_tailored_resume
from phase8_ai_usage_service import save_claude_rate_limits

# You can import openai and use it if an API key is provided
# import openai

router = APIRouter(prefix="/api/resume", tags=["resume"])

class ResumeCreateRequest(BaseModel):
    title: str
    target_role: Optional[str] = None
    job_description: Optional[str] = None
    experience_ids: Optional[List[int]] = [] # IDs of ConsultantExperience to include
    draft: bool = False # If True, don't generate PDF yet
    user_id: Optional[int] = None # The candidate user_id to generate for

async def _get_resume_for_user(db: AsyncSession, resume_id: int, current_user: User):
    if current_user.role == "ADMIN":
        result = await db.execute(select(Resume).where(Resume.id == resume_id))
        return result.scalar_one_or_none()
    elif current_user.role == "RECRUITER":
        consultant_users_query = select(Consultant.user_id).where(
            or_(
                Consultant.sales_recruiter_user_id == current_user.id,
                Consultant.id.in_(
                    select(RecruiterConsultant.consultant_id).where(
                        RecruiterConsultant.recruiter_id == current_user.id
                    )
                )
            )
        )
        result = await db.execute(select(Resume).where(
            Resume.id == resume_id,
            or_(
                Resume.user_id == current_user.id,
                Resume.user_id.in_(consultant_users_query)
            )
        ))
        return result.scalar_one_or_none()
    else:
        result = await db.execute(select(Resume).where(Resume.id == resume_id, Resume.user_id == current_user.id))
        return result.scalar_one_or_none()

class ResumeUpdateRequest(BaseModel):
    title: Optional[str] = None
    target_role: Optional[str] = None
    job_description: Optional[str] = None
    data: Optional[dict] = None
    status: Optional[str] = None

class ResumeResponse(BaseModel):
    id: int
    user_id: int
    title: str
    target_role: Optional[str] = None
    job_description: Optional[str] = None
    data: dict
    s3_key: Optional[str] = None
    s3_url: Optional[str] = None
    ats_score: Optional[int] = None
    status: str
    download_count: int
    last_downloaded: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    is_base: bool = False
    
    class Config:
        from_attributes = True

class PaginatedResumes(BaseModel):
    data: List[ResumeResponse]
    total: int
    page: int
    page_size: int
    total_pages: int

@router.post("/generate", response_model=ResumeResponse)
async def generate_resume(
    request: ResumeCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    target_user_id = current_user.id
    target_user = current_user
    if request.user_id and current_user.role in ("ADMIN", "RECRUITER"):
        target_user_id = request.user_id
        target_user_result = await db.execute(select(User).where(User.id == target_user_id))
        target_user = target_user_result.scalar_one_or_none()
        if not target_user:
            raise HTTPException(status_code=404, detail="Target user not found")

    if target_user.role != "CONSULTANT":
        raise HTTPException(status_code=403, detail="Resumes can only be generated for consultants.")

    # Fetch consultant to get profile experiences
    consultant = None
    if target_user.role == "CONSULTANT":
        consultant_res = await db.execute(select(Consultant).where(Consultant.user_id == target_user.id))
        consultant = consultant_res.scalar_one_or_none()

    profile_experiences = []
    if consultant:
        exp_results = await db.execute(select(ConsultantExperience).where(ConsultantExperience.consultant_id == consultant.id).order_by(ConsultantExperience.sort_order))
        profile_experiences = exp_results.scalars().all()
    
    explicit_experiences = []
    if request.experience_ids:
        exp_results = await db.execute(select(ConsultantExperience).where(ConsultantExperience.id.in_(request.experience_ids)))
        explicit_experiences = exp_results.scalars().all()
    
    # Merge avoiding duplicates
    all_exps = {exp.id: exp for exp in profile_experiences}
    for exp in explicit_experiences:
        all_exps[exp.id] = exp
    
    merged_exps = list(all_exps.values())

    manual_exp_entries = []
    for exp in merged_exps:
        bullets = []
        if exp.responsibilities:
            bullets.extend([b.strip() for b in exp.responsibilities.split('\n') if b.strip()])
        if exp.achievements:
            bullets.extend([b.strip() for b in exp.achievements.split('\n') if b.strip()])
        
        tech_str = ", ".join(exp.technologies) if exp.technologies else ""
        if tech_str:
            bullets.append(f"Technologies: {tech_str}")

        start_str = exp.start_date.strftime("%b %Y") if exp.start_date else ""
        end_str = "Present" if exp.is_present else (exp.end_date.strftime("%b %Y") if exp.end_date else "")
        date_str = f"{start_str} - {end_str}".strip(" -")

        manual_exp_entries.append({
            "title": exp.role_title,
            "company": exp.client_name,
            "dates": date_str,
            "bullets": bullets
        })

    # Use resume_info if available, else build a basic profile from db
    import copy
    resume_info = copy.deepcopy(target_user.resume_info) if target_user.resume_info else {
        "full_name": target_user.full_name or target_user.email.split('@')[0],
        "email": target_user.email,
        "experience": []
    }

    if "experience" not in resume_info:
        resume_info["experience"] = []
    
    # Prepend profile experiences so they are processed as most relevant/recent
    resume_info["experience"] = manual_exp_entries + resume_info["experience"]

    try:
        generated_data, rate_limits = generate_tailored_resume(resume_info, request.job_description or "General Role")
        if rate_limits:
            await save_claude_rate_limits(db, rate_limits)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Generation failed: {e}")
    # Compute a real ATS score from the generated resume vs. the job description.
    # Reuses phase6's keyword scorer and phase3's skill detector. Left as None
    # (no badge shown) when the JD has no recognizable skills to match against.
    ats_value = None
    try:
        from phase6 import _ats_score
        from phase3 import _detect_skills
        jd_skills = list(dict.fromkeys(
            _detect_skills(request.job_description or "")
            + (generated_data.get("missing_skills") or [])
        ))
        if jd_skills:
            text_parts = [
                generated_data.get("summary", ""),
                " ".join(generated_data.get("skills", []) or []),
                request.target_role or "",
            ]
            for exp in generated_data.get("experience", []) or []:
                text_parts.append(exp.get("role", ""))
                text_parts.append(" ".join(exp.get("bullets", []) or []))
            resume_text = " ".join(text_parts)
            ats_total, *_ = _ats_score(jd_skills, resume_text, request.target_role or "")
            ats_value = int(round(ats_total))
    except Exception as e:
        print(f"ATS scoring failed, leaving score empty: {e}")
        ats_value = None
    new_resume = Resume(
        user_id=target_user_id,
        title=request.title,
        target_role=request.target_role,
        job_description=request.job_description,
        data=generated_data,
        status='draft' if request.draft else 'generating',
        ats_score=ats_value,
    )
    
    db.add(new_resume)
    await db.commit()
    await db.refresh(new_resume)
    
    if request.draft:
        return new_resume

    # Generate DOCX and PDF using phase6 logic
    from phase6 import _generate_docx, _convert_to_pdf
    from pathlib import Path
    
    resume_dir = Path("/tmp/resumes") / str(target_user_id) / str(new_resume.id)
    resume_dir.mkdir(parents=True, exist_ok=True)
    
    docx_path = resume_dir / "resume.docx"
    pdf_path = resume_dir / "resume.pdf"
    
    try:
        _generate_docx(generated_data, docx_path)
        pdf_ok = _convert_to_pdf(docx_path, pdf_path)
        
        if pdf_ok:
            s3_key = f"users/{target_user_id}/resumes/{new_resume.id}/resume.pdf"
            with open(pdf_path, "rb") as f:
                if upload_file_to_s3(f, s3_key, "application/pdf"):
                    new_resume.s3_key = s3_key
                    new_resume.status = 'completed'
                else:
                    new_resume.status = 'failed_upload'
        else:
            new_resume.status = 'failed_pdf_conversion'
    except Exception as e:
        new_resume.status = 'failed_generation'
        print(f"Resume generation failed: {e}")
        
    await db.commit()
    await db.refresh(new_resume)
    
    return new_resume

class FinalizeResumeRequest(BaseModel):
    data: dict

@router.post("/{resume_id}/finalize", response_model=ResumeResponse)
async def finalize_resume(
    resume_id: int,
    request: FinalizeResumeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    resume = await _get_resume_for_user(db, resume_id, current_user)
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
        
    resume.data = request.data
    resume.status = 'generating'
    await db.commit()
    await db.refresh(resume)
    
    from phase6 import _generate_docx, _convert_to_pdf
    from pathlib import Path
    
    resume_dir = Path("/tmp/resumes") / str(resume.user_id) / str(resume.id)
    resume_dir.mkdir(parents=True, exist_ok=True)
    
    docx_path = resume_dir / "resume.docx"
    pdf_path = resume_dir / "resume.pdf"
    
    try:
        _generate_docx(resume.data, docx_path)
        pdf_ok = _convert_to_pdf(docx_path, pdf_path)
        
        if pdf_ok:
            s3_key = f"users/{resume.user_id}/resumes/{resume.id}/resume.pdf"
            with open(pdf_path, "rb") as f:
                if upload_file_to_s3(f, s3_key, "application/pdf"):
                    resume.s3_key = s3_key
                    resume.status = 'completed'
                else:
                    resume.status = 'failed_upload'
        else:
            resume.status = 'failed_pdf_conversion'
    except Exception as e:
        resume.status = 'failed_generation'
        print(f"Resume generation failed: {e}")
        
    await db.commit()
    await db.refresh(resume)
    
    return resume

@router.post("/upload", response_model=ResumeResponse)
async def upload_resume(
    title: str = Form(...),
    target_role: str = Form(None),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
        
    s3_key = f"users/{current_user.id}/resumes/{uuid.uuid4()}/final.pdf"
    
    success = upload_file_to_s3(file.file, s3_key, content_type="application/pdf")
    if not success:
        raise HTTPException(status_code=500, detail="Failed to upload file to S3.")
        
    new_resume = Resume(
        user_id=current_user.id,
        title=title,
        target_role=target_role,
        s3_key=s3_key,
        status='completed'
    )
    
    db.add(new_resume)
    await db.commit()
    await db.refresh(new_resume)
    
    return new_resume

@router.get("/list", response_model=PaginatedResumes)
async def list_resumes(
    page: int = 1,
    page_size: int = 10,
    search: Optional[str] = None,
    user_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if current_user.role == "ADMIN":
        query = select(Resume)
    elif current_user.role == "RECRUITER":
        consultant_users_query = select(Consultant.user_id).where(
            or_(
                Consultant.sales_recruiter_user_id == current_user.id,
                Consultant.id.in_(
                    select(RecruiterConsultant.consultant_id).where(
                        RecruiterConsultant.recruiter_id == current_user.id
                    )
                )
            )
        )
        query = select(Resume).where(
            or_(
                Resume.user_id == current_user.id,
                Resume.user_id.in_(consultant_users_query)
            )
        )
    else:
        query = select(Resume).where(Resume.user_id == current_user.id)
        
    if user_id:
        # Additional safety to only allow filtering if they have access
        if current_user.role == "ADMIN" or current_user.role == "RECRUITER":
            query = query.where(Resume.user_id == user_id)
        
    if search:
        query = query.where(Resume.title.ilike(f"%{search}%"))
    
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()
    
    query = query.order_by(Resume.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    resumes = (await db.execute(query)).scalars().all()
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0
    
    # generate s3_url for each resume
    response_data = []
    for r in resumes:
        r_dict = {
            "id": r.id,
            "user_id": r.user_id,
            "title": r.title,
            "target_role": r.target_role,
            "job_description": r.job_description,
            "data": r.data or {},
            "s3_key": r.s3_key,
            "s3_url": generate_presigned_url(r.s3_key) if r.s3_key else None,
            "ats_score": r.ats_score,
            "status": r.status,
            "download_count": r.download_count,
            "last_downloaded": r.last_downloaded,
            "created_at": r.created_at,
            "updated_at": r.updated_at
        }
        response_data.append(ResumeResponse(**r_dict))

    # --- Pin the consultant's base resume (from their profile) at the top ---
    # It lives on the consultants table + Spaces, not the resumes table, so we surface it
    # here as a read-only entry instead of duplicating a row (single source of truth).
    if page == 1:
        base_target_user_id = None
        if current_user.role == "CONSULTANT":
            base_target_user_id = current_user.id
        elif user_id:  # admin/recruiter viewing a specific candidate
            base_target_user_id = user_id

        if base_target_user_id is not None and (not search or search.lower() in "base resume"):
            base_consultant = (await db.execute(
                select(Consultant).where(Consultant.user_id == base_target_user_id)
            )).scalar_one_or_none()

            if base_consultant and base_consultant.base_resume_file_path:
                base_ts = base_consultant.updated_at or datetime.now(timezone.utc)
                response_data.insert(0, ResumeResponse(
                    id=-1,
                    user_id=base_target_user_id,
                    title="Base Resume",
                    target_role="From profile",
                    job_description=None,
                    data={},
                    s3_key=None,
                    s3_url=generate_presigned_url(base_consultant.base_resume_file_path) or None,
                    ats_score=None,
                    status="base",
                    download_count=0,
                    last_downloaded=None,
                    created_at=base_ts,
                    updated_at=base_ts,
                    is_base=True,
                ))
                total += 1

    return PaginatedResumes(
        data=response_data,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )

@router.get("/consultants")
async def get_consultants_for_resumes(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    def map_user_consultant(u, c):
        return {
            "id": u.id, # Keep id for backward compatibility (maps to user_id)
            "user_id": u.id,
            "consultant_id": c.id if c else None,
            "name": u.full_name or u.email,
            "email": u.email,
            "skills": u.skills,
            "experience_years": u.experience_years or (c.total_experience_years if c else 0)
        }

    if current_user.role == "ADMIN":
        query = select(User, Consultant).outerjoin(Consultant, Consultant.user_id == User.id).where(User.role == "CONSULTANT")
        results = (await db.execute(query)).all()
        return [map_user_consultant(u, c) for u, c in results]
    elif current_user.role == "RECRUITER":
        consultant_users_query = select(Consultant.user_id).where(
            or_(
                Consultant.sales_recruiter_user_id == current_user.id,
                Consultant.id.in_(
                    select(RecruiterConsultant.consultant_id).where(
                        RecruiterConsultant.recruiter_id == current_user.id
                    )
                )
            )
        )
        query = select(User, Consultant).outerjoin(Consultant, Consultant.user_id == User.id).where(User.id.in_(consultant_users_query))
        results = (await db.execute(query)).all()
        return [map_user_consultant(u, c) for u, c in results]
    else:
        query = select(User, Consultant).outerjoin(Consultant, Consultant.user_id == User.id).where(User.id == current_user.id)
        results = (await db.execute(query)).all()
        return [map_user_consultant(u, c) for u, c in results]

@router.get("/{id}", response_model=ResumeResponse)
async def get_resume(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # BUG FIX: was owner-only (Resume.user_id == current_user.id) — same
    # class of bug as /download. Uses the shared role-scoped helper instead.
    resume = await _get_resume_for_user(db, id, current_user)
    
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
        
    return resume

@router.put("/{id}", response_model=ResumeResponse)
async def update_resume(
    id: int,
    request: ResumeUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # BUG FIX: was owner-only (Resume.user_id == current_user.id) — same
    # class of bug as /download. Uses the shared role-scoped helper instead.
    resume = await _get_resume_for_user(db, id, current_user)
    
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
        
    if request.title is not None:
        resume.title = request.title
    if request.target_role is not None:
        resume.target_role = request.target_role
    if request.job_description is not None:
        resume.job_description = request.job_description
    if request.data is not None:
        resume.data = request.data
    if request.status is not None:
        resume.status = request.status
        
    await db.commit()
    await db.refresh(resume)
    
    return resume

@router.delete("/{id}")
async def delete_resume(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # BUG FIX: was owner-only (Resume.user_id == current_user.id) — same
    # class of bug as /download. Uses the shared role-scoped helper instead.
    resume = await _get_resume_for_user(db, id, current_user)
    
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
        
    if resume.s3_key:
        delete_file_from_s3(resume.s3_key)
        
    await db.delete(resume)
    await db.commit()
    
    return {"success": True}

@router.get("/{id}/download")
async def download_resume(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # BUG FIX: was `select(Resume).where(Resume.id == id, Resume.user_id ==
    # current_user.id)` — only ever matched when the CALLER owned the resume.
    # /admin/apply/:reqId and /recruiter/apply/:reqId reuse this same page to
    # apply on behalf of a consultant, so current_user is the admin/recruiter,
    # not the consultant who owns the resume — this 404'd every single time
    # for them ("Preparing resume attachment..." -> "Failed to download and
    # attach resume"). Now uses the shared role-scoped helper (same one
    # get_resume/update_resume/delete_resume already use above) instead of
    # duplicating the ADMIN/RECRUITER/owner query logic inline: ADMIN sees
    # any resume, RECRUITER sees their own + assigned consultants', everyone
    # else only their own.
    resume = await _get_resume_for_user(db, id, current_user)
    
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
        
    if not resume.s3_key:
        raise HTTPException(status_code=400, detail="Resume does not have a generated PDF.")
        
    url = generate_presigned_url(resume.s3_key)
    if not url:
        raise HTTPException(status_code=500, detail="Failed to generate download link.")
        
    # Update download stats
    resume.download_count += 1
    resume.last_downloaded = datetime.now(timezone.utc)
    await db.commit()
    
    return {"url": url}

