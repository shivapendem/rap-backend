# phase5.py
# ---------------------------------------------------------------------------
# Phase 5 — Consultant and Recruiter Dashboards
#
# Architecture: single flat file in project root, same pattern as phase4.py.
# Reuses get_db, get_current_user from auth.py — no circular dependency.
# Extends existing models — does NOT rewrite phases 1-4.
#
# Endpoints (URLs and response shapes verified against actual frontend
# service files: services/consultantService.ts and lib/api/recruiter.api.ts):
#
#   CONSULTANT DASHBOARD
#   GET  /api/consultant/requirements              paginated, camelCase, flat {data,total,page,pageSize}
#   GET  /api/consultant/applications               consultant's own application history
#   POST /api/consultant/requirements/{id}/apply     submit application (6-check eligibility)
#
#   RECRUITER DASHBOARD
#   GET  /api/recruiter/requirements                 paginated, camelCase, {data, meta:{page,pageSize,total,totalPages}}
#   GET  /api/recruiter/consultants/{id}/requirements paginated ConsultantRequirementDTO rows
#   GET  /api/requirements/{id}/detail               full requirement detail for modal
#
#   SHARED
#   GET  /api/dashboard/stats                role-specific stats
#   POST /api/admin/resumes/generate         trigger resume generation (admin/recruiter)
#
# WebSocket:
#   WS   /api/ws/dashboard                  real-time events via Redis pub/sub
#
# NOTE on enum casing: the two frontend type files disagree on casing for the
# same concepts (types/consultant.ts uses "REMOTE"/"C2C" upper-case; lib/types/
# recruiter.types.ts uses "Remote"/"C2C" title-case for workMode specifically).
# This file outputs the casing each consumer's file actually expects —
# consultant-facing endpoints use the consultant.ts casing, recruiter-facing
# endpoints use the recruiter.types.ts casing.
#
# GET /api/recruiter/consultants is owned by phase3.py — not duplicated here.
# Resume generation pipeline is owned by phase6.py — this file only triggers it.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import math
import asyncio
from datetime import datetime, timezone
from typing import List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from sqlalchemy import func, select, cast, Text, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import (
    User,
    Consultant,
    RecruiterConsultant,
    Requirement,
    RequirementConsultantMatch,
    GeneratedResume,
    Application,
    JobMatch,
)
from auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


# ===========================================================================
# Helpers
# ===========================================================================

def _require_role(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {list(roles)}",
        )


# Validation constants — reuse the same enums already defined on the models
# (Requirement.work_mode and Consultant.preferred_employment_types don't carry
# their own VALID_* sets, so these mirror what phase4.py / models.py already
# accept elsewhere in the codebase, kept here for query-param validation).
_VALID_WORK_MODES = {"REMOTE", "HYBRID", "ONSITE", "TRAVEL_REQUIRED"}
_VALID_EMPLOYMENT_TYPES = {"C2C", "W2", "FULLTIME", "FULL_TIME"}
_VALID_REQUIREMENT_STATUSES = getattr(
    Requirement, "VALID_STATUSES",
    {"NEW", "REVIEWING", "SUBMITTED", "INTERVIEWING", "CLOSED", "REJECTED"},
)
_MAX_SEARCH_LENGTH = 200
_MAX_FILTER_LENGTH = 100


def _validate_work_mode(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    upper = value.upper()
    if upper not in _VALID_WORK_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"work_mode must be one of {sorted(_VALID_WORK_MODES)}, got '{value}'",
        )
    return upper


def _validate_employment_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    upper = value.upper()
    if upper not in _VALID_EMPLOYMENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"employment_type must be one of {sorted(_VALID_EMPLOYMENT_TYPES)}, got '{value}'",
        )
    return upper


def _validate_status(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    upper = value.upper()
    if upper not in _VALID_REQUIREMENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(_VALID_REQUIREMENT_STATUSES)}, got '{value}'",
        )
    return upper


def _validate_search_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > _MAX_SEARCH_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"search must be at most {_MAX_SEARCH_LENGTH} characters",
        )
    return stripped


def _validate_filter_text(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > _MAX_FILTER_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be at most {_MAX_FILTER_LENGTH} characters",
        )
    return stripped


async def _validate_consultant_id_exists(db: AsyncSession, consultant_id: int) -> Consultant:
    if consultant_id <= 0:
        raise HTTPException(status_code=422, detail="consultant_id must be a positive integer")
    result = await db.execute(select(Consultant).where(Consultant.id == consultant_id))
    consultant = result.scalars().first()
    if not consultant:
        raise HTTPException(status_code=404, detail=f"Consultant {consultant_id} not found")
    return consultant


async def _validate_requirement_id_exists(db: AsyncSession, requirement_id: int) -> Requirement:
    if requirement_id <= 0:
        raise HTTPException(status_code=422, detail="requirement_id must be a positive integer")
    result = await db.execute(select(Requirement).where(Requirement.id == requirement_id))
    requirement = result.scalars().first()
    if not requirement:
        raise HTTPException(status_code=404, detail=f"Requirement {requirement_id} not found")
    return requirement


def _validate_vendor_email(value: Optional[str]) -> bool:
    """Returns True if vendor_email looks like a usable email address."""
    if not value or "@" not in value or len(value) > 320:
        return False
    local, _, domain = value.rpartition("@")
    return bool(local) and bool(domain) and "." in domain


# ---------------------------------------------------------------------------
# Enum casing converters — the two frontend type files disagree on casing.
# types/consultant.ts: WorkMode = "REMOTE" | "HYBRID" | "ONSITE" (upper-case)
# lib/types/recruiter.types.ts: WorkMode = "Remote" | "Hybrid" | "Onsite" (title-case)
# ---------------------------------------------------------------------------

_WORK_MODE_TITLE_CASE = {"REMOTE": "Remote", "HYBRID": "Hybrid", "ONSITE": "Onsite", "TRAVEL_REQUIRED": "Hybrid"}


def _work_mode_for_consultant(value: Optional[str]) -> str:
    """consultant.ts expects upper-case REMOTE|HYBRID|ONSITE."""
    if not value:
        return "REMOTE"
    upper = value.upper()
    return upper if upper in ("REMOTE", "HYBRID", "ONSITE") else "REMOTE"


def _work_mode_for_recruiter(value: Optional[str]) -> str:
    """recruiter.types.ts expects title-case Remote|Hybrid|Onsite."""
    if not value:
        return "Remote"
    return _WORK_MODE_TITLE_CASE.get(value.upper(), "Remote")


def _employment_types_for_consultant(values: Optional[List[str]]) -> List[str]:
    """consultant.ts EmploymentType: FULL_TIME | PART_TIME | CONTRACT | C2C | W2."""
    if not values:
        return []
    mapping = {"FULLTIME": "FULL_TIME", "FULL_TIME": "FULL_TIME", "C2C": "C2C", "W2": "W2"}
    return [mapping.get(v.upper(), v.upper()) for v in values]


def _employment_types_for_recruiter(values: Optional[List[str]]) -> List[str]:
    """recruiter.types.ts EmploymentType: C2C | W2 | CONTRACT | FULL TIME | PART TIME."""
    if not values:
        return []
    mapping = {"FULLTIME": "FULL TIME", "FULL_TIME": "FULL TIME", "C2C": "C2C", "W2": "W2"}
    return [mapping.get(v.upper(), v.upper()) for v in values]


def _match_status_to_consultant_requirement_status(match_status: Optional[str]) -> str:
    """consultant.ts RequirementStatus: MATCHED | RESUME_READY | APPLIED | NEEDS_REVIEW."""
    mapping = {
        "ASSIGNED": "MATCHED",
        "RESUME_GENERATED": "RESUME_READY",
        "READY_TO_APPLY": "RESUME_READY",
        "APPLIED": "APPLIED",
        "REJECTED": "NEEDS_REVIEW",
    }
    return mapping.get(match_status or "", "MATCHED")


def _match_status_to_recruiter_requirement_status(match_status: Optional[str], requirement_status: str) -> str:
    """recruiter.types.ts RequirementStatus: New | Matched | Resume Ready | Applied | Needs Review | Rejected."""
    if match_status is None:
        return "New" if requirement_status == "NEW" else "Matched"
    mapping = {
        "ASSIGNED": "Matched",
        "RESUME_GENERATED": "Resume Ready",
        "READY_TO_APPLY": "Resume Ready",
        "APPLIED": "Applied",
        "REJECTED": "Rejected",
    }
    return mapping.get(match_status, "Matched")


