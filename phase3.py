# phase3.py
# ---------------------------------------------------------------------------
# Phase 3 — Consultant Profiles, Resume Upload, Experience, Recruiter Mapping
#
# Architecture: single flat file in project root, following the same pattern
# as main.py. Reuses get_db, get_current_user, decode_access_token,
# PK_TYPE/FK_TYPE, ArrayTextColumn, JSONBColumn from existing modules.
#
# New endpoints:
#
#   Consultant CRUD
#   POST   /api/consultant/profile              create consultant profile
#   GET    /api/consultant/profile              get own profile (consultant)
#   PUT    /api/consultant/profile              update own profile (consultant)
#   GET    /api/consultants/{id}                get any consultant (admin/recruiter)
#   PUT    /api/consultants/{id}                update any consultant (admin/recruiter)
#   GET    /api/consultants                     list consultants (paginated)
#   POST   /api/admin/consultants               create consultant (admin)
#   PATCH  /api/admin/consultants/{id}/deactivate  soft-delete
#   PATCH  /api/admin/consultants/{id}/activate    reactivate
#
#   Resume
#   POST   /api/consultant/resume/upload        upload base resume (DOCX/PDF)
#   GET    /api/consultant/resume               get resume metadata
#   DELETE /api/consultant/resume               remove resume (admin)
#
#   Experience
#   GET    /api/consultant/experience                     list entries
#   POST   /api/consultant/experience                     add entry
#   PUT    /api/consultant/experience/{id}                full update
#   DELETE /api/consultant/experience/{id}                delete
#   PATCH  /api/consultant/experience/reorder             save sort order
#
#   Recruiter ↔ Consultant Mapping
#   GET    /api/recruiter/consultants                     my consultants
#   POST   /api/recruiter/consultants                     assign consultant
#   DELETE /api/recruiter/consultants/{id}                unassign
#   GET    /api/admin/consultants/{id}/recruiters         list recruiters (admin)
#   PUT    /api/admin/consultants/{id}/recruiters         set recruiters (admin)
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import re
import secrets
import string
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from database import get_db
from models import (
    User,
    Consultant,
    RecruiterConsultant,
    ConsultantExperience,
    Application,
    Resume,
    PK_TYPE,
    FK_TYPE,
)

# ---------------------------------------------------------------------------
# Re-use get_current_user and decode_access_token from main.py
# Imported here at module level — no circular dependency because phase3.py
# does NOT import the FastAPI app instance, only the utility functions.
# ---------------------------------------------------------------------------
from auth import get_current_user, decode_access_token
from s3_service import upload_file_to_s3, delete_file_from_s3

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# FEATURE CHANGE: only .docx is accepted now, no PDF — matches the same
# restriction applied to the "My Resumes" upload flow (resume_router.py).
ALLOWED_RESUME_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads/resumes"))

# ---------------------------------------------------------------------------
# Pydantic schemas — matching frontend DTO contracts exactly
# (ConsultantProfileDTO, ExperienceDTO from frontend types/index.ts)
# ---------------------------------------------------------------------------

# ── Profile ─────────────────────────────────────────────────────────────────

class EducationEntryRequest(BaseModel):
    degree: Optional[str] = None
    institution: Optional[str] = None
    year: Optional[str] = None
    details: Optional[str] = None


class ProfileUpdateRequest(BaseModel):
    fullName: str = Field(..., min_length=1, max_length=200)
    location: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=30)
    linkedInUrl: Optional[str] = None
    primarySkills: List[str] = []
    secondarySkills: List[str] = []
    workAuth: Optional[str] = None
    employmentTypes: List[str] = ["C2C"]
    preferredRoles: Optional[str] = None
    preferredLocations: Optional[str] = None
    totalExperienceYears: Optional[float] = Field(None, ge=0, le=60)
    # BUG FIX: these three were never collectable anywhere — the
    # "Profile incomplete" check (resume_validation.py) has always
    # required them, but there was no form field, no request field, and
    # (for title/summary/education) no storage column at all. Stored in
    # User.resume_info (see update_own_profile below) rather than new
    # Consultant columns, to avoid a migration.
    title: Optional[str] = None
    summary: Optional[str] = None
    education: List[EducationEntryRequest] = []

    @field_validator("workAuth")
    @classmethod
    def validate_work_auth(cls, v):
        if v is not None and v not in {"US_CITIZEN", "GC", "H1B", "OPT", "OTHER"}:
            raise ValueError(f"workAuth must be one of US_CITIZEN, GC, H1B, OPT, OTHER")
        return v

    @field_validator("employmentTypes")
    @classmethod
    def validate_employment_types(cls, v):
        allowed = {"C2C", "W2", "FULL_TIME"}
        invalid = [t for t in v if t not in allowed]
        if invalid:
            raise ValueError(f"Invalid employmentTypes: {invalid}")
        return list(dict.fromkeys(v))


class ProfileResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    fullName: Optional[str] = None
    email: Optional[str] = None
    location: Optional[str] = None
    phone: Optional[str] = None
    linkedInUrl: Optional[str] = None
    primarySkills: List[str] = []
    secondarySkills: List[str] = []
    workAuth: Optional[str] = None
    employmentTypes: List[str] = []
    resume: Optional[dict] = None
    experienceCount: int = 0
    gmailConnected: bool = False
    baseResumeUploaded: bool = False
    atsScore: float = 0.0
    status: str = "ACTIVE"
    preferredRoles: Optional[str] = None
    preferredLocations: Optional[str] = None
    totalExperienceYears: Optional[float] = None
    availabilityStatus: Optional[str] = None
    createdAt: Optional[str] = None
    # BUG FIX: previously missing — recruiter roster grid (ConsultantCard)
    # and the recruiter/admin detail views hardcoded these to 0 on the
    # frontend because this endpoint never returned them. Now computed for
    # real in _consultant_to_profile_response below.
    profileCompleteness: int = 0
    activeApplicationsCount: int = 0
    # BUG FIX: read back from User.resume_info — see ProfileUpdateRequest.
    title: Optional[str] = None
    summary: Optional[str] = None
    education: List[EducationEntryRequest] = []


