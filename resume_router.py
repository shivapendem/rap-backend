import os
import uuid
import math
from typing import Optional, List
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Body
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func

from database import get_db
from models import User, Resume, ConsultantExperience
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
    ats_score: Optional[int] = None
    status: str
    download_count: int
    last_downloaded: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    
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
    # Fetch consultant experiences if provided
    experiences_text = ""
    if request.experience_ids:
        exp_results = await db.execute(select(ConsultantExperience).where(ConsultantExperience.id.in_(request.experience_ids)))
        experiences = exp_results.scalars().all()
        for exp in experiences:
            experiences_text += f"{exp.role} at {exp.company} ({exp.start_date} - {exp.end_date}): {exp.description}\n"

    # Use resume_info if available, else build a basic profile from db
    resume_info = current_user.resume_info or {
        "full_name": current_user.email.split('@')[0],
        "email": current_user.email,
        "experience": [{"bullets": [experiences_text]}] if experiences_text else []
    }

    try:
        generated_data, rate_limits = generate_tailored_resume(resume_info, request.job_description or "General Role")
        if rate_limits:
            await save_claude_rate_limits(db, rate_limits)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Generation failed: {e}")
    
    new_resume = Resume(
        user_id=current_user.id,
        title=request.title,
        target_role=request.target_role,
        job_description=request.job_description,
        data=generated_data,
        status='draft' if request.draft else 'generating',
        ats_score=85 # Dummy score
    )
    
    db.add(new_resume)
    await db.commit()
    await db.refresh(new_resume)
    
    if request.draft:
        return new_resume

    # Generate DOCX and PDF using phase6 logic
    from phase6 import _generate_docx, _convert_to_pdf
    from pathlib import Path
    
    resume_dir = Path("/tmp/resumes") / str(current_user.id) / str(new_resume.id)
    resume_dir.mkdir(parents=True, exist_ok=True)
    
    docx_path = resume_dir / "resume.docx"
    pdf_path = resume_dir / "resume.pdf"
    
    try:
        _generate_docx(generated_data, docx_path)
        pdf_ok = _convert_to_pdf(docx_path, pdf_path)
        
        if pdf_ok:
            s3_key = f"users/{current_user.id}/resumes/{new_resume.id}/resume.pdf"
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
    result = await db.execute(select(Resume).where(Resume.id == resume_id, Resume.user_id == current_user.id))
    resume = result.scalar_one_or_none()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")
        
    resume.data = request.data
    resume.status = 'generating'
    await db.commit()
    await db.refresh(resume)
    
    from phase6 import _generate_docx, _convert_to_pdf
    from pathlib import Path
    
    resume_dir = Path("/tmp/resumes") / str(current_user.id) / str(resume.id)
    resume_dir.mkdir(parents=True, exist_ok=True)
    
    docx_path = resume_dir / "resume.docx"
    pdf_path = resume_dir / "resume.pdf"
    
    try:
        _generate_docx(resume.data, docx_path)
        pdf_ok = _convert_to_pdf(docx_path, pdf_path)
        
        if pdf_ok:
            s3_key = f"users/{current_user.id}/resumes/{resume.id}/resume.pdf"
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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    query = select(Resume).where(Resume.user_id == current_user.id)
    
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()
    
    query = query.order_by(Resume.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    resumes = (await db.execute(query)).scalars().all()
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0
    
    return PaginatedResumes(
        data=resumes,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )

@router.get("/{id}", response_model=ResumeResponse)
async def get_resume(
    id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Resume).where(Resume.id == id, Resume.user_id == current_user.id))
    resume = result.scalars().first()
    
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
    result = await db.execute(select(Resume).where(Resume.id == id, Resume.user_id == current_user.id))
    resume = result.scalars().first()
    
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
    result = await db.execute(select(Resume).where(Resume.id == id, Resume.user_id == current_user.id))
    resume = result.scalars().first()
    
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
    result = await db.execute(select(Resume).where(Resume.id == id, Resume.user_id == current_user.id))
    resume = result.scalars().first()
    
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