def _parse_vendor_contact(raw: Optional[str]) -> dict:
    """
    requirements.vendor_contact is stored as free text (e.g. a name, or
    'Jane Doe <jane@vendor.com> 555-1234'). recruiter.types.ts expects a
    structured {name, email, phone} object. Best-effort parse; unknown
    parts default to empty string rather than inventing data.
    """
    if not raw:
        return {"name": "", "email": "", "phone": ""}
    email = ""
    if "<" in raw and ">" in raw:
        email = raw.split("<", 1)[1].split(">", 1)[0].strip()
        name = raw.split("<", 1)[0].strip()
    elif "@" in raw:
        # raw might just be an email, or "email phone"
        parts = raw.split()
        email = next((p for p in parts if "@" in p), "")
        name = ""
    else:
        name = raw.strip()
    phone = ""
    for token in raw.replace(",", " ").split():
        digits = "".join(c for c in token if c.isdigit())
        if len(digits) >= 7:
            phone = token
            break
    return {"name": name, "email": email, "phone": phone}


async def _get_consultant_for_user(db: AsyncSession, user: User) -> Consultant:
    result = await db.execute(select(Consultant).where(Consultant.user_id == user.id))
    consultant = result.scalars().first()
    if not consultant:
        raise HTTPException(status_code=404, detail="Consultant profile not found for this user")
    return consultant


async def _check_apply_eligibility(
    db: AsyncSession,
    user: User,
    requirement: Requirement,
    selected_consultant_id: Optional[int] = None,
    *,
    preloaded_consultant: Optional[Consultant] = None,
    preloaded_match: Optional[RequirementConsultantMatch] = "_unset",
    preloaded_has_sent_application: Optional[bool] = None,
) -> tuple[bool, str]:
    """
    Phase 5 spec — 6 eligibility checks for Apply button visibility.
    Returns (can_apply: bool, reason: str)

    PERFORMANCE: callers iterating many rows (dashboards) should pass
    preloaded_consultant / preloaded_match / preloaded_has_sent_application
    so this function does zero additional queries per row. Single-row callers
    (the apply endpoint itself) can omit them and it falls back to querying,
    matching the original per-call behaviour for that single check.
    """
    if preloaded_consultant is not None:
        consultant = preloaded_consultant
    elif user.role == "CONSULTANT":
        consultant = await _get_consultant_for_user(db, user)
    elif user.role in ("RECRUITER", "ADMIN"):
        if selected_consultant_id is None:
            return False, "Select a consultant first"
        if user.role == "RECRUITER":
            rc = (await db.execute(
                select(RecruiterConsultant).where(
                    RecruiterConsultant.recruiter_id == user.id,
                    RecruiterConsultant.consultant_id == selected_consultant_id,
                    RecruiterConsultant.is_active == True,
                )
            )).scalars().first()
            if not rc:
                return False, "Consultant is not assigned to this recruiter"
        result = await db.execute(select(Consultant).where(Consultant.id == selected_consultant_id))
        consultant = result.scalars().first()
        if not consultant:
            return False, "Consultant not found"
    else:
        return False, "Invalid role"

    # Check 1: Requirement assigned to consultant
    if preloaded_match != "_unset":
        match = preloaded_match
    else:
        match = (await db.execute(
            select(RequirementConsultantMatch).where(
                RequirementConsultantMatch.requirement_id == requirement.id,
                RequirementConsultantMatch.consultant_id == consultant.id,
            )
        )).scalars().first()
    if not match:
        return False, "Requirement is not assigned to this consultant"

    # Check 2: Not already applied
    if preloaded_has_sent_application is not None:
        already_applied = preloaded_has_sent_application
    else:
        existing_app = (await db.execute(
            select(Application).where(
                Application.requirement_id == requirement.id,
                Application.consultant_id == consultant.id,
                Application.status == "SENT",
            )
        )).scalars().first()
        already_applied = existing_app is not None
    if already_applied:
        return False, "Already applied"

    # Check 3: Vendor email exists and is well-formed
    if not _validate_vendor_email(requirement.vendor_email):
        return False, "Vendor email is missing or invalid"

    # Check 4: Gmail connected
    if not consultant.gmail_connected:
        return False, "Consultant Gmail is not connected"

    # Check 5: Base resume uploaded
    if not consultant.base_resume_file_path:
        return False, "Base resume is missing"

    return True, "Allowed"


# ===========================================================================
# Pydantic schemas
# ===========================================================================

class RequirementDetailResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    role: str
    vendor: Optional[str] = None
    vendor_email: Optional[str] = None
    client: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[str] = None
    employment_types: Optional[List[str]] = None
    rate: Optional[str] = None
    duration: Optional[str] = None
    job_description: Optional[str] = None
    status: str
    received_date: Optional[str] = None
    parse_confidence: Optional[float] = None


class GeneratedResumeDTO(BaseModel):
    id: str
    pdf_url: Optional[str] = None
    ats_score: Optional[float] = None
    is_final: bool
    generation_status: str


class ConsultantDashboardRow(BaseModel):
    """One row in the consultant dashboard table."""
    requirement_id: str
    role: str
    vendor: Optional[str] = None
    client: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[str] = None
    employment_types: Optional[List[str]] = None
    rate: Optional[str] = None
    status: str
    match_score: float
    match_status: str
    matched_skills: List[str] = []
    missing_skills: List[str] = []
    received_date: Optional[str] = None
    # Resume column
    generated_resume: Optional[GeneratedResumeDTO] = None
    # Apply button
    can_apply: bool = False
    apply_reason: str = ""


class PaginatedConsultantDashboard(BaseModel):
    data: List[ConsultantDashboardRow]
    total: int
    page: int
    page_size: int
    total_pages: int


class RecruiterDashboardRow(BaseModel):
    """One row in the recruiter dashboard table."""
    requirement_id: str
    role: str
    vendor: Optional[str] = None
    client: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[str] = None
    employment_types: Optional[List[str]] = None
    rate: Optional[str] = None
    status: str
    received_date: Optional[str] = None
    ats_match_count: int = 0
    # Only present when consultant_id filter is active
    match_score: Optional[float] = None
    match_status: Optional[str] = None
    generated_resume: Optional[GeneratedResumeDTO] = None
    can_apply: bool = False
    apply_reason: str = ""


class PaginatedRecruiterDashboard(BaseModel):
    data: List[RecruiterDashboardRow]
    total: int
    page: int
    page_size: int
    total_pages: int


class ConsultantDropdownItem(BaseModel):
    id: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None


class ApplicationHistoryRow(BaseModel):
    application_id: str
    requirement_id: str
    role: str
    vendor: Optional[str] = None
    vendor_email: Optional[str] = None
    application_status: str
    sent_at: Optional[str] = None
    resume_url: Optional[str] = None
    ats_score: Optional[float] = None
    # BUG FIX: resume_url comes from GeneratedResume.pdf_url, a real column
    # that is never actually written anywhere in this codebase — always
    # None. resume_id (used for the authenticated download endpoint, same
    # pattern as the admin Applications Tracker) is the field that
    # actually works.
    resume_id: Optional[str] = None
    resume_available: bool = False
    # BUG FIX: client was never returned (blank UI column); sent_by fields
    # let a consultant see whether they applied themselves or a
    # recruiter/admin sent it on their behalf.
    client: Optional[str] = None
    gmail_message_id: Optional[str] = None
    sent_by_name: Optional[str] = None
    sent_by_role: Optional[str] = None
    # Real filename, so the frontend can show "John_Doe_Resume.docx"
    # instead of a generic "PDF" label that's wrong for non-PDF uploads.
    # Falls back to the raw attachment path's basename for applications
    # sent via the email-queue/Apply-to-Requirement flow, which never
    # get a GeneratedResume row at all.
    resume_filename: Optional[str] = None


class PaginatedApplicationHistory(BaseModel):
    data: List[ApplicationHistoryRow]
    total: int
    page: int
    page_size: int
    total_pages: int


class RecruiterApplicationRow(BaseModel):
    """One row in the recruiter's cross-consultant applications view."""
    application_id: str
    consultant_name: Optional[str] = None
    consultant_id: str
    requirement_id: str
    role: str
    vendor: Optional[str] = None
    vendor_email: Optional[str] = None
    status: str
    sent_at: Optional[str] = None
    # BUG FIX: client, gmail_message_id, sent_by, and resume were all
    # missing — same UI gaps as the admin Applications Tracker.
    client: Optional[str] = None
    gmail_message_id: Optional[str] = None
    resume_id: Optional[str] = None
    resume_available: bool = False
    sent_by_name: Optional[str] = None
    sent_by_role: Optional[str] = None


class PaginatedRecruiterApplications(BaseModel):
    data: List[RecruiterApplicationRow]
    total: int
    page: int
    page_size: int
    total_pages: int