# ---------------------------------------------------------------------------
# Admin "Add Consultant" request/response — field names, enum values, and
# response shape verified against the actual frontend form component
# (AddConsultantDrawer.tsx): its useCreateConsultant() hook posts snake_case
# keys straight from `form` state, validates work_auth against WORK_AUTHS =
# ["USC","GC","H1B","OPT","CPT","EAD","TN","Other"] and employment_prefs
# against EMP_PREFS = ["C2C","W2","1099","FULL_TIME","CONTRACT"], and reads
# result.message / result.temp_password back from CreateConsultantResponseDTO
# to show the "temporary password — shown once" panel. This DTO intentionally
# does NOT follow the camelCase convention ProfileUpdateRequest uses — its
# only consumer is this one admin form, so it matches that form's payload
# shape exactly, same as the per-endpoint casing convention phase5.py uses.
# ---------------------------------------------------------------------------

_ADMIN_WORK_AUTHS = {"USC", "GC", "H1B", "OPT", "CPT", "EAD", "TN", "Other"}
_ADMIN_EMPLOYMENT_PREFS = {"C2C", "W2", "1099", "FULL_TIME", "CONTRACT"}


class AdminConsultantCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    work_auth: str
    employment_prefs: List[str] = Field(..., min_length=1)
    primary_skills: str = ""
    recruiter_id: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=30)
    current_location: Optional[str] = None
    preferred_locations: Optional[str] = None
    availability_status: Optional[str] = None
    total_experience_years: Optional[float] = Field(None, ge=0, le=60)
    secondary_skills: Optional[str] = None
    preferred_roles: Optional[str] = None
    resume_info: Optional[dict] = None
    linkedin_url: Optional[str] = None
    education: List[EducationEntryRequest] = []

    @field_validator("email")
    @classmethod
    def normalise_email(cls, v):
        return v.lower().strip()

    @field_validator("work_auth")
    @classmethod
    def validate_work_auth(cls, v):
        if v not in _ADMIN_WORK_AUTHS:
            raise ValueError(f"work_auth must be one of {sorted(_ADMIN_WORK_AUTHS)}")
        return v

    @field_validator("employment_prefs")
    @classmethod
    def validate_employment_prefs(cls, v):
        invalid = [t for t in v if t not in _ADMIN_EMPLOYMENT_PREFS]
        if invalid:
            raise ValueError(f"Invalid employment_prefs: {invalid}")
        return list(dict.fromkeys(v))


class CreateConsultantResponse(BaseModel):
    """Matches frontend CreateConsultantResponseDTO — the drawer reads
    result.message and result.temp_password directly."""
    message: str
    temp_password: str
    consultant_id: str
    name: str
    email: str


