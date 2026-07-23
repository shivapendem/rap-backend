# phase6.py
# ---------------------------------------------------------------------------
# Phase 6 — Resume Tailoring, ATS Scoring, Resume Storage
#
# Architecture: single flat file, same pattern as phase3.py / phase4.py.
# Imports get_current_user from auth.py — no circular dependency.
#
# Endpoints:
#   POST /api/consultant/requirements/{id}/generate-resume  → GenerateResumeResultDTO
#   GET  /api/consultant/requirements/{id}/resume           → ResumeDataDTO
#   GET  /api/consultant/requirements/{id}/resume/history   → ResumeHistoryDTO
#   GET  /api/consultant/requirements/{id}/resume/download/{type}  → FileResponse (PDF/DOCX)
#   GET  /api/recruiter/consultants/{id}/requirements/{req_id}/resume → ResumeDataDTO
#
# All response shapes exactly match frontend DTO contracts in:
#   features/consultant/resume/types/index.ts
#   types/consultant.ts
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from claude_service import generate_tailored_resume
from models import (
    Consultant,
    ConsultantExperience,
    GeneratedResume,
    Requirement,
    RequirementConsultantMatch,
    RecruiterConsultant,
    User,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")   # legacy - unused since Claude migration
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")  # legacy - unused since Claude migration
# Kept in sync with claude_service.generate_tailored_resume()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ATS_PASS_THRESHOLD = 80          # per Phase 6 doc — NOT configurable
MAX_GENERATION_ATTEMPTS = 3      # per frontend RegenerateDialog MAX_ATTEMPTS = 3
RESUME_UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads/resumes"))
BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")


# ---------------------------------------------------------------------------
# Helpers — reused patterns from phase3/phase4
# ---------------------------------------------------------------------------

def _require_role(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {list(roles)}",
        )


async def _get_consultant_for_user(db: AsyncSession, user: User) -> Consultant:
    result = await db.execute(select(Consultant).where(Consultant.user_id == user.id))
    consultant = result.scalars().first()
    if not consultant:
        raise HTTPException(status_code=404, detail="Consultant profile not found for this user")
    return consultant


async def _get_requirement_or_404(db: AsyncSession, requirement_id: int) -> Requirement:
    result = await db.execute(select(Requirement).where(Requirement.id == requirement_id))
    req = result.scalars().first()
    if not req:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return req


async def _get_match_or_404(
    db: AsyncSession, requirement_id: int, consultant_id: int
) -> RequirementConsultantMatch:
    result = await db.execute(
        select(RequirementConsultantMatch).where(
            RequirementConsultantMatch.requirement_id == requirement_id,
            RequirementConsultantMatch.consultant_id == consultant_id,
        )
    )
    match = result.scalars().first()
    if not match:
        raise HTTPException(
            status_code=404,
            detail="No match found. Consultant must be matched to this requirement first (Phase 4).",
        )
    return match


# ---------------------------------------------------------------------------
# Task 5 — Filename convention (verbatim from Phase 6 doc code example)
# ---------------------------------------------------------------------------

def _clean_filename_part(value: Any) -> str:
    if not value:
        return "unknown"
    value = str(value).lower().strip().replace("&", "and")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def _build_resume_filename(
    first_name: str,
    last_name: str,
    role: str,
    client_name: Optional[str],
    vendor_name: Optional[str],
    years_exp: Any,
) -> str:
    """Verbatim from Phase 6 doc Task 5 code example."""
    company = (
        client_name
        if client_name and client_name.upper() not in ("UNKNOWN", "N/A", "")
        else vendor_name
    )
    return (
        f"{_clean_filename_part(first_name)}_"
        f"{_clean_filename_part(last_name)}_"
        f"{_clean_filename_part(role)}_"
        f"{_clean_filename_part(company)}_"
        f"{_clean_filename_part(years_exp)}-years.pdf"
    )


# ---------------------------------------------------------------------------
# Task 3 — ATS Scoring Engine (verbatim from Phase 6 doc code example)
# ---------------------------------------------------------------------------

def _ats_score(
    jd_skills: List[str],
    resume_text: str,
    role: str,
) -> tuple[float, float, float, float, List[str], List[str]]:
    """
    Verbatim from Phase 6 doc Task 3 code example.
    Returns (total, keyword_score, role_score, format_score, matched, missing).
    """
    resume_lower = resume_text.lower()
    matched = [s for s in jd_skills if s.lower() in resume_lower]
    missing = [s for s in jd_skills if s.lower() not in resume_lower]
    keyword_score = len(matched) / max(len(jd_skills), 1) * 70
    role_score = 15.0 if role.lower() in resume_lower else 5.0
    format_score = 15.0
    total = round(min(100.0, keyword_score + role_score + format_score), 2)
    return (
        total,
        round(keyword_score, 2),
        round(role_score, 2),
        round(format_score, 2),
        matched,
        missing,
    )


# ---------------------------------------------------------------------------
# Task 2 — AI Resume Tailoring System Prompt (verbatim from Phase 6 doc)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a resume tailoring assistant. Use only the consultant base resume and structured experience provided.
Do not invent clients, projects, skills, certifications, titles, dates, or years of experience.
If the job description includes a skill not present in the consultant profile, mark it as missing instead of adding it.
Improve wording, reorder relevant skills, and add 4-6 truthful bullets based on existing experience only.
Return structured JSON.

Return exactly this JSON structure with no markdown, no code fences, no extra text:
{
  "name": "string",
  "email": "string",
  "phone": "string",
  "summary": "string",
  "skills": ["skill1", "skill2"],
  "missing_skills": ["skill_not_in_profile"],
  "experience": [
    {
      "client": "string",
      "role": "string",
      "start": "string",
      "end": "string",
      "location": "string",
      "bullets": ["bullet1", "bullet2", "bullet3", "bullet4"]
    }
  ],
  "generation_notes": "string"
}"""


async def _call_ai_tailoring(
    consultant: Consultant,
    experiences: List[ConsultantExperience],
    requirement: Requirement,
    matched_skills: List[str],
    missing_skills: List[str],
    db: Optional[AsyncSession] = None,
) -> dict:
    """
    Generate a tailored resume via Anthropic Claude (claude_service).

    MIGRATION NOTE: this previously called OpenAI/GPT-4o directly. The rest of
    the platform (resume_router -> claude_service) standardised on Claude and
    OPENAI_API_KEY was never provisioned in any environment, so this code path
    failed for every consultant while "My Resumes" worked. Same JSON contract,
    one provider, one key to manage.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("your_"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ANTHROPIC_API_KEY is not configured. Set it in .env to enable resume generation.",
        )

    # ── Structured profile ────────────────────────────────────────────────
    # Key names mirror claude_service's internal mock_fallback shape so its
    # offline fallback still yields sensible data instead of placeholders.
    experience_payload: List[Dict[str, Any]] = []
    for exp in sorted(experiences, key=lambda e: e.sort_order):
        raw = f"{exp.responsibilities or ''}\n{exp.achievements or ''}"
        bullets = [ln.strip(" -*\u2022\t") for ln in re.split(r"[\r\n]+", raw) if ln.strip(" -*\u2022\t")]
        experience_payload.append(
            {
                "company": exp.client_name or "",
                "role": exp.role_title or "",
                "start_date": exp.start_date.strftime("%b %Y") if exp.start_date else "",
                "end_date": "Present"
                if exp.is_present
                else (exp.end_date.strftime("%b %Y") if exp.end_date else ""),
                "technologies": exp.technologies or [],
                "bullets": bullets,
            }
        )

    primary = [s.strip() for s in (consultant.primary_skills or "").split(",") if s.strip()]
    secondary = [s.strip() for s in (consultant.secondary_skills or "").split(",") if s.strip()]

    resume_info: Dict[str, Any] = {
        "full_name": consultant.full_name or "",
        "email": consultant.email or "",
        "phone": consultant.phone or "",
        "total_experience_years": float(consultant.total_experience_years)
        if consultant.total_experience_years is not None
        else None,
        "work_authorization": consultant.work_authorization or "",
        "current_location": consultant.current_location or "",
        "tech_stack": {"expert": primary, "familiar": secondary},
        "base_resume_text": consultant.base_resume_text or "",
        "experience": experience_payload,
    }

    # ── Requirement context (preserves everything the old prompt carried) ──
    jd_context = f"""Role: {requirement.role}
Client: {requirement.client or 'Not specified'}
Location: {requirement.location or 'Not specified'}
Work Mode: {requirement.work_mode or 'Not specified'}
Employment Types: {', '.join(requirement.employment_types or []) or 'Not specified'}

JOB DESCRIPTION:
{requirement.job_description or 'No job description available.'}

MATCHED SKILLS (already in profile): {', '.join(matched_skills) or 'None'}
MISSING SKILLS (in JD, not in profile): {', '.join(missing_skills) or 'None'}"""

    # ── Call Claude off the event loop ────────────────────────────────────
    # generate_tailored_resume() is synchronous and blocks for the duration of
    # the HTTP call. Running it inline would freeze this uvicorn worker for
    # every other request, so it goes to a thread.
    try:
        resume_data, rate_limits = await asyncio.to_thread(
            generate_tailored_resume, resume_info, jd_context
        )
    except Exception as exc:  # noqa: BLE001 - surface any client/transport error
        logger.error("Claude resume generation failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"AI service error: {exc}")

    if not isinstance(resume_data, dict) or not resume_data.get("experience"):
        logger.error("Claude returned unusable payload: %r", resume_data)
        raise HTTPException(status_code=502, detail="AI returned malformed response. Please retry.")

    # claude_service swallows API errors and silently returns mock data.
    # Persisting that as a real resume would be worse than failing loudly.
    if "Mock generated" in (resume_data.get("generation_notes") or ""):
        logger.error(
            "Claude returned mock fallback (consultant_id=%s requirement_id=%s) - check ANTHROPIC_API_KEY",
            consultant.id, requirement.id,
        )
        raise HTTPException(
            status_code=502,
            detail="AI service is unavailable right now. Please retry in a moment.",
        )

    # Best-effort telemetry — powers the admin AI Usage screen. Never fatal.
    if db is not None and rate_limits:
        try:
            from phase8_ai_usage_service import save_claude_rate_limits

            await save_claude_rate_limits(db, rate_limits)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist Claude rate limits: %s", exc)

    return resume_data


def _validate_resume_output(resume_data: dict, consultant: Consultant) -> tuple[dict, str]:
    """
    Task 2: Reject skills not in consultant profile.
    Returns (validated_data, generation_notes).
    """
    notes = resume_data.get("generation_notes", "")
    profile_skills_lower = set(
        s.strip().lower()
        for s in (consultant.primary_skills or "").split(",") + (consultant.secondary_skills or "").split(",")
        if s.strip()
    )

    original_skills = resume_data.get("skills", [])
    validated_skills = []
    rejected_skills = []

    for skill in original_skills:
        if skill.strip().lower() in profile_skills_lower:
            validated_skills.append(skill)
        else:
            rejected_skills.append(skill)

    if rejected_skills:
        note = f"Rejected unsupported skills: {', '.join(rejected_skills)}."
        notes = f"{notes} {note}".strip()
        logger.warning(
            "Rejected %d invented skills for consultant_id=%s: %s",
            len(rejected_skills), consultant.id, rejected_skills,
        )

    resume_data["skills"] = validated_skills
    resume_data["generation_notes"] = notes
    return resume_data, notes


# ---------------------------------------------------------------------------
# Task 4 — DOCX Generation (verbatim from Phase 6 doc code example)
# ---------------------------------------------------------------------------

def _generate_docx(resume_data: dict, output_path: Path) -> None:
    """Verbatim from Phase 6 doc Task 4 code example."""
    from docx import Document

    doc = Document()
    doc.add_heading(resume_data.get("name", ""), 0)

    # Contact
    contact = f"{resume_data.get('email', '')} | {resume_data.get('phone', '')}"
    doc.add_paragraph(contact.strip(" |"))

    doc.add_heading("Professional Summary", level=1)
    doc.add_paragraph(resume_data.get("summary", ""))

    doc.add_heading("Technical Skills", level=1)
    skills = resume_data.get("skills", [])
    doc.add_paragraph(", ".join(skills) if skills else "")

    doc.add_heading("Professional Experience", level=1)
    for exp in resume_data.get("experience", []):
        title = f"{exp.get('role', '')} | {exp.get('client', '')} | {exp.get('start', '')} – {exp.get('end', '')}"
        doc.add_heading(title, level=2)
        if exp.get("location"):
            doc.add_paragraph(exp["location"])
        for bullet in exp.get("bullets", []):
            doc.add_paragraph(bullet, style="List Bullet")

    # Missing skills transparency — per Task 2 truthfulness requirement
    missing = resume_data.get("missing_skills", [])
    if missing:
        doc.add_heading("Skills Gap", level=1)
        doc.add_paragraph(
            f"Skills from job description not in profile (not added): {', '.join(missing)}"
        )

    doc.save(str(output_path))


def _convert_to_pdf(docx_path: Path, pdf_path: Path) -> bool:
    """
    LibreOffice headless first (production), reportlab fallback (local dev).
    Returns True if PDF was created successfully.
    """
    # Try LibreOffice headless
    try:
        result = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf",
             "--outdir", str(pdf_path.parent), str(docx_path)],
            capture_output=True, timeout=30, text=True,
        )
        if result.returncode == 0:
            lo_output = pdf_path.parent / (docx_path.stem + ".pdf")
            if lo_output.exists() and lo_output != pdf_path:
                lo_output.rename(pdf_path)
            elif lo_output.exists():
                pass  # already at correct path
            return pdf_path.exists()
        logger.warning("LibreOffice failed: %s", result.stderr[:200])
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("LibreOffice not available (%s), using reportlab fallback.", exc)

    # reportlab fallback
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        from docx import Document as DocxDocument

        # Extract text from DOCX for the PDF
        try:
            docx_doc = DocxDocument(str(docx_path))
            paragraphs_text = [p.text for p in docx_doc.paragraphs if p.text.strip()]
        except Exception:
            paragraphs_text = ["Resume content — install LibreOffice for formatted PDF."]

        pdf_doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
        styles = getSampleStyleSheet()
        story = []
        for text in paragraphs_text[:50]:  # limit for safety
            story.append(Paragraph(text, styles["Normal"]))
            story.append(Spacer(1, 6))
        pdf_doc.build(story)
        return True
    except ImportError:
        logger.error("Neither LibreOffice nor reportlab available for PDF.")
        return False


