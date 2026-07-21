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
from typing import List, Optional

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

ALLOWED_RESUME_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/pdf",
}
MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads/resumes"))

# ---------------------------------------------------------------------------
# Pydantic schemas — matching frontend DTO contracts exactly
# (ConsultantProfileDTO, ExperienceDTO from frontend types/index.ts)
# ---------------------------------------------------------------------------

# ── Profile ─────────────────────────────────────────────────────────────────

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


def _consultant_to_profile_response(c: Consultant, experience_count: int = 0) -> ProfileResponse:
    """Map ORM Consultant → ProfileResponse matching frontend ConsultantProfileDTO."""
    primary = [s.strip() for s in (c.primary_skills or "").split(",") if s.strip()]
    secondary = [s.strip() for s in (c.secondary_skills or "").split(",") if s.strip()]
    emp_types = c.preferred_employment_types or []

    resume = None
    if c.base_resume_file_path:
        fname = Path(c.base_resume_file_path).name
        resume = {
            "filename": fname,
            "uploadedAt": c.updated_at.isoformat() if c.updated_at else datetime.utcnow().isoformat(),
            "sizeBytes": 0,  # size not stored — safe default
        }

    return ProfileResponse(
        id=str(c.id),
        fullName=c.full_name,
        email=c.email,
        location=c.current_location,
        phone=c.phone,
        linkedInUrl=None,  # not in model yet — Phase 8 extension
        primarySkills=primary,
        secondarySkills=secondary,
        workAuth=c.work_authorization,
        employmentTypes=emp_types,
        resume=resume,
        experienceCount=experience_count,
        gmailConnected=c.gmail_connected,
        baseResumeUploaded=bool(c.base_resume_file_path),
        atsScore=float(c.ats_score or 0),
        status=c.status,
        preferredRoles=c.preferred_roles,
        preferredLocations=c.preferred_locations,
        totalExperienceYears=float(c.total_experience_years) if c.total_experience_years is not None else None,
        availabilityStatus=c.availability_status,
        createdAt=c.created_at.isoformat() if c.created_at else None,
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
    """Save file to disk, return relative path."""
    ext_map = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/pdf": ".pdf",
    }
    ext = ext_map.get(content_type, ".bin")
    dest_dir = UPLOAD_DIR / str(consultant_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{uuid.uuid4().hex}{ext}"
    dest_path.write_bytes(file_bytes)
    return str(dest_path)


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
        return "\n".join(parts)
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
        return "\n".join(parts)
    except Exception as exc:
        logger.warning("PDF text extraction failed: %s", exc)
        return ""


def _extract_resume_text(file_bytes: bytes, content_type: str) -> str:
    if "wordprocessingml" in content_type or content_type == "application/docx":
        return _extract_text_from_docx(file_bytes)
    if content_type == "application/pdf":
        return _extract_text_from_pdf(file_bytes)
    return ""


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
    return _consultant_to_profile_response(consultant, exp_count)


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
    consultant.preferred_employment_types = payload.employmentTypes
    consultant.preferred_roles = payload.preferredRoles
    consultant.preferred_locations = payload.preferredLocations
    consultant.total_experience_years = payload.totalExperienceYears

    await db.commit()
    await db.refresh(consultant)

    count_result = await db.execute(
        select(func.count()).where(ConsultantExperience.consultant_id == consultant.id)
    )
    exp_count = count_result.scalar_one()
    return _consultant_to_profile_response(consultant, exp_count)


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

    data = [_consultant_to_profile_response(c) for c in rows]
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
    return _consultant_to_profile_response(consultant, exp_count)


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

    await db.commit()
    await db.refresh(consultant)

    count_result = await db.execute(
        select(func.count()).where(ConsultantExperience.consultant_id == consultant_id)
    )
    exp_count = count_result.scalar_one()
    return _consultant_to_profile_response(consultant, exp_count)


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
    return _consultant_to_profile_response(consultant)


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
    return _consultant_to_profile_response(consultant)


# ---------------------------------------------------------------------------
# Resume endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/api/consultant/resume/upload",
    response_model=ResumeUploadResponse,
    summary="Upload base resume — DOCX or PDF, max 10 MB",
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
        raise HTTPException(400, f"Only DOCX and PDF accepted. Got: '{content_type}'")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")
    if len(file_bytes) > MAX_RESUME_BYTES:
        raise HTTPException(413, "File exceeds 10 MB limit")

    # Delete old object from Spaces if present (best-effort — never block the upload)
    if consultant.base_resume_file_path:
        try:
            delete_file_from_s3(consultant.base_resume_file_path)
        except Exception:
            pass

    # Store the base resume in DigitalOcean Spaces — same object store the Resumes list uses.
    # NOTE: base_resume_file_path now holds the Spaces object KEY, not a local disk path.
    _ext_map = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/pdf": ".pdf",
    }
    ext = _ext_map.get(content_type, ".bin")
    s3_key = f"users/{current_user.id}/base_resume/{uuid.uuid4().hex}{ext}"
    if not upload_file_to_s3(io.BytesIO(file_bytes), s3_key, content_type=content_type):
        raise HTTPException(500, "Failed to store resume in object storage")

    # Extract text (best-effort — never fail the upload)
    extracted_text = _extract_resume_text(file_bytes, content_type)

    # Detect and merge skills
    detected = _detect_skills(extracted_text)
    if detected:
        existing = [s.strip() for s in (consultant.primary_skills or "").split(",") if s.strip()]
        merged = list(dict.fromkeys(existing + detected))
        consultant.primary_skills = ", ".join(merged)

    consultant.base_resume_file_path = s3_key
    consultant.base_resume_text = extracted_text
    await db.commit()
    await db.refresh(consultant)

    logger.info("Resume uploaded consultant_id=%s s3_key=%s skills_detected=%d", consultant.id, s3_key, len(detected))

    return ResumeUploadResponse(
        resume={
            "filename": file.filename or Path(s3_key).name,
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

    return {
        "filename": Path(consultant.base_resume_file_path).name,
        "uploadedAt": consultant.updated_at.isoformat() if consultant.updated_at else None,
        "hasExtractedText": bool(consultant.base_resume_text),
        "extractedTextLength": len(consultant.base_resume_text or ""),
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

    # Next sort order = current max + 1
    max_result = await db.execute(
        select(func.max(ConsultantExperience.sort_order))
        .where(ConsultantExperience.consultant_id == consultant.id)
    )
    next_order = (max_result.scalar_one() or 0) + 1

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
        sort_order=payload.sortOrder,
    )
    db.add(exp)
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
    await _get_consultant_or_404(db, consultant_id)

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
    return {"message": f"Updated recruiter assignments for consultant {consultant_id}"}