class ApplyResponse(BaseModel):
    success: bool
    message: str
    application_id: Optional[str] = None


class GenerateResumeRequest(BaseModel):
    consultant_id: int
    requirement_id: int


class GenerateResumeResponse(BaseModel):
    success: bool
    message: str
    resume_id: Optional[str] = None
    generation_status: str


class DashboardStatsResponse(BaseModel):
    role: str
    # Consultant-specific
    total_assigned: Optional[int] = None
    total_applied: Optional[int] = None
    total_resumes_generated: Optional[int] = None
    # Recruiter-specific
    total_requirements: Optional[int] = None
    total_consultants: Optional[int] = None
    total_applications_sent: Optional[int] = None


# ---------------------------------------------------------------------------
# Frontend-matching schemas — exact shapes from types/consultant.ts and
# lib/types/recruiter.types.ts, verified against the actual frontend files.
# ---------------------------------------------------------------------------

class ResumeInfoDTO(BaseModel):
    """consultant.ts ResumeInfo."""
    generated: bool
    viewUrl: Optional[str] = None
    downloadPdfUrl: Optional[str] = None
    downloadDocxUrl: Optional[str] = None
    atsScore: Optional[float] = None
    # "generated" = tailored GeneratedResume (auto, ATS-scored against the
    # requirement's JD). "manual" = a Resume the consultant authored
    # themselves via the no-JD custom flow, linked by requirement_id.
    sourceType: Optional[str] = None
    resumeId: Optional[str] = None


class ConsultantRequirementResponse(BaseModel):
    """consultant.ts RequirementDTO — used by GET /api/consultant/requirements."""
    id: str
    role: str
    vendor: str
    client: str
    location: str
    workMode: str
    employmentTypes: List[str]
    matchScore: float
    status: str
    resume: ResumeInfoDTO
    vendorEmail: Optional[str] = None
    assignmentExists: bool
    appliedAt: Optional[str] = None
    skills: List[str] = []
    experience: Optional[str] = None
    # True when this requirement has no job_description at all — the
    # tailored GeneratedResume pipeline needs a JD to work from, so for
    # these the dashboard offers the manual custom-resume flow instead.
    hasJobDescription: bool = True


class ConsultantRequirementsListResponse(BaseModel):
    """consultant.ts PaginatedResponse<RequirementDTO> — flat, no meta wrapper."""
    data: List[ConsultantRequirementResponse]
    total: int
    page: int
    pageSize: int


class VendorContactDTO(BaseModel):
    name: str
    email: str
    phone: str


def _coerce_skills_list(value) -> list:
    """
    parsed_fields['skills'] should be a List[str] (see parser.py's
    extract_skills), but requirements parsed before that fix — or any
    future bad data — may still have it stored as a raw comma-separated
    string. RecruiterParsedFieldsDTO.skills is strictly List[str], and a
    single bad row fails Pydantic response validation for the ENTIRE
    /api/recruiter/requirements list (not just that row), so this
    normalizes defensively rather than trusting what's in the DB.
    """
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value.strip():
        return [s.strip() for s in value.split(",") if s.strip()]
    return []


class RecruiterParsedFieldsDTO(BaseModel):
    experience: str = ""
    skills: List[str] = []
    education: str = ""
    budget: str = ""


class RecruiterRequirementResponse(BaseModel):
    """recruiter.types.ts RequirementDTO — used by GET /api/recruiter/requirements."""
    id: str
    role: str
    vendor: str
    client: str
    location: str
    employmentTypes: List[str]
    workMode: str
    receivedDate: str
    status: str
    jobDescription: str
    parsedFields: RecruiterParsedFieldsDTO
    vendorContact: VendorContactDTO
    # Consultants (from this recruiter's own roster) matched to this
    # requirement — batch-loaded per page, not per row, to avoid N+1
    # queries. Empty list if none matched yet.
    matchedConsultants: List[str] = []


class ConsultantFilteredRequirementResponse(RecruiterRequirementResponse):
    """recruiter.types.ts ConsultantRequirementDTO — extends RequirementDTO."""
    matchScore: float
    matchedSkills: List[str]
    missingSkills: List[str]
    matchReason: str
    resumeStatus: str  # "ready" | "not_generated"
    alreadyApplied: bool
    gmailConnected: bool
    atsScore: float


class PaginationMetaDTO(BaseModel):
    page: int
    pageSize: int
    total: int
    totalPages: int


class RecruiterRequirementsListResponse(BaseModel):
    """recruiter.types.ts PaginatedResponse<RequirementDTO> — meta wrapper."""
    data: List[RecruiterRequirementResponse]
    meta: PaginationMetaDTO


class ConsultantFilteredRequirementsListResponse(BaseModel):
    """recruiter.types.ts PaginatedResponse<ConsultantRequirementDTO> — meta wrapper."""
    data: List[ConsultantFilteredRequirementResponse]
    meta: PaginationMetaDTO


# ===========================================================================
# Task 1: Consultant Dashboard API
# ===========================================================================