# ---------------------------------------------------------------------------
# Download URL helpers
# ---------------------------------------------------------------------------

def _build_download_url(requirement_id: int, file_type: str) -> str:
    """Build the download endpoint URL for a file type (pdf or docx)."""
    return f"{BASE_URL}/api/consultant/requirements/{requirement_id}/resume/download/{file_type}"


def _build_download_urls(requirement_id: int) -> dict:
    """
    Build fresh download URLs.
    Frontend spec: 'fresh presigned URLs generated on every fetchResumeData call'
    and 'staleTime: 0 — NO URL caching allowed per spec'.
    URLs expire in 24h (displayed in DownloadButtonGroup component).
    """
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    return {
        "pdfUrl": _build_download_url(requirement_id, "pdf"),
        "docxUrl": _build_download_url(requirement_id, "docx"),
        "expiresAt": expires_at,
    }


# ---------------------------------------------------------------------------
# Response builders — exactly matching frontend DTO contracts
# ---------------------------------------------------------------------------

def _build_resume_data_dto(
    generated: GeneratedResume,
    requirement: Requirement,
) -> dict:
    """
    Build ResumeDataDTO matching frontend types/index.ts:
    { requirementId, requirementRole, clientName, jdText,
      atsScore, atsBreakdown, skillMatch, downloadUrls,
      generationAttempts, generated }
    """
    return {
        "requirementId": str(requirement.id),
        "requirementRole": requirement.role,
        "clientName": requirement.client or "",
        "jdText": requirement.job_description or "",
        "atsScore": float(generated.ats_score or 0),
        "atsBreakdown": {
            "keywordMatch": float(generated.ats_keyword_score or 0),
            "roleTitleMatch": float(generated.ats_role_score or 0),
            "formatScore": float(generated.ats_format_score or 0),
        },
        "skillMatch": {
            "matched": generated.ats_matched_keywords or [],
            "missing": generated.ats_missing_keywords or [],
        },
        "downloadUrls": _build_download_urls(requirement.id),
        "generationAttempts": generated.generation_attempt,
        "generated": True,
    }