class ConsultantListResponse(BaseModel):
    data: List[ProfileResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class ResumeUploadResponse(BaseModel):
    resume: dict   # matches ResumeInfoDTO: { filename, uploadedAt, sizeBytes }


# ── Experience ───────────────────────────────────────────────────────────────

class ExperienceMonthYear(BaseModel):
    month: int = Field(..., ge=1, le=12)
    year: int = Field(..., ge=1970, le=2100)


class ExperienceRequest(BaseModel):
    clientName: str = Field(..., min_length=1, max_length=200)
    implementationPartner: Optional[str] = Field(None, max_length=200)
    roleTitle: str = Field(..., min_length=1, max_length=200)
    startDate: ExperienceMonthYear
    endDate: Optional[ExperienceMonthYear] = None
    isPresent: bool = False
    location: Optional[str] = None
    workMode: Optional[str] = Field(None, pattern="^(REMOTE|ONSITE|HYBRID|TRAVEL_REQUIRED)$")
    workModeDetail: Optional[str] = None
    technologies: List[str] = []
    responsibilities: Optional[str] = None
    achievements: Optional[str] = None
    sortOrder: int = 0

    @model_validator(mode="after")
    def validate_dates(self):
        if self.isPresent:
            self.endDate = None
        return self


class ExperienceResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    clientName: str
    implementationPartner: Optional[str] = None
    roleTitle: str
    startDate: Optional[ExperienceMonthYear] = None
    endDate: Optional[ExperienceMonthYear] = None
    isPresent: bool = False
    location: Optional[str] = None
    workMode: Optional[str] = None
    workModeDetail: Optional[str] = None
    technologies: List[str] = []
    responsibilities: Optional[str] = None
    achievements: Optional[str] = None
    sortOrder: int = 0


class ReorderRequest(BaseModel):
    orderedIds: List[str] = Field(..., min_length=1)


# ── Recruiter Mapping ────────────────────────────────────────────────────────

class AssignConsultantRequest(BaseModel):
    consultantId: int = Field(..., gt=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_role(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {list(roles)}",
        )


async def _get_consultant_or_404(db: AsyncSession, consultant_id: int) -> Consultant:
    result = await db.execute(select(Consultant).where(Consultant.id == consultant_id))
    c = result.scalars().first()
    if not c:
        raise HTTPException(status_code=404, detail="Consultant not found")
    return c


async def _assert_recruiter_mapped(db: AsyncSession, recruiter_id: int, consultant_id: int) -> None:
    result = await db.execute(
        select(RecruiterConsultant).where(
            RecruiterConsultant.recruiter_id == recruiter_id,
            RecruiterConsultant.consultant_id == consultant_id,
            RecruiterConsultant.is_active == True,
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=403, detail="Consultant not assigned to this recruiter")


async def _get_consultant_for_user(db: AsyncSession, user: User) -> Consultant:
    """Return the Consultant row linked to a CONSULTANT-role user."""
    result = await db.execute(select(Consultant).where(Consultant.user_id == user.id))
    c = result.scalars().first()
    if not c:
        raise HTTPException(status_code=404, detail="Consultant profile not found for this user")
    return c


async def _consultant_to_profile_response(
    db: AsyncSession, c: Consultant, experience_count: int = 0
) -> ProfileResponse:
    # BUG FIX: title/summary/education/linkedin have no Consultant
    # column — read them from the linked User.resume_info JSON instead,
    # the same blob update_own_profile now writes them into.
    resume_info: Dict[str, Any] = {}
    if c.user_id:
        user_result = await db.execute(select(User.resume_info).where(User.id == c.user_id))
        resume_info = user_result.scalar_one_or_none() or {}
    """Map ORM Consultant → ProfileResponse matching frontend ConsultantProfileDTO."""
    primary = [s.strip() for s in (c.primary_skills or "").split(",") if s.strip()]
    secondary = [s.strip() for s in (c.secondary_skills or "").split(",") if s.strip()]
    emp_types = c.preferred_employment_types or []

    resume = None
    if c.base_resume_file_path:
        from s3_service import get_s3_file_metadata
        fname = Path(c.base_resume_file_path).name
        size_bytes, _content_type = get_s3_file_metadata(c.base_resume_file_path)
        resume = {
            "filename": fname,
            "uploadedAt": c.updated_at.isoformat() if c.updated_at else datetime.utcnow().isoformat(),
            "sizeBytes": size_bytes or 0,
        }

    # BUG FIX: was `float(c.ats_score or 0)` — Consultant.ats_score is a
    # column that is never written anywhere in the codebase (only ever read
    # here and in phase_users_service.py), so it was permanently stuck at
    # its default of 0 and every "ATS Score" in the UI showed 0. The real,
    # meaningful ATS score lives per-generated-resume (Resume.ats_score,
    # written by phase6.py/resume_router.py whenever a tailored resume is
    # built). Use the consultant's most recently generated resume's score
    # instead — same signal the resume screens already show.
    latest_resume_result = await db.execute(
        select(Resume.ats_score)
        .where(Resume.user_id == c.user_id, Resume.ats_score.isnot(None))
        .order_by(Resume.created_at.desc())
        .limit(1)
    )
    latest_ats_score = latest_resume_result.scalar_one_or_none() if c.user_id else None

    # BUG FIX: profileCompleteness and activeApplicationsCount were not
    # returned by this endpoint at all — ConsultantCard (recruiter roster
    # grid) and the recruiter/admin detail pages hardcoded both to 0 on the
    # frontend with a comment noting the backend gap. Completeness formula
    # mirrors ProfileCompletenessBar.tsx / phase_users_service.py so every
    # screen agrees on the same number. Active apps = applications actually
    # SENT for this consultant (same definition phase_users_service.py uses
    # for total_applications_sent).
     
    completeness = 0
    if (c.primary_skills or "").strip() or (c.secondary_skills or "").strip():
        completeness += 30  # Skills
    if experience_count > 0:
        completeness += 25  # Experience
    if c.preferred_employment_types:
        completeness += 20  # Employment type
    if (c.work_authorization or "").strip():
        completeness += 15  # Work auth
    if len((c.current_location or "").strip()) >= 2:
        completeness += 10  # Location

    active_apps_result = await db.execute(
        select(func.count()).select_from(Application).where(
            Application.consultant_id == c.id, Application.status == "SENT"
        )
    )
    active_applications_count = active_apps_result.scalar_one() or 0

    return ProfileResponse(
        id=str(c.id),
        fullName=c.full_name,
        email=c.email,
        location=c.current_location,
        phone=c.phone,
        # Prefer the real Consultant.linkedin_url column (what admin edits
        # write to) — fall back to the legacy resume_info blob only for
        # rows saved before this endpoint wrote the column directly.
        # BUG FIX: `c.linkedin_url or resume_info.get(...)` treated an
        # explicitly-cleared LinkedIn URL (admin/consultant blanked the
        # field, leaving "") the same as "never set" — Python's `or` falls
        # through on any falsy value, not just None — so clearing the field
        # would silently resurrect old, stale data from the legacy blob
        # instead of showing empty. Only fall back when the column is
        # genuinely unset (None), which only happens for rows saved before
        # this column existed.
        linkedInUrl=c.linkedin_url if c.linkedin_url is not None else resume_info.get("linkedin"),
        primarySkills=primary,
        secondarySkills=secondary,
        workAuth=c.work_authorization,
        employmentTypes=emp_types,
        resume=resume,
        experienceCount=experience_count,
        gmailConnected=c.gmail_connected,
        baseResumeUploaded=bool(c.base_resume_file_path),
        atsScore=float(latest_ats_score) if latest_ats_score is not None else 0.0,
        status=c.status,
        preferredRoles=c.preferred_roles,
        preferredLocations=c.preferred_locations,
        title=resume_info.get("title"),
        summary=resume_info.get("summary"),
        # Prefer the real Consultant.education column (what admin now
        # edits) — fall back to the legacy resume_info blob only for rows
        # saved before update_own_profile started writing the column too.
        education=c.education or resume_info.get("education") or [],
        totalExperienceYears=float(c.total_experience_years) if c.total_experience_years is not None else None,
        availabilityStatus=c.availability_status,
        createdAt=c.created_at.isoformat() if c.created_at else None,
        profileCompleteness=completeness,
        activeApplicationsCount=active_applications_count,
    )


def _exp_to_response(e: ConsultantExperience) -> ExperienceResponse:
    """Map ORM ConsultantExperience → ExperienceResponse matching frontend ExperienceDTO."""
    start_date = None
    if e.start_date:
        start_date = ExperienceMonthYear(month=e.start_date.month, year=e.start_date.year)

    end_date = None
    if not e.is_present and e.end_date:
        end_date = ExperienceMonthYear(month=e.end_date.month, year=e.end_date.year)

    return ExperienceResponse(
        id=str(e.id),
        clientName=e.client_name,
        implementationPartner=e.implementation_partner,
        roleTitle=e.role_title,
        startDate=start_date,
        endDate=end_date,
        isPresent=e.is_present,
        location=e.location,
        workMode=e.work_mode,
        workModeDetail=e.work_mode_detail,
        technologies=e.technologies or [],
        responsibilities=e.responsibilities,
        achievements=e.achievements,
        sortOrder=e.sort_order,
    )

def _save_resume_file(file_bytes: bytes, consultant_id: int, original_filename: str, content_type: str) -> str:
    """Upload file to S3, return the S3 key."""
    import io
    from s3_service import upload_file_to_s3
    ext_map = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/pdf": ".pdf",
    }
    ext = ext_map.get(content_type, ".bin")
    s3_key = f"uploads/resumes/{consultant_id}/{uuid.uuid4().hex}{ext}"
    upload_file_to_s3(io.BytesIO(file_bytes), s3_key, content_type)
    return s3_key


def _delete_file_if_exists(path: str) -> None:
    p = Path(path)
    if p.exists() and p.is_file():
        p.unlink()


def _extract_text_from_docx(file_bytes: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        parts.append(cell.text.strip())
        text = "\n".join(parts)
        return text.replace("\x00", "").replace("\u0000", "")
    except Exception as exc:
        logger.warning("DOCX text extraction failed: %s", exc)
        return ""


def _extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    parts.append(text.strip())
        extracted = "\n".join(parts)
        return extracted.replace("\x00", "").replace("\u0000", "")
    except Exception as exc:
        logger.warning("PDF text extraction failed: %s", exc)
        return ""


def _extract_resume_text(file_bytes: bytes, content_type: str) -> str:
    text = ""
    if "wordprocessingml" in content_type or content_type == "application/docx":
        text = _extract_text_from_docx(file_bytes)
    elif content_type == "application/pdf":
        text = _extract_text_from_pdf(file_bytes)
    return text.replace("\x00", "").replace("\u0000", "")


def _generate_temp_password(length: int = 12) -> str:
    """Generate a random temporary password containing upper, lower, and digit chars."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
        ):
            return pwd


def _hash_password(password: str) -> str:
    """
    Hash a password for storage. Prefers auth.py's own hasher, so the login
    flow (auth.py) verifies it with the exact same algorithm/config it was
    hashed with; falls back to a local passlib bcrypt context only if
    auth.py doesn't expose one.
    """
    try:
        from auth import get_password_hash
        return get_password_hash(password)
    except ImportError:
        pass
    try:
        from passlib.context import CryptContext
        return CryptContext(schemes=["bcrypt"], deprecated="auto").hash(password)
    except ImportError:
        raise RuntimeError(
            "No password hasher available for admin_create_consultant() — "
            "add get_password_hash() to auth.py, or install passlib[bcrypt]."
        )


def _set_password_on_user(user: User, hashed_password: str) -> None:
    """
    Set the hashed password on a new User instance. The exact column name
    isn't referenced anywhere else in this file, so this checks the common
    candidates on the mapped class rather than guessing — update the
    candidate list if your User model uses a different name.
    """
    for candidate in ("hashed_password", "password_hash", "password"):
        if hasattr(User, candidate):
            setattr(user, candidate, hashed_password)
            return
    raise RuntimeError(
        "Could not find a password column on the User model (checked "
        "hashed_password/password_hash/password) — update "
        "_set_password_on_user() in phase3.py with the correct column name."
    )


# Canonical skill library — 100+ skills with aliases for detection
_SKILL_ALIASES: dict[str, list[str]] = {
    "Python": ["python", "python3"],
    "Java": ["java", "core java"],
    "JavaScript": ["javascript", "js", "es6"],
    "TypeScript": ["typescript", "ts"],
    "C#": ["c#", "csharp"],
    "Go": ["golang", "go"],
    "React": ["react", "react.js", "reactjs"],
    "Angular": ["angular", "angularjs"],
    "Vue.js": ["vue", "vue.js", "vuejs"],
    "Next.js": ["next.js", "nextjs"],
    "Node.js": ["node.js", "nodejs"],
    "FastAPI": ["fastapi"],
    "Django": ["django"],
    "Flask": ["flask"],
    "Spring Boot": ["spring boot", "springboot"],
    "PostgreSQL": ["postgresql", "postgres"],
    "MySQL": ["mysql"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "Elasticsearch": ["elasticsearch"],
    "AWS": ["aws", "amazon web services"],
    "Azure": ["azure", "microsoft azure"],
    "GCP": ["gcp", "google cloud"],
    "Docker": ["docker"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Terraform": ["terraform"],
    "CI/CD": ["ci/cd", "cicd"],
    "REST API": ["rest api", "restful"],
    "GraphQL": ["graphql"],
    "Microservices": ["microservices"],
    "Machine Learning": ["machine learning", "ml"],
    "SQL": ["sql"],
    "Kafka": ["kafka", "apache kafka"],
    "Spark": ["spark", "apache spark", "pyspark"],
    "Airflow": ["airflow", "apache airflow"],
    "Tailwind": ["tailwind", "tailwindcss"],
    "Redux": ["redux"],
    "SAP": ["sap"],
    "Salesforce": ["salesforce", "sfdc"],
    "ServiceNow": ["servicenow"],
    "Linux": ["linux", "ubuntu"],
    "Ansible": ["ansible"],
    "Jenkins": ["jenkins"],
}

_ALIAS_MAP: dict[str, str] = {
    alias.lower(): canonical
    for canonical, aliases in _SKILL_ALIASES.items()
    for alias in aliases
}


def _detect_skills(text: str) -> list[str]:
    """Return canonical skill names found in text, ordered by first appearance."""
    if not text:
        return []
    lower = text.lower()
    found: dict[str, int] = {}
    for alias, canonical in _ALIAS_MAP.items():
        pos = lower.find(alias)
        if pos != -1 and (canonical not in found or pos < found[canonical]):
            found[canonical] = pos
    return [k for k, _ in sorted(found.items(), key=lambda x: x[1])]


# ---------------------------------------------------------------------------
# Consultant Profile endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/api/consultant/profile",
    response_model=ProfileResponse,
    summary="Get own consultant profile",
)
async def get_own_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CONSULTANT role only — returns their own profile."""
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    count_result = await db.execute(
        select(func.count()).where(ConsultantExperience.consultant_id == consultant.id)
    )
    exp_count = count_result.scalar_one()
    return await _consultant_to_profile_response(db, consultant, exp_count)


@router.put(
    "/api/consultant/profile",
    response_model=ProfileResponse,
    summary="Update own consultant profile",
)
async def update_own_profile(
    payload: ProfileUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CONSULTANT role only — updates their own profile."""
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    consultant.full_name = payload.fullName
    consultant.current_location = payload.location
    consultant.phone = payload.phone
    consultant.work_authorization = payload.workAuth
    consultant.primary_skills = ", ".join(payload.primarySkills)
    consultant.secondary_skills = ", ".join(payload.secondarySkills)
    # BUG FIX: this used to only stash linkedInUrl inside User.resume_info
    # (see below) — the admin screens (phase_users_service.py, phase3.py's
    # admin update_consultant) read/write the real Consultant.linkedin_url
    # column instead, so a consultant editing their own LinkedIn URL here
    # never showed up on the admin side. Write the real column too so both
    # directions stay in sync.
    consultant.linkedin_url = payload.linkedInUrl
    consultant.preferred_employment_types = payload.employmentTypes
    consultant.preferred_roles = payload.preferredRoles
    consultant.preferred_locations = payload.preferredLocations
    consultant.total_experience_years = payload.totalExperienceYears
    # Same fix as linkedin_url above: write the real Consultant.education
    # column (what admin now reads/edits) in addition to resume_info below
    # (kept for resume_validation.py's FIELD_CHECKS / resume generation).
    consultant.education = [e.model_dump() for e in payload.education]

    # BUG FIX: this consultant self-service endpoint never wrote to
    # User.resume_info at all — the AI generation eligibility check
    # (resume_validation.py) only ever reads from there, so no amount of
    # filling in this form could ever clear the "Profile incomplete"
    # warning. Sync every field it checks for, using the exact keys
    # FIELD_CHECKS expects. linkedInUrl was ALSO previously accepted by
    # this endpoint's request body but never assigned anywhere — genuinely
    # discarded on every save until now.
    existing_info = dict(current_user.resume_info or {})
    existing_info.update({
        "full_name": payload.fullName,
        "email": current_user.email,
        "phone": payload.phone,
        "linkedin": payload.linkedInUrl,
        "title": payload.title,
        "summary": payload.summary,
        "years_experience": payload.totalExperienceYears,
        "skills": payload.primarySkills + payload.secondarySkills,
        "education": [e.model_dump() for e in payload.education],
    })
    current_user.resume_info = existing_info

    # Regenerate base_resume_text right away — matching_router.py and the
    # completeness check both read it, and neither should have to wait
    # for a manual visit to the Base Resume editor to see this save.
    from resume_router import sync_base_resume_text
    await sync_base_resume_text(db, consultant)

    await db.commit()
    await db.refresh(consultant)

    # Re-run matching for this consultant so their requirement matches reflect
    # the just-saved profile. Runs in the background with its own DB session so
    # it never blocks or fails the profile save.
    consultant_id = consultant.id
    async def _rematch_in_background(cid: int):
        from database import AsyncSessionLocal
        from phase4 import match_consultant
        try:
            async with AsyncSessionLocal() as bg_session:
                await match_consultant(bg_session, cid)
        except Exception as e:
            logger.error("Background auto-match failed for consultant_id=%s: %s", cid, e)
            from error_logger import log_db_error
            await log_db_error(
                stage="background_auto_match",
                error=e,
                source_type="consultant",
                source_id=str(cid),
            )
    asyncio.create_task(_rematch_in_background(consultant_id))

    count_result = await db.execute(
        select(func.count()).where(ConsultantExperience.consultant_id == consultant.id)
    )
    exp_count = count_result.scalar_one()
    return await _consultant_to_profile_response(db, consultant, exp_count)


@router.get(
    "/api/consultants",
    response_model=ConsultantListResponse,
    summary="List consultants (admin sees all, recruiter sees assigned)",
)
async def list_consultants(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER")

    query = select(Consultant)

    if current_user.role == "RECRUITER":
        assigned = select(RecruiterConsultant.consultant_id).where(
            RecruiterConsultant.recruiter_id == current_user.id,
            RecruiterConsultant.is_active == True,
        )
        query = query.where(Consultant.id.in_(assigned))

    if status:
        if status not in Consultant.VALID_STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(Consultant.VALID_STATUSES)}")
        query = query.where(Consultant.status == status)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (await db.execute(query.order_by(Consultant.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()

    # Batch-count experience rows for this whole page in one query instead
    # of N+1 — profileCompleteness now factors in Experience, so leaving
    # this defaulted to 0 here would show every consultant on this roster
    # as missing Experience regardless of their real data.
    exp_counts: Dict[int, int] = {}
    if rows:
        exp_rows = (await db.execute(
            select(ConsultantExperience.consultant_id, func.count())
            .where(ConsultantExperience.consultant_id.in_([c.id for c in rows]))
            .group_by(ConsultantExperience.consultant_id)
        )).all()
        exp_counts = {cons_id: cnt for cons_id, cnt in exp_rows}

    data = [
        await _consultant_to_profile_response(db, c, exp_counts.get(c.id, 0))
        for c in rows
    ]
    return ConsultantListResponse(

        data=data,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if page_size else 0,
    )


@router.get(
    "/api/consultants/{consultant_id}",
    response_model=ProfileResponse,
    summary="Get consultant by ID (admin or assigned recruiter)",
)
async def get_consultant_by_id(
    consultant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER")
    if current_user.role == "RECRUITER":
        await _assert_recruiter_mapped(db, current_user.id, consultant_id)

    consultant = await _get_consultant_or_404(db, consultant_id)
    count_result = await db.execute(
        select(func.count()).where(ConsultantExperience.consultant_id == consultant_id)
    )
    exp_count = count_result.scalar_one()
    return await _consultant_to_profile_response(db, consultant, exp_count)


@router.put(
    "/api/consultants/{consultant_id}",
    response_model=ProfileResponse,
    summary="Update consultant profile (admin or assigned recruiter)",
)
async def update_consultant_by_id(
    consultant_id: int,
    payload: ProfileUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN", "RECRUITER")
    if current_user.role == "RECRUITER":
        await _assert_recruiter_mapped(db, current_user.id, consultant_id)

    consultant = await _get_consultant_or_404(db, consultant_id)

    consultant.full_name = payload.fullName
    consultant.current_location = payload.location
    consultant.phone = payload.phone
    consultant.work_authorization = payload.workAuth
    consultant.primary_skills = ", ".join(payload.primarySkills)
    consultant.secondary_skills = ", ".join(payload.secondarySkills)
    consultant.preferred_employment_types = payload.employmentTypes
    consultant.preferred_roles = payload.preferredRoles
    consultant.preferred_locations = payload.preferredLocations
    consultant.total_experience_years = payload.totalExperienceYears

    from resume_router import sync_base_resume_text
    await sync_base_resume_text(db, consultant)

    await db.commit()
    await db.refresh(consultant)

    count_result = await db.execute(
        select(func.count()).where(ConsultantExperience.consultant_id == consultant_id)
    )
    exp_count = count_result.scalar_one()
    return await _consultant_to_profile_response(db, consultant, exp_count)


@router.post(
    "/api/admin/consultants",
    response_model=CreateConsultantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new consultant profile + login (admin only)",
)
async def admin_create_consultant(
    payload: AdminConsultantCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Creates both the login (User, role=CONSULTANT) and the Consultant
    profile, optionally assigning the consultant to a recruiter. Returns a
    one-time temporary password matching AddConsultantDrawer.tsx's expected
    CreateConsultantResponseDTO shape ({ message, temp_password, ... }).
    """
    _require_role(current_user, "ADMIN")

    existing_user = await db.execute(select(User).where(User.email == payload.email))
    if existing_user.scalars().first():
        raise HTTPException(409, f"A user with email '{payload.email}' already exists")
    existing_consultant = await db.execute(select(Consultant).where(Consultant.email == payload.email))
    if existing_consultant.scalars().first():
        raise HTTPException(409, f"Consultant with email '{payload.email}' already exists")

    recruiter_id_int: Optional[int] = None
    if payload.recruiter_id:
        try:
            recruiter_id_int = int(payload.recruiter_id)
        except (TypeError, ValueError):
            raise HTTPException(422, f"Invalid recruiter_id: {payload.recruiter_id}")
        r = await db.execute(
            select(User).where(User.id == recruiter_id_int, User.role == "RECRUITER", User.is_active == True)
        )
        if not r.scalars().first():
            raise HTTPException(404, f"Active recruiter with id={recruiter_id_int} not found")

    temp_password = _generate_temp_password()

    user = User(
        email=payload.email,
        full_name=payload.name,
        role="CONSULTANT",
        is_active=True,
        # resume_info lives on User (not Consultant) — see generate_resume()
        # in resume_router.py, which reads target_user.resume_info to build
        # the AI resume draft. Previously this creation flow had no way to
        # set it at all, so every new consultant started with an empty
        # profile until someone separately edited them via Users → Edit.
        resume_info=payload.resume_info,
    )
    _set_password_on_user(user, _hash_password(temp_password))
    db.add(user)
    await db.flush()  # populate user.id before creating the linked Consultant row

    consultant = Consultant(
        user_id=user.id,
        full_name=payload.name,
        email=payload.email,
        phone=payload.phone,
        work_authorization=payload.work_auth,
        preferred_employment_types=payload.employment_prefs,
        primary_skills=payload.primary_skills or "",
        secondary_skills=payload.secondary_skills or "",
        status="ACTIVE",
        preferred_roles=payload.preferred_roles,
        preferred_locations=payload.preferred_locations,
        current_location=payload.current_location,
        total_experience_years=payload.total_experience_years,
        linkedin_url=payload.linkedin_url,
        education=[e.model_dump() for e in payload.education],
    )
    # availability_status isn't referenced elsewhere in this file, so only
    # set it if the model actually defines that column.
    if hasattr(Consultant, "availability_status"):
        consultant.availability_status = payload.availability_status

    db.add(consultant)
    await db.flush()  # populate consultant.id before the recruiter mapping

    if recruiter_id_int is not None:
        db.add(RecruiterConsultant(
            recruiter_id=recruiter_id_int,
            consultant_id=consultant.id,
            is_active=True,
        ))

    await db.commit()
    await db.refresh(consultant)

    logger.info("Admin %s created consultant id=%s email=%s", current_user.email, consultant.id, consultant.email)

    return CreateConsultantResponse(
        message=f"Consultant '{consultant.full_name}' created successfully.",
        temp_password=temp_password,
        consultant_id=str(consultant.id),
        name=consultant.full_name or "",
        email=consultant.email or "",
    )


@router.patch(
    "/api/admin/consultants/{consultant_id}/deactivate",
    response_model=ProfileResponse,
    summary="Deactivate a consultant (admin only)",
)
async def deactivate_consultant(
    consultant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")
    consultant = await _get_consultant_or_404(db, consultant_id)
    consultant.status = "INACTIVE"
    await db.commit()
    await db.refresh(consultant)
    logger.info("Admin %s deactivated consultant id=%s", current_user.email, consultant_id)
    return await _consultant_to_profile_response(db, consultant)


@router.patch(
    "/api/admin/consultants/{consultant_id}/activate",
    response_model=ProfileResponse,
    summary="Reactivate a consultant (admin only)",
)
async def activate_consultant(
    consultant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")
    consultant = await _get_consultant_or_404(db, consultant_id)
    consultant.status = "ACTIVE"
    await db.commit()
    await db.refresh(consultant)
    return await _consultant_to_profile_response(db, consultant)


# ---------------------------------------------------------------------------
# Resume endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/api/consultant/resume/upload",
    response_model=ResumeUploadResponse,
    summary="Upload base resume — DOCX only, max 10 MB",
)
async def upload_resume(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    CONSULTANT role uploads their own resume.
    Extracts text and detects skills automatically.
    Replaces any previously stored resume.
    """
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    content_type = file.content_type or ""
    if content_type not in ALLOWED_RESUME_TYPES:
        raise HTTPException(400, f"Only DOCX (Word) files are accepted. Got: '{content_type}'")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")
    if len(file_bytes) > MAX_RESUME_BYTES:
        raise HTTPException(413, "File exceeds 10 MB limit")

    # Delete old file if present
    if consultant.base_resume_file_path:
        _delete_file_if_exists(consultant.base_resume_file_path)

    # Store file
    file_path = _save_resume_file(file_bytes, consultant.id, file.filename or "resume", content_type)

    # Extract text (best-effort — never fail the upload)
    extracted_text = _extract_resume_text(file_bytes, content_type)

    # Detect and merge skills
    detected = _detect_skills(extracted_text)
    if detected:
        existing = [s.strip() for s in (consultant.primary_skills or "").split(",") if s.strip()]
        merged = list(dict.fromkeys(existing + detected))
        consultant.primary_skills = ", ".join(merged)

    consultant.base_resume_file_path = file_path
    consultant.base_resume_text = extracted_text
    # A new file replaces the old one's content entirely — clear the
    # structured JSON (base_resume_content) so the Edit Base Resume page's
    # GET /api/resume/base/content backfill re-parses THIS text instead of
    # silently continuing to show whatever was parsed from the previous
    # upload.
    consultant.base_resume_content = None
    await db.commit()
    await db.refresh(consultant)

    logger.info("Resume uploaded consultant_id=%s file=%s skills_detected=%d", consultant.id, file_path, len(detected))

    return ResumeUploadResponse(
        resume={
            "filename": file.filename or Path(file_path).name,
            "uploadedAt": datetime.utcnow().isoformat(),
            "sizeBytes": len(file_bytes),
        }
    )


@router.get(
    "/api/consultant/resume",
    summary="Get own resume metadata",
)
async def get_own_resume(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    if not consultant.base_resume_file_path:
        raise HTTPException(404, "No resume uploaded")

    from s3_service import get_s3_file_metadata
    size_bytes, _content_type = get_s3_file_metadata(consultant.base_resume_file_path)

    return {
        "filename": Path(consultant.base_resume_file_path).name,
        "uploadedAt": consultant.updated_at.isoformat() if consultant.updated_at else None,
        "hasExtractedText": bool(consultant.base_resume_text),
        "extractedTextLength": len(consultant.base_resume_text or ""),
        "sizeBytes": size_bytes or 0,
    }


@router.delete(
    "/api/admin/consultants/{consultant_id}/resume",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete consultant resume (admin only)",
)
async def admin_delete_resume(
    consultant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")
    consultant = await _get_consultant_or_404(db, consultant_id)

    if consultant.base_resume_file_path:
        _delete_file_if_exists(consultant.base_resume_file_path)

    consultant.base_resume_file_path = None
    consultant.base_resume_text = None
    consultant.base_resume_content = None
    await db.commit()


# ---------------------------------------------------------------------------
# Experience endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/api/consultant/experience",
    response_model=List[ExperienceResponse],
    summary="List own experience entries ordered by sortOrder",
)
async def list_own_experience(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    result = await db.execute(
        select(ConsultantExperience)
        .where(ConsultantExperience.consultant_id == consultant.id)
        .order_by(ConsultantExperience.sort_order.asc())
    )
    return [_exp_to_response(e) for e in result.scalars().all()]


@router.post(
    "/api/consultant/experience",
    response_model=ExperienceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add an experience entry",
)
async def create_experience(
    payload: ExperienceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    # BUG FIX: new experiences are meant to always land at the TOP
    # (sort_order 0) — but simply setting sort_order=0 collides with
    # whatever entry already holds that position (e.g. right after a
    # manual drag-reorder reassigns sort_order 0..N to match the new
    # visual order). Two rows tied at sort_order=0 then depend on the
    # database's tie-breaking (typically insertion order/id), which
    # favored the OLDER entry over the genuinely new one — showing the
    # new entry second instead of first. Shift every existing entry down
    # by one first, so sort_order=0 is actually free before the new row
    # claims it — no tie possible.
    await db.execute(
        update(ConsultantExperience)
        .where(ConsultantExperience.consultant_id == consultant.id)
        .values(sort_order=ConsultantExperience.sort_order + 1)
    )

    exp = ConsultantExperience(
        consultant_id=consultant.id,
        client_name=payload.clientName,
        implementation_partner=payload.implementationPartner,
        role_title=payload.roleTitle,
        start_date=date(payload.startDate.year, payload.startDate.month, 1),
        end_date=date(payload.endDate.year, payload.endDate.month, 1) if payload.endDate else None,
        is_present=payload.isPresent,
        location=payload.location,
        work_mode=payload.workMode,
        work_mode_detail=payload.workModeDetail,
        technologies=payload.technologies,
        responsibilities=payload.responsibilities,
        achievements=payload.achievements,
        sort_order=0,
    )
    db.add(exp)
    await db.flush()

    from resume_router import sync_base_resume_text
    await sync_base_resume_text(db, consultant)

    await db.commit()
    await db.refresh(exp)
    return _exp_to_response(exp)


@router.put(
    "/api/consultant/experience/{experience_id}",
    response_model=ExperienceResponse,
    summary="Full update of an experience entry",
)
async def update_experience(
    experience_id: int,
    payload: ExperienceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    result = await db.execute(
        select(ConsultantExperience).where(
            ConsultantExperience.id == experience_id,
            ConsultantExperience.consultant_id == consultant.id,
        )
    )
    exp = result.scalars().first()
    if not exp:
        raise HTTPException(404, "Experience entry not found")

    exp.client_name = payload.clientName
    exp.implementation_partner = payload.implementationPartner
    exp.role_title = payload.roleTitle
    exp.start_date = date(payload.startDate.year, payload.startDate.month, 1)
    exp.end_date = date(payload.endDate.year, payload.endDate.month, 1) if payload.endDate else None
    exp.is_present = payload.isPresent
    exp.location = payload.location
    exp.work_mode = payload.workMode
    exp.work_mode_detail = payload.workModeDetail
    exp.technologies = payload.technologies
    exp.responsibilities = payload.responsibilities
    exp.achievements = payload.achievements
    exp.sort_order = payload.sortOrder

    from resume_router import sync_base_resume_text
    await sync_base_resume_text(db, consultant)

    await db.commit()
    await db.refresh(exp)
    return _exp_to_response(exp)


@router.delete(
    "/api/consultant/experience/{experience_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an experience entry",
)
async def delete_experience(
    experience_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    result = await db.execute(
        select(ConsultantExperience).where(
            ConsultantExperience.id == experience_id,
            ConsultantExperience.consultant_id == consultant.id,
        )
    )
    exp = result.scalars().first()
    if not exp:
        raise HTTPException(404, "Experience entry not found")

        await db.delete(exp)

    from resume_router import sync_base_resume_text
    await sync_base_resume_text(db, consultant)

    await db.commit()


@router.patch(
    "/api/consultant/experience/reorder",
    summary="Save drag-drop sort order — accepts { orderedIds: [str, ...] }",
)
async def reorder_experience(
    payload: ReorderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Reorder experience entries. orderedIds is a list of experience IDs
    in the desired display order. Each entry's sort_order is set to its
    index in the list.
    """
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    for idx, exp_id_str in enumerate(payload.orderedIds):
        try:
            exp_id = int(exp_id_str)
        except (ValueError, TypeError):
            raise HTTPException(422, f"Invalid experience id: {exp_id_str}")

        await db.execute(
            update(ConsultantExperience)
            .where(
                ConsultantExperience.id == exp_id,
                ConsultantExperience.consultant_id == consultant.id,
            )
            .values(sort_order=idx)
        )

    from resume_router import sync_base_resume_text
    await sync_base_resume_text(db, consultant)

    from resume_router import sync_base_resume_text
    await sync_base_resume_text(db, consultant)

    await db.commit()
    return {"message": f"Reordered {len(payload.orderedIds)} entries"}


# ---------------------------------------------------------------------------
# Recruiter ↔ Consultant Mapping endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/api/recruiter/consultants",
    summary="Get consultants assigned to the current recruiter — returns ConsultantDTO[]",
)
async def get_my_consultants(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns ConsultantDTO shape matching frontend recruiter.types.ts:
    { id, name, email, title }
    """
    _require_role(current_user, "RECRUITER", "ADMIN")

    result = await db.execute(
        select(Consultant)
        .join(RecruiterConsultant, RecruiterConsultant.consultant_id == Consultant.id)
        .where(
            RecruiterConsultant.recruiter_id == current_user.id,
            RecruiterConsultant.is_active == True,
        )
        .order_by(Consultant.full_name.asc())
    )
    consultants = result.scalars().all()
    return [
        {
            "id": str(c.id),
            "name": c.full_name or "",
            "email": c.email or "",
            "title": c.preferred_roles.split(",")[0].strip() if c.preferred_roles else "",
        }
        for c in consultants
    ]


@router.post(
    "/api/recruiter/consultants",
    status_code=status.HTTP_201_CREATED,
    summary="Assign a consultant to the current recruiter",
)
async def assign_consultant(
    payload: AssignConsultantRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "RECRUITER", "ADMIN")

    await _get_consultant_or_404(db, payload.consultantId)

    existing_q = await db.execute(
        select(RecruiterConsultant).where(
            RecruiterConsultant.recruiter_id == current_user.id,
            RecruiterConsultant.consultant_id == payload.consultantId,
        )
    )
    existing = existing_q.scalars().first()

    if existing:
        if existing.is_active:
            raise HTTPException(409, "Consultant already assigned to this recruiter")
        existing.is_active = True
        await db.commit()
        await db.refresh(existing)
        return {"id": str(existing.id), "message": "Assignment reactivated"}

    mapping = RecruiterConsultant(
        recruiter_id=current_user.id,
        consultant_id=payload.consultantId,
        is_active=True,
    )
    db.add(mapping)
    await db.commit()
    await db.refresh(mapping)

    logger.info("Recruiter %s assigned consultant_id=%s", current_user.email, payload.consultantId)
    return {"id": str(mapping.id), "message": "Consultant assigned"}


@router.delete(
    "/api/recruiter/consultants/{consultant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unassign a consultant from the current recruiter",
)
async def unassign_consultant(
    consultant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "RECRUITER", "ADMIN")
    await _assert_recruiter_mapped(db, current_user.id, consultant_id)

    await db.execute(
        update(RecruiterConsultant)
        .where(
            RecruiterConsultant.recruiter_id == current_user.id,
            RecruiterConsultant.consultant_id == consultant_id,
        )
        .values(is_active=False)
    )
    await db.commit()
    logger.info("Recruiter %s unassigned consultant_id=%s", current_user.email, consultant_id)


@router.get(
    "/api/admin/consultants/{consultant_id}/recruiters",
    summary="List recruiters assigned to a consultant (admin only)",
)
async def list_recruiters_for_consultant(
    consultant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")
    await _get_consultant_or_404(db, consultant_id)

    result = await db.execute(
        select(RecruiterConsultant, User)
        .join(User, User.id == RecruiterConsultant.recruiter_id)
        .where(RecruiterConsultant.consultant_id == consultant_id)
        .order_by(RecruiterConsultant.is_active.desc(), User.full_name.asc())
    )
    return [
        {
            "mappingId": str(m.id),
            "recruiterId": str(m.recruiter_id),
            "recruiterName": u.full_name,
            "recruiterEmail": u.email,
            "isActive": m.is_active,
            "assignedAt": m.created_at.isoformat() if m.created_at else None,
        }
        for m, u in result.all()
    ]


@router.put(
    "/api/admin/consultants/{consultant_id}/recruiters",
    summary="Replace recruiter assignments for a consultant (admin only)",
)
async def set_recruiters_for_consultant(
    consultant_id: int,
    recruiter_ids: List[int],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")
    consultant = await _get_consultant_or_404(db, consultant_id)

    for rid in recruiter_ids:
        r = await db.execute(
            select(User).where(User.id == rid, User.role == "RECRUITER", User.is_active == True)
        )
        if not r.scalars().first():
            raise HTTPException(404, f"Active recruiter with id={rid} not found")

    # Deactivate all current
    await db.execute(
        update(RecruiterConsultant)
        .where(RecruiterConsultant.consultant_id == consultant_id)
        .values(is_active=False)
    )

    # Activate or create specified ones
    for rid in recruiter_ids:
        existing_q = await db.execute(
            select(RecruiterConsultant).where(
                RecruiterConsultant.recruiter_id == rid,
                RecruiterConsultant.consultant_id == consultant_id,
            )
        )
        existing = existing_q.scalars().first()
        if existing:
            existing.is_active = True
        else:
            db.add(RecruiterConsultant(
                recruiter_id=rid,
                consultant_id=consultant_id,
                is_active=True,
            ))

    await db.commit()

    # Return the fresh assignment list along with the message so the
    # frontend can update immediately instead of waiting on a refetch
    # (and doesn't need to guess at names from raw ids).
    result = await db.execute(
        select(RecruiterConsultant, User)
        .join(User, User.id == RecruiterConsultant.recruiter_id)
        .where(
            RecruiterConsultant.consultant_id == consultant_id,
            RecruiterConsultant.is_active == True,
        )
        .order_by(User.full_name.asc())
    )
    assigned_recruiters = [
        {"id": str(u.id), "name": u.full_name, "email": u.email} for _, u in result.all()
    ]

    consultant_label = consultant.full_name or consultant.email or f"consultant {consultant_id}"
    return {
        "message": f"Updated recruiter assignments for {consultant_label}.",
        "assigned_recruiters": assigned_recruiters,
    }