@router.get(
    "/api/consultant/requirements",
    response_model=ConsultantRequirementsListResponse,
    summary="Consultant dashboard — assigned requirements with resume + apply eligibility",
    tags=["Phase5 - Consultant Dashboard"],
)
async def get_consultant_requirements(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    roleKeyword: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    workMode: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    employmentTypes: Optional[List[str]] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    URL and response shape verified against services/consultantService.ts
    fetchRequirementsReal() and types/consultant.ts RequirementDTO /
    PaginatedResponse<T> (flat, no meta wrapper) / RequirementsQueryParams
    (roleKeyword, workMode, status — not role/location/employment_type).

    PERFORMANCE: applications for this consultant are batch-loaded once
    before the row loop (1 query) instead of once per row, eliminating
    the N+1 pattern in eligibility checking. match is already available
    per row from the outer JOIN, so eligibility re-querying it is skipped too.
    """
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    # Validate filter inputs
    roleKeyword = _validate_filter_text(roleKeyword, "roleKeyword")
    work_mode_filter = _validate_work_mode(workMode)
    status_filter = status  # frontend status enum (MATCHED/RESUME_READY/...) maps to match.status below, validated there if provided

    base_q = (
        select(RequirementConsultantMatch, Requirement, GeneratedResume)
        .join(Requirement, Requirement.id == RequirementConsultantMatch.requirement_id)
        .outerjoin(
            GeneratedResume,
            (GeneratedResume.requirement_id == RequirementConsultantMatch.requirement_id)
            & (GeneratedResume.consultant_id == RequirementConsultantMatch.consultant_id)
            & (GeneratedResume.is_final == True),
        )
    )
    filters = [RequirementConsultantMatch.consultant_id == consultant.id]
    if roleKeyword:
        filters.append(Requirement.role.ilike(f"%{roleKeyword}%"))
    if search:
        search_term = f"%{search}%"
        filters.append(or_(
            Requirement.role.ilike(search_term),
            Requirement.vendor_email.ilike(search_term)
        ))
    if work_mode_filter:
        filters.append(Requirement.work_mode == work_mode_filter)

    base_q = base_q.where(and_(*filters))

    count_q = select(func.count()).select_from(
        select(RequirementConsultantMatch.id)
        .join(Requirement, Requirement.id == RequirementConsultantMatch.requirement_id)
        .where(and_(*filters))
        .subquery()
    )
    total = (await db.execute(count_q)).scalar_one()

    results = (
        await db.execute(
            base_q.order_by(Requirement.received_date.desc().nullslast())
            .offset((page - 1) * pageSize)
            .limit(pageSize)
        )
    ).all()

    # Batch-load: which requirement_ids has this consultant already SENT an
    # application for? One query for the whole page instead of one per row.
    page_requirement_ids = [req.id for _, req, _ in results]
    sent_application_req_ids: set[int] = set()
    if page_requirement_ids:
        sent_rows = (await db.execute(
            select(Application.requirement_id).where(
                Application.consultant_id == consultant.id,
                Application.requirement_id.in_(page_requirement_ids),
                Application.status == "SENT",
            )
        )).scalars().all()
        sent_application_req_ids = set(sent_rows)

    # Batch-load sent_at for appliedAt field
    sent_at_by_req: dict[int, str] = {}
    if page_requirement_ids:
        sent_at_rows = (await db.execute(
            select(Application.requirement_id, Application.sent_at).where(
                Application.consultant_id == consultant.id,
                Application.requirement_id.in_(page_requirement_ids),
                Application.status == "SENT",
            )
        )).all()
        sent_at_by_req = {rid: sent_at.isoformat() if sent_at else None for rid, sent_at in sent_at_rows}

    # Batch-load manually-authored resumes for JD-less requirements — the
    # tailored GeneratedResume pipeline needs a real job_description to
    # tailor against, so requirements without one are satisfied by a
    # Resume the consultant wrote themselves via the custom-resume flow,
    # linked back here via Resume.requirement_id.
    manual_resume_by_req: dict[int, "Resume"] = {}
    if page_requirement_ids:
        from models import Resume
        manual_rows = (await db.execute(
            select(Resume).where(
                Resume.user_id == current_user.id,
                Resume.requirement_id.in_(page_requirement_ids),
                Resume.status == "completed",
            )
        )).scalars().all()
        for r in manual_rows:
            manual_resume_by_req[r.requirement_id] = r

    rows: List[ConsultantRequirementResponse] = []
    for match, req, resume in results:
        frontend_status = _match_status_to_consultant_requirement_status(match.status)
        if status_filter and frontend_status != status_filter:
            continue

        already_applied = req.id in sent_application_req_ids
        resume_generated = bool(resume and resume.generation_status == "COMPLETED")
        has_job_description = bool(req.job_description and req.job_description.strip())
        # BUG FIX: this used to hide an already-created manual resume the
        # moment a JD later appeared on the requirement (e.g. an admin
        # edits it, or a reparse finds one) — the consultant's completed
        # work would vanish and Apply would re-lock. Once a manual resume
        # exists for this requirement, it stays valid regardless of the
        # requirement's current JD state; has_job_description should only
        # decide which "Generate" button to offer when NO resume exists
        # at all yet, never whether an existing one is hidden.
        manual_resume = manual_resume_by_req.get(req.id)

        if resume_generated:
            resume_dto = ResumeInfoDTO(
                generated=True,
                viewUrl=f"/api/consultant/requirements/{req.id}/resume",
                downloadPdfUrl=resume.pdf_url,
                downloadDocxUrl=f"/api/consultant/requirements/{req.id}/resume/download/docx",
                atsScore=float(resume.ats_score) if resume.ats_score is not None else None,
                sourceType="generated",
                resumeId=None,
            )
        elif manual_resume:
            resume_dto = ResumeInfoDTO(
                generated=True,
                viewUrl=f"/api/resume/{manual_resume.id}",
                downloadPdfUrl=f"/api/resume/{manual_resume.id}/download/file",
                downloadDocxUrl=f"/api/resume/{manual_resume.id}/download/file/docx",
                # Deliberately always null, even though Resume.ats_score may
                # have a real computed value. This is the whole point of the
                # no-JD manual flow — there's no real job description to
                # meaningfully score against, so holding it to the same >=80
                # bar as an auto-tailored resume would strand consultants on
                # "Resume Not Ready" for doing exactly what was asked of
                # them. The Apply gate already treats null as "not scored,
                # don't block" (see ApplyButton.tsx resolveApplyState).
                atsScore=None,
                sourceType="manual",
                resumeId=str(manual_resume.id),
            )
        else:
            resume_dto = ResumeInfoDTO(generated=False)

        rows.append(ConsultantRequirementResponse(
            id=str(req.id),
            role=req.role,
            vendor=req.vendor or "Unknown Vendor",
            client=req.client or "Unknown Client",
            location=req.location or "Not Specified",
            workMode=_work_mode_for_consultant(req.work_mode),
            employmentTypes=_employment_types_for_consultant(req.employment_types),
            matchScore=float(match.match_score),
            status=frontend_status,
            resume=resume_dto,
            vendorEmail=req.vendor_email,
            assignmentExists=True,
            appliedAt=sent_at_by_req.get(req.id),
            skills=_coerce_skills_list((req.parsed_fields or {}).get("skills")),
            experience=(req.parsed_fields or {}).get("experience") or None,
            hasJobDescription=has_job_description,
        ))

    return ConsultantRequirementsListResponse(
        data=rows, total=total, page=page, pageSize=pageSize,
    )


# ===========================================================================
# Task 2: Recruiter Dashboard API
# ===========================================================================

@router.get(
    "/api/recruiter/requirements",
    response_model=RecruiterRequirementsListResponse,
    summary="Recruiter dashboard — all requirements, unfiltered",
    tags=["Phase5 - Recruiter Dashboard"],
)
async def get_all_requirements_for_recruiter(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    sortField: Optional[str] = Query(None),
    sortDir: Optional[str] = Query(None),
    roleSearch: Optional[str] = Query(None),
    experienceSearch: Optional[str] = Query(None),
    skillsSearch: Optional[str] = Query(None),
    workMode: Optional[str] = Query(None),
    dateFrom: Optional[str] = Query(None),
    dateTo: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    URL, query params, and response shape verified against
    lib/api/recruiter.api.ts fetchAllRequirementsAPI() and
    lib/types/recruiter.types.ts RequirementDTO / PaginatedResponse<T>
    (meta wrapper) / RequirementFilterParams.

    No apply-eligibility data here — this is the default unfiltered view;
    eligibility only applies once a consultant is selected, which is the
    separate GET /api/recruiter/consultants/{id}/requirements endpoint below.
    """
    _require_role(current_user, "RECRUITER", "ADMIN")

    roleSearch = _validate_filter_text(roleSearch, "roleSearch")
    experienceSearch = _validate_filter_text(experienceSearch, "experienceSearch")
    skillsSearch = _validate_filter_text(skillsSearch, "skillsSearch")
    work_mode_filter = _validate_work_mode(workMode)

    base_q = select(Requirement)
    count_q = select(func.count(Requirement.id))

    if roleSearch:
        base_q = base_q.where(Requirement.role.ilike(f"%{roleSearch}%"))
        count_q = count_q.where(Requirement.role.ilike(f"%{roleSearch}%"))
    # experience/skills live inside parsed_fields (JSON) rather than their
    # own columns, so match against the JSON blob cast to text — portable
    # across Postgres and the SQLite dev fallback, unlike JSONB path ops.
    if experienceSearch:
        base_q = base_q.where(cast(Requirement.parsed_fields, Text).ilike(f"%{experienceSearch}%"))
        count_q = count_q.where(cast(Requirement.parsed_fields, Text).ilike(f"%{experienceSearch}%"))
    if skillsSearch:
        base_q = base_q.where(cast(Requirement.parsed_fields, Text).ilike(f"%{skillsSearch}%"))
        count_q = count_q.where(cast(Requirement.parsed_fields, Text).ilike(f"%{skillsSearch}%"))
    if work_mode_filter:
        base_q = base_q.where(Requirement.work_mode == work_mode_filter)
        count_q = count_q.where(Requirement.work_mode == work_mode_filter)
    # dateFrom/dateTo/status were accepted as query params but never applied
    # to the query below — the FilterBar's date range and status filters had
    # no effect at all. Applying them now.
    if dateFrom:
        try:
            df = datetime.fromisoformat(dateFrom).replace(hour=0, minute=0, second=0, microsecond=0)
            base_q = base_q.where(Requirement.received_date >= df)
            count_q = count_q.where(Requirement.received_date >= df)
        except ValueError:
            raise HTTPException(status_code=422, detail="dateFrom must be an ISO date (YYYY-MM-DD)")
    if dateTo:
        try:
            dt = datetime.fromisoformat(dateTo).replace(hour=23, minute=59, second=59, microsecond=999999)
            base_q = base_q.where(Requirement.received_date <= dt)
            count_q = count_q.where(Requirement.received_date <= dt)
        except ValueError:
            raise HTTPException(status_code=422, detail="dateTo must be an ISO date (YYYY-MM-DD)")
    if status:
        # This endpoint never joins RequirementConsultantMatch, so the
        # displayed status (_match_status_to_recruiter_requirement_status
        # called with match_status=None below) can only ever resolve to
        # "New" or "Matched" — mirror that same two-way split here rather
        # than filtering on the raw NEW/REVIEWING/SUBMITTED/... DB values,
        # which the frontend never sends.
        if status == "New":
            base_q = base_q.where(Requirement.status == "NEW")
            count_q = count_q.where(Requirement.status == "NEW")
        elif status == "Matched":
            base_q = base_q.where(Requirement.status != "NEW")
            count_q = count_q.where(Requirement.status != "NEW")
        else:
            # "Resume Ready" / "Applied" / "Needs Review" / "Rejected" can't
            # occur on this unfiltered list (those only exist once a
            # consultant match exists) — correctly yields zero rows rather
            # than silently ignoring the filter and showing everything.
            base_q = base_q.where(False)
            count_q = count_q.where(False)

    allowed_sort = {"received_date", "role", "vendor", "client", "status", "created_at"}
    sort_col_name = sortField if sortField in allowed_sort else "received_date"
    sort_col = getattr(Requirement, sort_col_name)
    base_q = base_q.order_by(sort_col.desc() if sortDir != "asc" else sort_col.asc())

    total = (await db.execute(count_q)).scalar_one()
    results = (
        await db.execute(base_q.offset((page - 1) * pageSize).limit(pageSize))
    ).scalars().all()

    # Batch-load matched consultant names for every requirement on this
    # page in one query, scoped to the recruiter's own active roster (ADMIN
    # sees matches from any consultant). Avoids an N+1 query per row.
    req_ids = [r.id for r in results]
    matches_by_req: dict[int, list[str]] = {}
    if req_ids:
        matches_q = (
            select(RequirementConsultantMatch.requirement_id, Consultant.full_name)
            .join(Consultant, Consultant.id == RequirementConsultantMatch.consultant_id)
            .join(User, User.id == Consultant.user_id)
            .where(
                RequirementConsultantMatch.requirement_id.in_(req_ids),
                Consultant.status == "ACTIVE",
                User.is_authorized == True,
            )
        )
        if current_user.role == "RECRUITER":
            assigned_result = await db.execute(
                select(RecruiterConsultant.consultant_id).where(
                    RecruiterConsultant.recruiter_id == current_user.id,
                    RecruiterConsultant.is_active == True,
                )
            )
            assigned_ids = [row[0] for row in assigned_result.all()]
            matches_q = matches_q.where(RequirementConsultantMatch.consultant_id.in_(assigned_ids))

        for req_id, name in (await db.execute(matches_q)).all():
            matches_by_req.setdefault(req_id, []).append(name)

    data = [
        RecruiterRequirementResponse(
            id=str(r.id),
            role=r.role,
            vendor=r.vendor or "",
            client=r.client or "",
            location=r.location or "",
            employmentTypes=_employment_types_for_recruiter(r.employment_types),
            workMode=_work_mode_for_recruiter(r.work_mode),
            receivedDate=r.received_date.isoformat() if r.received_date else "",
            status=_match_status_to_recruiter_requirement_status(None, r.status),
            jobDescription=r.job_description or "",
            parsedFields=RecruiterParsedFieldsDTO(
                experience=str((r.parsed_fields or {}).get("experience", "")) if r.parsed_fields else "",
                skills=_coerce_skills_list((r.parsed_fields or {}).get("skills")) if r.parsed_fields else [],
                education=str((r.parsed_fields or {}).get("education", "")) if r.parsed_fields else "",
                budget=r.rate or "",
            ),
            vendorContact=VendorContactDTO(**_parse_vendor_contact(r.vendor_contact)),
            matchedConsultants=matches_by_req.get(r.id, []),
        )
        for r in results
    ]

    return RecruiterRequirementsListResponse(
        data=data,
        meta=PaginationMetaDTO(
            page=page, pageSize=pageSize, total=total,
            totalPages=math.ceil(total / pageSize) if total else 1,
        ),
    )


@router.get(
    "/api/recruiter/consultants/{consultant_id}/requirements",
    response_model=ConsultantFilteredRequirementsListResponse,
    summary="Recruiter dashboard — requirements assigned to one consultant, with apply eligibility",
    tags=["Phase5 - Recruiter Dashboard"],
)
async def get_consultant_requirements_for_recruiter(
    consultant_id: int,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    roleSearch: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    experienceSearch: Optional[str] = Query(None),
    skillsSearch: Optional[str] = Query(None),
    workMode: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    URL and response shape verified against lib/api/recruiter.api.ts
    fetchConsultantRequirementsAPI() and recruiter.types.ts
    ConsultantRequirementDTO / PaginatedResponse<T> (meta wrapper).

    PERFORMANCE: applications for this consultant are batch-loaded once
    for the page (1 query) instead of once per row inside the eligibility check.
    """
    _require_role(current_user, "RECRUITER", "ADMIN")

    consultant = await _validate_consultant_id_exists(db, consultant_id)

    if current_user.role == "RECRUITER":
        rc = (await db.execute(
            select(RecruiterConsultant).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.consultant_id == consultant_id,
                RecruiterConsultant.is_active == True,
            )
        )).scalars().first()
        if not rc:
            raise HTTPException(status_code=403, detail="Consultant not assigned to this recruiter")

    roleSearch = _validate_filter_text(roleSearch, "roleSearch")
    experienceSearch = _validate_filter_text(experienceSearch, "experienceSearch")
    skillsSearch = _validate_filter_text(skillsSearch, "skillsSearch")
    work_mode_filter = _validate_work_mode(workMode)

    base_q = (
        select(Requirement, RequirementConsultantMatch, GeneratedResume)
        .join(RequirementConsultantMatch, RequirementConsultantMatch.requirement_id == Requirement.id)
        .outerjoin(
            GeneratedResume,
            (GeneratedResume.requirement_id == Requirement.id)
            & (GeneratedResume.consultant_id == consultant_id)
            & (GeneratedResume.is_final == True),
        )
    )
    filters = [RequirementConsultantMatch.consultant_id == consultant_id]
    if roleSearch:
        filters.append(Requirement.role.ilike(f"%{roleSearch}%"))
    if search:
        search_term = f"%{search}%"
        filters.append(or_(
            Requirement.role.ilike(search_term),
            Requirement.vendor_email.ilike(search_term)
        ))
    if experienceSearch:
        filters.append(cast(Requirement.parsed_fields, Text).ilike(f"%{experienceSearch}%"))
    if skillsSearch:
        filters.append(cast(Requirement.parsed_fields, Text).ilike(f"%{skillsSearch}%"))
    if work_mode_filter:
        filters.append(Requirement.work_mode == work_mode_filter)

    base_q = base_q.where(and_(*filters))

    count_q = select(func.count()).select_from(
        select(RequirementConsultantMatch.id)
        .join(Requirement, Requirement.id == RequirementConsultantMatch.requirement_id)
        .where(and_(*filters))
        .subquery()
    )
    total = (await db.execute(count_q)).scalar_one()

    results = (
        await db.execute(
            base_q.order_by(Requirement.received_date.desc().nullslast())
            .offset((page - 1) * pageSize).limit(pageSize)
        )
    ).all()

    # Batch-load sent applications for this page — eliminates the per-row
    # Application query inside _check_apply_eligibility.
    page_requirement_ids = [req.id for req, _, _ in results]
    sent_application_req_ids: set[int] = set()
    if page_requirement_ids:
        sent_rows = (await db.execute(
            select(Application.requirement_id).where(
                Application.consultant_id == consultant_id,
                Application.requirement_id.in_(page_requirement_ids),
                Application.status == "SENT",
            )
        )).scalars().all()
        sent_application_req_ids = set(sent_rows)

    data: List[ConsultantFilteredRequirementResponse] = []
    for req, match, resume in results:
        can_apply, _reason = await _check_apply_eligibility(
            db, current_user, req,
            selected_consultant_id=consultant_id,
            preloaded_consultant=consultant,
            preloaded_match=match,
            preloaded_has_sent_application=req.id in sent_application_req_ids,
        )
        resume_status = "ready" if (resume and resume.generation_status == "COMPLETED") else "not_generated"

        data.append(ConsultantFilteredRequirementResponse(
            id=str(req.id),
            role=req.role,
            vendor=req.vendor or "",
            client=req.client or "",
            location=req.location or "",
            employmentTypes=_employment_types_for_recruiter(req.employment_types),
            workMode=_work_mode_for_recruiter(req.work_mode),
            receivedDate=req.received_date.isoformat() if req.received_date else "",
            status=_match_status_to_recruiter_requirement_status(match.status, req.status),
            jobDescription=req.job_description or "",
            parsedFields=RecruiterParsedFieldsDTO(
                experience=str((req.parsed_fields or {}).get("experience", "")) if req.parsed_fields else "",
                skills=_coerce_skills_list((req.parsed_fields or {}).get("skills")) if req.parsed_fields else [],
                education=str((req.parsed_fields or {}).get("education", "")) if req.parsed_fields else "",
                budget=req.rate or "",
            ),
            vendorContact=VendorContactDTO(**_parse_vendor_contact(req.vendor_contact)),
            matchScore=float(match.match_score),
            matchedSkills=match.matched_skills or [],
            missingSkills=match.missing_skills or [],
            matchReason=match.match_reason or "",
            resumeStatus=resume_status,
            alreadyApplied=req.id in sent_application_req_ids,
            gmailConnected=consultant.gmail_connected,
            atsScore=float(resume.ats_score) if resume and resume.ats_score is not None else 0.0,
        ))

    return ConsultantFilteredRequirementsListResponse(
        data=data,
        meta=PaginationMetaDTO(
            page=page, pageSize=pageSize, total=total,
            totalPages=math.ceil(total / pageSize) if total else 1,
        ),
    )


# NOTE: GET /api/recruiter/consultants is already implemented in phase3.py
# with the ConsultantDTO shape the frontend expects ({id, name, email, title}).
# Removed here to avoid a route conflict — Phase 3's version is registered in main.py.


@router.get(
    "/api/requirements/{requirement_id}/detail",
    response_model=RequirementDetailResponse,
    summary="Full requirement detail (for modal/page)",
    tags=["Phase5 - Shared"],
)
async def get_requirement_detail(
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns full requirement including job_description for the detail modal."""
    req = await _validate_requirement_id_exists(db, requirement_id)

    # Consultants can only view requirements assigned to them. Two
    # separate matching systems exist in this codebase —
    # RequirementConsultantMatch (the dashboard/requirements matched
    # view) and JobMatch (the matching engine, used by "Pending
    # Applications") — and they don't share rows. A consultant applying
    # from Pending Applications was always rejected here because only
    # RequirementConsultantMatch was checked; accept either as valid
    # proof of assignment.
    if current_user.role == "CONSULTANT":
        consultant = await _get_consultant_for_user(db, current_user)
        match = (await db.execute(
            select(RequirementConsultantMatch).where(
                RequirementConsultantMatch.requirement_id == requirement_id,
                RequirementConsultantMatch.consultant_id == consultant.id,
            )
        )).scalars().first()
        if not match:
            job_match = (await db.execute(
                select(JobMatch).where(
                    JobMatch.requirement_id == requirement_id,
                    JobMatch.consultant_id == consultant.id,
                )
            )).scalars().first()
            if not job_match:
                raise HTTPException(status_code=403, detail="Requirement not assigned to you")

    return RequirementDetailResponse(
        id=str(req.id),
        role=req.role,
        vendor=req.vendor,
        vendor_email=req.vendor_email,
        client=req.client,
        location=req.location,
        work_mode=req.work_mode,
        employment_types=req.employment_types,
        rate=req.rate,
        duration=req.duration,
        job_description=req.job_description,
        status=req.status,
        received_date=req.received_date.isoformat() if req.received_date else None,
        parse_confidence=float(req.parse_confidence) if req.parse_confidence else None,
    )


# ===========================================================================
# Apply endpoint — 6-check eligibility gate
# ===========================================================================

@router.post(
    "/api/consultant/requirements/{requirement_id}/apply",
    response_model=ApplyResponse,
    summary="Submit application — runs 6 eligibility checks",
    tags=["Phase5 - Consultant Dashboard"],
)
async def apply_to_requirement(
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Consultant submits application for a requirement.
    Runs all 6 Phase 5 eligibility checks before creating the Application record.
    Broadcasts 'application_updated' WebSocket event on success.

    PERFORMANCE: consultant is loaded once and passed to the eligibility
    check; the match lookup needed for eligibility is reused for the
    match-status update below instead of being queried twice.
    """
    _require_role(current_user, "CONSULTANT")

    req = await _validate_requirement_id_exists(db, requirement_id)
    consultant = await _get_consultant_for_user(db, current_user)

    match = (await db.execute(
        select(RequirementConsultantMatch).where(
            RequirementConsultantMatch.requirement_id == requirement_id,
            RequirementConsultantMatch.consultant_id == consultant.id,
        )
    )).scalars().first()

    can_apply, reason = await _check_apply_eligibility(
        db, current_user, req,
        preloaded_consultant=consultant,
        preloaded_match=match,
    )
    if not can_apply:
        raise HTTPException(status_code=400, detail=reason)

    # Get latest final resume
    resume = (await db.execute(
        select(GeneratedResume).where(
            GeneratedResume.consultant_id == consultant.id,
            GeneratedResume.requirement_id == requirement_id,
            GeneratedResume.is_final == True,
        )
    )).scalars().first()

    # BUG FIX: this endpoint previously only ever created an Application
    # row with status="SENT" and a WebSocket broadcast — it never sent an
    # actual email through Gmail at all. Every consultant clicking Apply
    # through THIS endpoint got a confident "Application submitted to
    # ..." success message while the vendor received nothing, with no
    # error anywhere to show anything had gone wrong (because nothing
    # technically errored — the code just never tried to send in the
    # first place). Now builds and sends a real email through the same
    # infrastructure email_queue.py's send-now endpoint uses (signature,
    # HTML body with company banner, resume attachment, Gmail token
    # resolution, real filename), instead of maintaining a second,
    # diverging "apply" implementation that only pretends to send.
    from models import EmailQueue
    from email_queue import process_single_email_queue_item, UPLOAD_DIR
    from email_template import build_application_email, resolve_sender_fields
    from pathlib import Path
    import uuid as _uuid

    sender = resolve_sender_fields(current_user, consultant)
    email_content = build_application_email(
        vendor_contact_name=None,
        role=req.role,
        consultant_name=consultant.full_name or "",
        consultant_email=consultant.email or "",
        consultant_phone=consultant.phone,
        primary_skills=consultant.primary_skills,
        **sender,
    )

    # Resolve the generated resume's actual bytes and place them where
    # process_single_email_queue_item's existing attachment resolution
    # logic already looks (a bare filename under UPLOAD_DIR, i.e.
    # /tmp/email_attachments/ — see original_filename_from_ref/
    # EMAIL_ATTACHMENT_S3_PREFIX in email_queue.py). GeneratedResume.pdf_path
    # is NOT in that ref format itself — it's whatever local temp path
    # (or, once that's cleaned up, S3 key) the generation pipeline used —
    # so it can't be passed through as-is; same local-then-S3 fallback
    # phase7.py already uses for this identical scenario.
    attachment_refs = None
    # BUG FIX: this only ever looked up a tailored GeneratedResume
    # (is_final=True for this requirement) — when a consultant applies
    # using their BASE resume instead (no tailored resume generated for
    # this requirement), `resume` is None here and attachment_refs stayed
    # None the whole way through, so the email queue item was created
    # with NO attachment at all. Nothing errored (silently missing, not
    # corrupt), which is why it could look like "sometimes there's an
    # attachment and sometimes there isn't" depending on whether a
    # tailored resume happened to exist. Fall back to the consultant's
    # base resume — same local-vs-Spaces resolution
    # resume_router.py's download_base_resume already uses.
    resume_filename = None
    resume_source_path = None
    if resume and resume.docx_path:
        resume_source_path = resume.docx_path
        # GeneratedResume.filename is always built with a .pdf extension
        # (see phase6.py's _build_resume_filename) regardless of which
        # file is actually being attached — swap it to .docx here since
        # we're sending the DOCX, not the PDF, or the attachment's real
        # bytes wouldn't match the extension in its own filename.
        base_name = resume.filename or f"Resume_{resume.id}.pdf"
        resume_filename = Path(base_name).stem + ".docx"
    elif consultant.base_resume_file_path:
        resume_source_path = consultant.base_resume_file_path
        resume_filename = Path(consultant.base_resume_file_path).name

    if resume_source_path:
        body_bytes = None
        if Path(resume_source_path).exists():
            with open(resume_source_path, "rb") as f:
                body_bytes = f.read()
        else:
            from s3_service import download_file_from_s3
            body_bytes, _ = download_file_from_s3(resume_source_path)

        if body_bytes:
            safe_name = "".join(
                c for c in (resume_filename or "Resume.pdf")
                if c.isalnum() or c in " -_."
            ).strip() or "Resume.pdf"
            unique_name = f"{_uuid.uuid4()}__{safe_name}"
            with open(Path(UPLOAD_DIR) / unique_name, "wb") as f:
                f.write(body_bytes)
            attachment_refs = [unique_name]
        else:
            # Resume exists but its bytes can't be found anywhere —
            # fail loudly instead of silently sending without it.
            raise HTTPException(
                status_code=502,
                detail="Could not retrieve the resume file to attach. Please check your resume and try again.",
            )

    queue_item = EmailQueue(
        consultant_id=consultant.id,
        requirement_id=requirement_id,
        from_email=consultant.email or "",
        to_email=req.vendor_email or "",
        cc_email=None,
        subject=email_content["subject"],
        content=email_content["body"],
        html_content=email_content["html_body"],
        attachments=attachment_refs,
        status="QUEUED",
        sent_by_user_id=current_user.id,
        scheduled_at=datetime.now(timezone.utc),
    )
    db.add(queue_item)
    await db.commit()
    await db.refresh(queue_item)

    await process_single_email_queue_item(db, queue_item)
    await db.refresh(queue_item)

    if queue_item.status != "SENT":
        raise HTTPException(
            status_code=502,
            detail=queue_item.status_text or "Failed to send application email.",
        )

    # NOTE: process_single_email_queue_item above already creates/updates
    # the Application row itself (it upserts on consultant_id +
    # requirement_id whenever item.requirement_id is set — see the
    # "revised" BUG FIX block in that function). Do NOT create a second
    # Application row here — Application has a unique constraint on
    # (consultant_id, requirement_id), so doing so would raise an
    # IntegrityError on every successful send. Just fetch what it already
    # wrote (with the real gmail_message_id, resume attachment, sent_at,
    # etc. — all more accurate than anything this endpoint could set
    # itself) to build the response below.
    app = (await db.execute(
        select(Application).where(
            Application.consultant_id == consultant.id,
            Application.requirement_id == requirement_id,
        )
    )).scalars().first()
    if not app:
        # Shouldn't happen given queue_item.status == "SENT" above, but
        # don't silently 500 with an AttributeError on app.id below if it
        # somehow does.
        raise HTTPException(
            status_code=500,
            detail="Email sent, but the application record could not be found afterward. Please check the Applications tab.",
        )

    # Update match status — reuses the match object already fetched above,
    # no second lookup needed.
    if match:
        match.status = "APPLIED"

    await db.commit()
    await db.refresh(app)

    # Broadcast WebSocket event
    await _broadcast_event("application_updated", {
        "application_id": str(app.id),
        "requirement_id": str(requirement_id),
        "consultant_id": str(consultant.id),
        "status": "SENT",
    })

    return ApplyResponse(
        success=True,
        message=f"Application submitted to {req.vendor_email}",
        application_id=str(app.id),
    )


# ===========================================================================
# Application history
# ===========================================================================

@router.get(
    "/api/consultant/applications",
    response_model=PaginatedApplicationHistory,
    summary="Consultant's own application history",
    tags=["Phase5 - Consultant Dashboard"],
)
async def get_consultant_applications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "CONSULTANT")
    consultant = await _get_consultant_for_user(db, current_user)

    total = (await db.execute(
        select(func.count(Application.id)).where(Application.consultant_id == consultant.id)
    )).scalar_one()

    results = (await db.execute(
        select(Application, Requirement, GeneratedResume, User)
        .join(Requirement, Requirement.id == Application.requirement_id)
        .outerjoin(GeneratedResume, GeneratedResume.id == Application.generated_resume_id)
        .outerjoin(User, User.id == Application.recruiter_id)
        .where(Application.consultant_id == consultant.id)
        .order_by(Application.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()

    rows = []
    for app, req, resume, sender in results:
        if resume and resume.filename:
            resume_filename = resume.filename
        elif app.resume_attachment_path:
            # BUG FIX: raw os.path.basename() returned the stored
            # reference unchanged (e.g. "f32a0912-...__agfgr.pdf") — the
            # UUID prefix was never stripped off here, so the table
            # showed the meaningless internal storage key instead of the
            # real filename someone actually uploaded. Same recovery
            # function the download endpoint (phase7.py) already uses.
            from email_queue import original_filename_from_ref
            resume_filename = original_filename_from_ref(app.resume_attachment_path)
        else:
            resume_filename = None

        rows.append(ApplicationHistoryRow(
            application_id=str(app.id),
            requirement_id=str(req.id),
            role=req.role,
            vendor=req.vendor,
            vendor_email=app.vendor_email,
            application_status=app.status,
            sent_at=(app.sent_at or app.created_at).isoformat(),
            resume_url=resume.pdf_url if resume else None,
            resume_id=str(app.generated_resume_id) if app.generated_resume_id else None,
            resume_available=bool(app.generated_resume_id or app.resume_attachment_path),
            ats_score=float(resume.ats_score) if resume and resume.ats_score else None,
            client=req.client,
            gmail_message_id=app.gmail_message_id,
            sent_by_name=sender.full_name if sender else consultant.full_name,
            sent_by_role=sender.role if sender else "CONSULTANT",
            resume_filename=resume_filename,
        ))

    return PaginatedApplicationHistory(
        data=rows, total=total, page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 1,
    )


@router.get(
    "/api/recruiter/applications",
    response_model=PaginatedRecruiterApplications,
    summary="Recruiter view of all applications",
    tags=["Phase5 - Recruiter Dashboard"],
)
async def get_recruiter_applications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    consultant_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns a typed, paginated response (Pydantic model, not a raw dict).
    total respects the consultant_id filter when present.

    BUG FIX: sent_at ordering was previously hardcoded desc with no way
    for a caller to request asc — the frontend was compensating by
    re-sorting only the current page's already-fixed rows client-side,
    which is incoherent across page boundaries (page 2 wouldn't continue
    the same sequence, since the underlying data was always fetched
    server-side in descending order regardless of what the UI showed).
    """
    _require_role(current_user, "RECRUITER", "ADMIN")

    q = select(Application, Requirement, Consultant, User) \
        .join(Requirement, Requirement.id == Application.requirement_id) \
        .join(Consultant, Consultant.id == Application.consultant_id) \
        .outerjoin(User, User.id == Application.recruiter_id)
    # NOTE: joined to Requirement/Consultant the same way as q above —
    # required so the `search` filter below (which references
    # Consultant.full_name / Requirement.role / vendor / client) can be
    # applied to count_q too, not just q. Application-only filters
    # (consultant_id, status) would have worked fine against a bare
    # func.count(Application.id), but count_q needs the same FROM-clause
    # as q for any filter that touches a joined table.
    count_q = (
        select(func.count(Application.id))
        .select_from(Application)
        .join(Requirement, Requirement.id == Application.requirement_id)
        .join(Consultant, Consultant.id == Application.consultant_id)
    )

    # BUG FIX: this only ever restricted by RecruiterConsultant assignment
    # when a specific consultant_id filter was passed. The frontend's
    # ApplicationsTable never sends consultant_id for the recruiter view
    # (only the admin view supports that filter today), so every recruiter
    # hitting this endpoint saw every application from every consultant in
    # the system — not just the ones assigned to them, despite the page
    # itself being labeled "applications sent on behalf of your
    # consultants". Scope to the recruiter's active assignments whenever
    # no explicit consultant_id is given, same as every other recruiter-
    # facing endpoint (e.g. _assert_recruiter_mapped) already does.
    if current_user.role == "RECRUITER" and not consultant_id:
        assigned_result = await db.execute(
            select(RecruiterConsultant.consultant_id).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.is_active == True,
            )
        )
        assigned_ids = [row[0] for row in assigned_result.all()]
        q = q.where(Application.consultant_id.in_(assigned_ids))
        count_q = count_q.where(Application.consultant_id.in_(assigned_ids))

    filters = []

    if consultant_id:
        try:
            ids = [int(x.strip()) for x in consultant_id.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid consultant_id format")

        if ids:
            if current_user.role == "RECRUITER":
                rcs = (await db.execute(
                    select(RecruiterConsultant.consultant_id).where(
                        RecruiterConsultant.recruiter_id == current_user.id,
                        RecruiterConsultant.consultant_id.in_(ids),
                        RecruiterConsultant.is_active == True,
                    )
                )).scalars().all()
                if len(set(rcs)) != len(set(ids)):
                    raise HTTPException(status_code=403, detail="One or more consultants not assigned to you")
            filters.append(Application.consultant_id.in_(ids))

    if status:
        filters.append(Application.status == status)

    # Search across the fields actually visible in the table — consultant
    # name, role, vendor, and client — so a recruiter can find an
    # application without knowing which specific field it's in.
    if search and search.strip():
        term = f"%{search.strip()}%"
        filters.append(
            or_(
                Consultant.full_name.ilike(term),
                Requirement.role.ilike(term),
                Requirement.vendor.ilike(term),
                Requirement.client.ilike(term),
            )
        )

    if filters:
        base_filter = and_(*filters)
        q = q.where(base_filter)
        count_q = count_q.where(base_filter)

    total = (await db.execute(count_q)).scalar_one()

    order_col = Application.sent_at.asc().nullslast() if sort_dir == "asc" else Application.sent_at.desc().nullslast()
    results = (await db.execute(
        q.order_by(order_col)
        .offset((page - 1) * page_size).limit(page_size)
    )).all()

    data = [
        RecruiterApplicationRow(
            application_id=str(app.id),
            consultant_name=cons.full_name,
            consultant_id=str(cons.id),
            requirement_id=str(req.id),
            role=req.role,
            vendor=req.vendor,
            vendor_email=app.vendor_email,
            status=app.status,
            sent_at=app.sent_at.isoformat() if app.sent_at else None,
            client=req.client,
            gmail_message_id=app.gmail_message_id,
            resume_id=str(app.generated_resume_id) if app.generated_resume_id else None,
            resume_available=bool(app.generated_resume_id or app.resume_attachment_path),
            sent_by_name=sender.full_name if sender else cons.full_name,
            sent_by_role=sender.role if sender else "CONSULTANT",
        )
        for app, req, cons, sender in results
    ]

    return PaginatedRecruiterApplications(
        data=data,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 1,
    )


# ===========================================================================
# Resume generation trigger
# ===========================================================================

@router.post(
    "/api/admin/resumes/generate",
    response_model=GenerateResumeResponse,
    summary="Trigger resume generation for a consultant+requirement",
    tags=["Phase5 - Shared"],
)
async def generate_resume(
    body: GenerateResumeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Triggers Phase 6's actual AI resume generation pipeline synchronously
    (no Celery worker exists in this codebase yet — see phase6.py
    _run_generation_pipeline for the real AI tailor -> ATS score -> DOCX/PDF flow).
    Broadcasts 'resume_generated' WebSocket event with the real outcome once done.
    """
    _require_role(current_user, "ADMIN", "RECRUITER")

    consultant = await _validate_consultant_id_exists(db, body.consultant_id)
    requirement = await _validate_requirement_id_exists(db, body.requirement_id)

    # Verify match exists
    match = (await db.execute(
        select(RequirementConsultantMatch).where(
            RequirementConsultantMatch.requirement_id == body.requirement_id,
            RequirementConsultantMatch.consultant_id == body.consultant_id,
        )
    )).scalars().first()
    if not match:
        raise HTTPException(status_code=404, detail="No match found for this consultant+requirement")

    if not requirement.job_description:
        raise HTTPException(status_code=422, detail="Requirement has no job description.")

    # Broadcast "started" immediately so connected dashboards show a spinner
    await _broadcast_event("resume_generation_started", {
        "consultant_id": str(body.consultant_id),
        "requirement_id": str(body.requirement_id),
    })

    # Call Phase 6's real pipeline — AI tailor -> validate -> ATS score -> DOCX/PDF -> store
    from phase6 import _run_generation_pipeline

    try:
        resume = await _run_generation_pipeline(
            db=db,
            consultant=consultant,
            requirement=requirement,
            match=match,
            current_user=current_user,
            attempt=1,
        )
    except HTTPException as exc:
        await _broadcast_event("resume_generation_failed", {
            "consultant_id": str(body.consultant_id),
            "requirement_id": str(body.requirement_id),
            "error": exc.detail,
        })
        raise

    # Broadcast the real completed result — pdf_url/generation_status are already
    # set correctly by phase6.py's _run_generation_pipeline itself
    await _broadcast_event("resume_generated", {
        "resume_id": str(resume.id),
        "consultant_id": str(body.consultant_id),
        "requirement_id": str(body.requirement_id),
        "status": resume.generation_status,
        "ats_score": float(resume.ats_score) if resume.ats_score is not None else None,
    })

    return GenerateResumeResponse(
        success=resume.status == "READY",
        message=(
            f"Resume generated. ATS score: {resume.ats_score}."
            if resume.status == "READY"
            else f"Resume generated but ATS score ({resume.ats_score}) is below 80. Manual review required."
        ),
        resume_id=str(resume.id),
        generation_status=resume.generation_status,
    )


# ===========================================================================
# Dashboard stats
# ===========================================================================

@router.get(
    "/api/dashboard/stats",
    response_model=DashboardStatsResponse,
    summary="Role-specific dashboard statistics",
    tags=["Phase5 - Shared"],
)
async def dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    PERFORMANCE: each branch issues a single query with multiple scalar
    subqueries (one round trip) instead of 3 separate sequential count queries.
    """
    if current_user.role == "CONSULTANT":
        consultant = await _get_consultant_for_user(db, current_user)

        assigned_sq = (
            select(func.count(RequirementConsultantMatch.id))
            .where(RequirementConsultantMatch.consultant_id == consultant.id)
            .scalar_subquery()
        )
        applied_sq = (
            select(func.count(Application.id))
            .where(Application.consultant_id == consultant.id, Application.status == "SENT")
            .scalar_subquery()
        )
        resumes_sq = (
            select(func.count(GeneratedResume.id))
            .where(GeneratedResume.consultant_id == consultant.id, GeneratedResume.is_final == True)
            .scalar_subquery()
        )

        row = (await db.execute(select(assigned_sq, applied_sq, resumes_sq))).one()
        total_assigned, total_applied, total_resumes = row

        return DashboardStatsResponse(
            role="CONSULTANT",
            total_assigned=total_assigned,
            total_applied=total_applied,
            total_resumes_generated=total_resumes,
        )
    else:
        reqs_sq = select(func.count(Requirement.id)).scalar_subquery()
        cons_sq = (
            select(func.count(Consultant.id))
            .where(Consultant.status == "ACTIVE")
            .scalar_subquery()
        )
        apps_sq = (
            select(func.count(Application.id))
            .where(Application.status == "SENT")
            .scalar_subquery()
        )

        row = (await db.execute(select(reqs_sq, cons_sq, apps_sq))).one()
        total_reqs, total_cons, total_apps = row

        return DashboardStatsResponse(
            role=current_user.role,
            total_requirements=total_reqs,
            total_consultants=total_cons,
            total_applications_sent=total_apps,
        )


# ===========================================================================
# WebSocket — real-time dashboard events
# ===========================================================================

# In-memory connection registry (Redis pub/sub with fallback)
_ws_connections: Set[WebSocket] = set()


_broadcast_redis_client = None


async def _get_broadcast_redis_client():
    """Lazily create the Redis client used for publishing dashboard
    events, once, and reuse it across every _broadcast_event call.
    Previously a brand new connection was opened and torn down
    (from_url + aclose) on every single broadcast — real per-call
    overhead on top of the connection setup itself. If the cached client
    is ever unusable (e.g. Redis restarted), the caller's own try/except
    already handles that by falling back to in-memory broadcast, and a
    fresh client gets created on the next call since the module-level
    reference is cleared here whenever a publish fails."""
    global _broadcast_redis_client
    if _broadcast_redis_client is None:
        import os
        import redis.asyncio as aioredis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        _broadcast_redis_client = await aioredis.from_url(redis_url, decode_responses=True)
    return _broadcast_redis_client


async def _broadcast_event(event_type: str, payload: dict):
    """
    Broadcast event to all connected WebSocket clients.
    Tries Redis pub/sub first, falls back to in-memory broadcast.
    """
    message = json.dumps({
        "event": event_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Try Redis pub/sub
    try:
        global _broadcast_redis_client
        r = await _get_broadcast_redis_client()
        await r.publish("dashboard_events", message)
        return
    except Exception:
        # Clear the cached client so a genuinely dead connection (e.g.
        # Redis restarted) doesn't keep failing forever — the next call
        # creates a fresh one.
        _broadcast_redis_client = None

    # Fallback: broadcast directly to connected clients
    dead = set()
    for ws in list(_ws_connections):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _ws_connections.discard(ws)


@router.websocket("/api/ws/dashboard")
async def dashboard_websocket(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
):
    """
    WebSocket endpoint for real-time dashboard updates.
    Client connects: ws://host/api/ws/dashboard?token=JWT
    Events: new_requirement, requirement_assigned, application_updated, resume_generated
    """
    await websocket.accept()

    # JWT auth via query param
    if not token:
        await websocket.send_text(json.dumps({"error": "token required"}))
        await websocket.close(code=4001)
        return

    try:
        from auth import decode_access_token
        payload = decode_access_token(token)
    except Exception:
        await websocket.send_text(json.dumps({"error": "invalid token"}))
        await websocket.close(code=4001)
        return

    _ws_connections.add(websocket)
    logger.info(f"WS connected: {payload.get('sub')} — total={len(_ws_connections)}")

    # Send welcome
    await websocket.send_text(json.dumps({
        "event": "connected",
        "payload": {"user": payload.get("sub"), "role": payload.get("role")},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }))

    # Try Redis subscription
    redis_task = None
    try:
        import redis.asyncio as aioredis
        import os
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

        async def redis_listener():
            r = await aioredis.from_url(redis_url, decode_responses=True)
            pubsub = r.pubsub()
            await pubsub.subscribe("dashboard_events")
            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        await websocket.send_text(message["data"])
                    except Exception:
                        break
            await r.aclose()

        redis_task = asyncio.create_task(redis_listener())
    except Exception:
        pass  # Fall back to in-memory broadcast

    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.info(f"WS disconnected: {payload.get('sub')}")
    finally:
        _ws_connections.discard(websocket)
        if redis_task:
            redis_task.cancel()