def _build_generate_result_dto(
    generated: GeneratedResume,
    requirement_id: int,
) -> dict:
    """
    Build GenerateResumeResultDTO matching frontend types/index.ts:
    { requirementId, atsScore, atsBreakdown, skillMatch,
      downloadUrls, generationAttempts }
    """
    return {
        "requirementId": str(requirement_id),
        "atsScore": float(generated.ats_score or 0),
        "atsBreakdown": {
            "keywordMatch": float(generated.ats_keyword_score or 0),
            "roleTitleMatch": float(generated.ats_role_score or 0),
            "formatScore": float(generated.ats_format_score or 0),
        },
        "skillMatch": {
            "matched": generated.ats_matched_keywords or [],
            "missing": generated.ats_missing_keywords or [],
        },
        "downloadUrls": _build_download_urls(requirement_id),
        "generationAttempts": generated.generation_attempt,
    }


# ---------------------------------------------------------------------------
# Core generation pipeline
# ---------------------------------------------------------------------------

async def _run_generation_pipeline(
    db: AsyncSession,
    consultant: Consultant,
    requirement: Requirement,
    match: RequirementConsultantMatch,
    current_user: User,
    attempt: int = 1,
) -> GeneratedResume:
    """
    Full Phase 6 pipeline per doc flow:
    AI Tailor → Validate → ATS Score → if <80 retry once → DOCX → PDF → store
    Max attempts: 3 (matching frontend MAX_ATTEMPTS = 3).
    """
    # Load experiences (batch — no N+1)
    exp_result = await db.execute(
        select(ConsultantExperience)
        .where(ConsultantExperience.consultant_id == consultant.id)
        .order_by(ConsultantExperience.sort_order.asc())
    )
    experiences = exp_result.scalars().all()

    matched_skills = match.matched_skills or []
    missing_skills = match.missing_skills or []

    logger.info(
        "AI resume generation: consultant_id=%s requirement_id=%s attempt=%d",
        consultant.id, requirement.id, attempt,
    )

    # ── Step 1: Call AI ───────────────────────────────────────────────────
    resume_data = await _call_ai_tailoring(
        consultant, experiences, requirement, matched_skills, missing_skills, db
    )

    # ── Step 2: Validate — reject invented skills ─────────────────────────
    resume_data, generation_notes = _validate_resume_output(resume_data, consultant)

    # ── Step 3: Build resume text for ATS scoring ─────────────────────────
    resume_text_parts = [
        resume_data.get("summary", ""),
        " ".join(resume_data.get("skills", [])),
        requirement.role,
    ]
    for exp in resume_data.get("experience", []):
        resume_text_parts.append(exp.get("role", ""))
        resume_text_parts.append(" ".join(exp.get("bullets", [])))
    resume_text = " ".join(resume_text_parts)

    # ── Step 4: ATS score ─────────────────────────────────────────────────
    jd_skills = matched_skills + missing_skills
    ats_total, kw_score, role_score, fmt_score, ats_matched, ats_missing = _ats_score(
        jd_skills, resume_text, requirement.role
    )
    logger.info("ATS score=%s attempt=%d consultant_id=%s", ats_total, attempt, consultant.id)

    # ── Step 5: One retry if below threshold (per doc Task 3) ─────────────
    if ats_total < ATS_PASS_THRESHOLD and attempt < MAX_GENERATION_ATTEMPTS:
        logger.info("ATS %s < %s — retrying (attempt %d)", ats_total, ATS_PASS_THRESHOLD, attempt + 1)
        return await _run_generation_pipeline(
            db, consultant, requirement, match, current_user, attempt=attempt + 1
        )

    final_status = "READY" if ats_total >= ATS_PASS_THRESHOLD else "NEEDS_REVIEW"
    if final_status == "NEEDS_REVIEW":
        logger.warning(
            "ATS %s < %s after %d attempts — NEEDS_REVIEW consultant_id=%s",
            ats_total, ATS_PASS_THRESHOLD, attempt, consultant.id,
        )

    # ── Step 6: Build filename (Task 5) ──────────────────────────────────
    full_name = (consultant.full_name or "").strip()
    name_parts = full_name.split() if full_name else ["", ""]
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    base_filename_with_ext = _build_resume_filename(
        first_name=first_name,
        last_name=last_name,
        role=requirement.role,
        client_name=requirement.client,
        vendor_name=requirement.vendor,
        years_exp=consultant.total_experience_years,
    )
    # base_filename_with_ext ends in .pdf per doc — strip for DOCX variant
    base_stem = base_filename_with_ext.removesuffix(".pdf")

    # ── Step 7: Generate DOCX ─────────────────────────────────────────────
    resume_dir = RESUME_UPLOAD_DIR / "generated" / str(consultant.id) / str(requirement.id)
    resume_dir.mkdir(parents=True, exist_ok=True)

    docx_path = resume_dir / f"{base_stem}.docx"
    pdf_path = resume_dir / f"{base_stem}.pdf"

    try:
        _generate_docx(resume_data, docx_path)
        logger.info("DOCX generated: %s", docx_path)
    except Exception as exc:
        logger.error("DOCX generation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to generate DOCX: {exc}")

    # ── Step 8: Convert to PDF ────────────────────────────────────────────
    pdf_ok = _convert_to_pdf(docx_path, pdf_path)
    s3_pdf_key = None
    if not pdf_ok:
        logger.warning("PDF conversion failed — DOCX available, PDF unavailable")
    else:
        try:
            from s3_service import upload_file_to_s3
            import uuid
            s3_pdf_key = f"resumes/generated/{consultant.id}/{requirement.id}/{uuid.uuid4()}.pdf"
            with open(pdf_path, "rb") as f:
                upload_success = upload_file_to_s3(f, s3_pdf_key, "application/pdf")
            if upload_success:
                logger.info(f"Uploaded generated PDF to S3: {s3_pdf_key}")
                # Optional: Delete local PDF to save space
                try:
                    pdf_path.unlink(missing_ok=True)
                except Exception as del_err:
                    logger.warning(f"Could not delete local PDF: {del_err}")
            else:
                logger.warning("S3 upload returned False, keeping local PDF")
                s3_pdf_key = None
        except Exception as e:
            logger.error(f"S3 upload failed for generated PDF: {e}")
            s3_pdf_key = None

    # ── Step 9: Mark previous versions non-final ─────────────────────────
    await db.execute(
        update(GeneratedResume)
        .where(
            GeneratedResume.consultant_id == consultant.id,
            GeneratedResume.requirement_id == requirement.id,
            GeneratedResume.is_final == True,
        )
        .values(is_final=False)
    )

    # ── Step 10: Save generated_resume record ────────────────────────────
    generated = GeneratedResume(
        consultant_id=consultant.id,
        requirement_id=requirement.id,
        created_by_user_id=current_user.id,
        ai_model=ANTHROPIC_MODEL,
        generation_notes=generation_notes,
        generation_attempt=attempt,
        resume_content=resume_data,
        ats_score=ats_total,
        ats_keyword_score=kw_score,
        ats_role_score=role_score,
        ats_format_score=fmt_score,
        ats_matched_keywords=ats_matched,
        ats_missing_keywords=ats_missing,
        docx_path=str(docx_path),
        pdf_path=s3_pdf_key if s3_pdf_key else (str(pdf_path) if pdf_ok else None),
        pdf_url=f"/api/consultant/requirements/{requirement.id}/resume/download/pdf" if pdf_ok else None,
        filename=base_filename_with_ext,
        status=final_status,
        generation_status="COMPLETED" if final_status == "READY" else final_status,
        is_final=True,
    )
    db.add(generated)

    # ── Step 11: Update match status ─────────────────────────────────────
    match.status = "READY_TO_APPLY" if final_status == "READY" else "RESUME_GENERATED"

    await db.commit()
    await db.refresh(generated)

    logger.info(
        "Generation complete: id=%s ats=%s status=%s match_status=%s",
        generated.id, ats_total, final_status, match.status,
    )
    return generated


# ---------------------------------------------------------------------------
# Task 1 — Generate Resume API
# ---------------------------------------------------------------------------

@router.post(
    "/api/consultant/requirements/{requirement_id}/generate-resume",
    summary="Generate AI-tailored resume (Task 1) — returns GenerateResumeResultDTO",
)
async def generate_resume(
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    CONSULTANT role only — per doc code example: require_role('CONSULTANT').
    Returns GenerateResumeResultDTO matching frontend types/index.ts.
    """
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)
    requirement = await _get_requirement_or_404(db, requirement_id)
    match = await _get_match_or_404(db, requirement_id, consultant.id)

    # Check max attempts — frontend MAX_ATTEMPTS = 3
    existing_count_result = await db.execute(
        select(GeneratedResume).where(
            GeneratedResume.consultant_id == consultant.id,
            GeneratedResume.requirement_id == requirement_id,
        )
    )
    existing_count = len(existing_count_result.scalars().all())
    if existing_count >= MAX_GENERATION_ATTEMPTS:
        raise HTTPException(
            status_code=422,
            detail=f"Maximum {MAX_GENERATION_ATTEMPTS} generation attempts reached for this requirement.",
        )

    # Validate prerequisites
    if not consultant.base_resume_text and not consultant.primary_skills:
        raise HTTPException(
            status_code=422,
            detail="Upload a base resume or add skills before generating a tailored resume.",
        )
    if not requirement.job_description:
        raise HTTPException(
            status_code=422,
            detail="This requirement has no job description. Cannot generate a tailored resume.",
        )

    generated = await _run_generation_pipeline(
        db=db,
        consultant=consultant,
        requirement=requirement,
        match=match,
        current_user=current_user,
        attempt=existing_count + 1,
    )

    try:
        from phase5 import _broadcast_event
        await _broadcast_event("resume_generated", {
            "resume_id": str(generated.id),
            "consultant_id": str(consultant.id),
            "requirement_id": str(requirement_id),
            "status": generated.generation_status,
            "ats_score": float(generated.ats_score) if generated.ats_score is not None else None,
        })
    except ImportError:
        pass

    return _build_generate_result_dto(generated, requirement_id)


# ---------------------------------------------------------------------------
# Task 6 — Resume Preview APIs (GET endpoints)
# ---------------------------------------------------------------------------

@router.get(
    "/api/consultant/requirements/{requirement_id}/resume",
    summary="Get current resume data — returns ResumeDataDTO (fresh download URLs every call)",
)
async def get_resume(
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns ResumeDataDTO exactly matching frontend types/index.ts.
    staleTime: 0 in frontend — fresh download URLs on every call per spec.
    Returns null-equivalent (404) if no resume exists → frontend redirects to dashboard.
    """
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)
    requirement = await _get_requirement_or_404(db, requirement_id)

    result = await db.execute(
        select(GeneratedResume).where(
            GeneratedResume.consultant_id == consultant.id,
            GeneratedResume.requirement_id == requirement_id,
            GeneratedResume.is_final == True,
        )
    )
    generated = result.scalars().first()
    if not generated:
        raise HTTPException(status_code=404, detail="No generated resume found.")

    return _build_resume_data_dto(generated, requirement)


class GeneratedResumeContentDTO(BaseModel):
    resumeContent: dict
    requirementRole: str
    clientName: str


@router.get(
    "/api/consultant/requirements/{requirement_id}/resume/content",
    response_model=GeneratedResumeContentDTO,
    summary="Get the editable structured content behind a tailored resume",
)
async def get_resume_content(
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    NEW: powers the dashboard's Edit action for tailored resumes. Nothing
    previously exposed resume_content — only the PDF/DOCX and ATS
    breakdown, via get_resume above. Same authorization lookup as that
    endpoint, just returning the structured JSON instead of file links.
    """
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)
    requirement = await _get_requirement_or_404(db, requirement_id)

    result = await db.execute(
        select(GeneratedResume).where(
            GeneratedResume.consultant_id == consultant.id,
            GeneratedResume.requirement_id == requirement_id,
            GeneratedResume.is_final == True,
        )
    )
    generated = result.scalars().first()
    if not generated:
        raise HTTPException(status_code=404, detail="No generated resume found.")

    return GeneratedResumeContentDTO(
        resumeContent=generated.resume_content or {},
        requirementRole=requirement.role,
        clientName=requirement.client or "",
    )


class UpdateGeneratedResumeRequest(BaseModel):
    resumeContent: dict


@router.put(
    "/api/consultant/requirements/{requirement_id}/resume/content",
    summary="Save edits to a tailored resume and regenerate its PDF/DOCX",
)
async def update_resume_content(
    requirement_id: int,
    request: UpdateGeneratedResumeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    NEW: the dashboard previously had no way to edit a tailored resume at
    all — this saves the edited content AND regenerates the actual
    downloadable files, so the PDF/DOCX a consultant downloads always
    matches what they last edited. Re-scores ATS against the same
    requirement/match so the score stays meaningful after edits, using
    the same pipeline as initial generation (_generate_docx,
    _convert_to_pdf, upload_file_to_s3).
    """
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)
    requirement = await _get_requirement_or_404(db, requirement_id)

    result = await db.execute(
        select(GeneratedResume).where(
            GeneratedResume.consultant_id == consultant.id,
            GeneratedResume.requirement_id == requirement_id,
            GeneratedResume.is_final == True,
        )
    )
    generated = result.scalars().first()
    if not generated:
        raise HTTPException(status_code=404, detail="No generated resume found.")

    resume_data = request.resumeContent
    generated.resume_content = resume_data

    # Re-score against the same requirement/match this resume was
    # originally tailored for, so editing doesn't silently zero the score.
    match_result = await db.execute(
        select(RequirementConsultantMatch).where(
            RequirementConsultantMatch.consultant_id == consultant.id,
            RequirementConsultantMatch.requirement_id == requirement_id,
        )
    )
    match = match_result.scalars().first()
    matched_skills = (match.matched_skills if match else None) or []
    missing_skills = (match.missing_skills if match else None) or []
    jd_skills = matched_skills + missing_skills

    resume_text_parts = [
        resume_data.get("summary", ""),
        " ".join(resume_data.get("skills", [])),
        requirement.role,
    ]
    for exp in resume_data.get("experience", []):
        resume_text_parts.append(exp.get("role", ""))
        resume_text_parts.append(" ".join(exp.get("bullets", [])))
    resume_text = " ".join(resume_text_parts)

    if jd_skills:
        ats_total, kw_score, role_score, fmt_score, ats_matched, ats_missing = _ats_score(
            jd_skills, resume_text, requirement.role
        )
        generated.ats_score = ats_total
        generated.ats_keyword_score = kw_score
        generated.ats_role_score = role_score
        generated.ats_format_score = fmt_score
        generated.ats_matched_keywords = ats_matched
        generated.ats_missing_keywords = ats_missing

    # Rebuild the actual files from the edited content — same file layout
    # and naming as initial generation, so download links keep working.
    full_name = (consultant.full_name or "").strip()
    name_parts = full_name.split() if full_name else ["", ""]
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    base_filename_with_ext = _build_resume_filename(
        first_name=first_name,
        last_name=last_name,
        role=requirement.role,
        client_name=requirement.client,
        vendor_name=requirement.vendor,
        years_exp=consultant.total_experience_years,
    )
    base_stem = base_filename_with_ext.removesuffix(".pdf")

    resume_dir = RESUME_UPLOAD_DIR / "generated" / str(consultant.id) / str(requirement.id)
    resume_dir.mkdir(parents=True, exist_ok=True)
    docx_path = resume_dir / f"{base_stem}.docx"
    pdf_path = resume_dir / f"{base_stem}.pdf"

    try:
        _generate_docx(resume_data, docx_path)
        pdf_ok = _convert_to_pdf(docx_path, pdf_path)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to regenerate DOCX: {exc}")

    s3_pdf_key = None
    if pdf_ok:
        from s3_service import upload_file_to_s3
        s3_pdf_key = f"generated/{consultant.id}/{requirement.id}/{base_stem}.pdf"
        with open(pdf_path, "rb") as f:
            if not upload_file_to_s3(f, s3_pdf_key, "application/pdf"):
                s3_pdf_key = None

    generated.filename = base_filename_with_ext
    generated.docx_path = str(docx_path)
    generated.pdf_path = s3_pdf_key if s3_pdf_key else (str(pdf_path) if pdf_ok else None)
    generated.generation_status = "COMPLETED" if pdf_ok else generated.generation_status

    await db.commit()
    await db.refresh(generated)

    return _build_resume_data_dto(generated, requirement)


@router.get(
    "/api/consultant/requirements/{requirement_id}/resume/history",
    summary="Get all generation attempts — returns ResumeHistoryDTO",
)
async def get_resume_history(
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns ResumeHistoryDTO matching frontend types/index.ts:
    { requirementId, attempts: [{ attemptNumber, atsScore, generatedAt }] }
    """
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    result = await db.execute(
        select(GeneratedResume)
        .where(
            GeneratedResume.consultant_id == consultant.id,
            GeneratedResume.requirement_id == requirement_id,
        )
        .order_by(GeneratedResume.created_at.asc())
    )
    resumes = result.scalars().all()

    attempts = [
        {
            "attemptNumber": r.generation_attempt,
            "atsScore": float(r.ats_score or 0),
            "generatedAt": r.created_at.isoformat() if r.created_at else "",
        }
        for r in resumes
    ]

    return {
        "requirementId": str(requirement_id),
        "attempts": attempts,
    }


@router.get(
    "/api/consultant/requirements/{requirement_id}/resume/download/{file_type}",
    summary="Download resume file (pdf or docx) — serves actual file",
    response_class=FileResponse,
)
async def download_resume(
    requirement_id: int,
    file_type: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Serves the actual DOCX or PDF file.
    Frontend DownloadButtonGroup and ResumeActionCell both link to these URLs.
    file_type must be 'pdf' or 'docx'.
    """
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    if file_type not in ("pdf", "docx"):
        raise HTTPException(status_code=422, detail="file_type must be 'pdf' or 'docx'")

    result = await db.execute(
        select(GeneratedResume).where(
            GeneratedResume.consultant_id == consultant.id,
            GeneratedResume.requirement_id == requirement_id,
            GeneratedResume.is_final == True,
        )
    )
    generated = result.scalars().first()
    if not generated:
        raise HTTPException(status_code=404, detail="No generated resume found.")

    file_path = generated.pdf_path if file_type == "pdf" else generated.docx_path

    # pdf_path may hold a Spaces object KEY rather than a local path (see
    # _run_generation_pipeline — the local file is deleted after upload).
    # Path(key).exists() is always False, so stream it back from Spaces.
    if file_path and not Path(file_path).exists():
        from s3_service import download_file_from_s3
        body, content_type = download_file_from_s3(file_path)
        if body:
            from fastapi.responses import Response
            return Response(
                content=body,
                media_type=content_type or "application/pdf",
                headers={
                    "Content-Disposition":
                        f'attachment; filename="{generated.filename or Path(file_path).name}"'
                },
            )

    if not file_path or not Path(file_path).exists():
        raise HTTPException(
            status_code=404,
            detail=f"{file_type.upper()} file not available. "
                   + ("Install LibreOffice on the server for PDF generation." if file_type == "pdf" else ""),
        )

    media_type = (
        "application/pdf" if file_type == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    filename = generated.filename or Path(file_path).name

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=filename,
    )


@router.get(
    "/api/recruiter/consultants/{consultant_id}/requirements/{requirement_id}/resume",
    summary="Get resume data for a consultant (recruiter/admin view) — returns ResumeDataDTO",
)
async def get_resume_recruiter_view(
    consultant_id: int,
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Recruiter/Admin view. Enforces recruiter→consultant mapping for RECRUITER role.
    Returns same ResumeDataDTO shape as the consultant endpoint.
    """
    _require_role(current_user, "RECRUITER", "ADMIN")

    if current_user.role == "RECRUITER":
        mapping_result = await db.execute(
            select(RecruiterConsultant).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.consultant_id == consultant_id,
                RecruiterConsultant.is_active == True,
            )
        )
        if not mapping_result.scalars().first():
            raise HTTPException(status_code=403, detail="Consultant not assigned to this recruiter")

    requirement = await _get_requirement_or_404(db, requirement_id)

    result = await db.execute(
        select(GeneratedResume).where(
            GeneratedResume.consultant_id == consultant_id,
            GeneratedResume.requirement_id == requirement_id,
            GeneratedResume.is_final == True,
        )
    )
    generated = result.scalars().first()
    if not generated:
        raise HTTPException(status_code=404, detail="No generated resume found.")

    return _build_resume_data_dto(generated, requirement)