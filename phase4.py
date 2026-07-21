# phase4.py
# ---------------------------------------------------------------------------
# Phase 4 — Consultant Matching Engine and Assignment Workflow
#
# Architecture: single flat file in project root, same pattern as phase3.py.
# Reuses get_db, get_current_user from auth.py — no circular dependency.
#
# New endpoints:
#
#   GET  /api/consultant/requirements                       my assigned requirements
#   GET  /api/recruiter/consultants/{consultant_id}/requirements   recruiter view (mapping enforced)
#   POST /api/admin/requirements/{requirement_id}/rematch    re-run matching for one requirement
#   POST /api/admin/requirements/match-all                   run matching for all unmatched requirements
#
# Core logic:
#   extract_skills()        — alias-dictionary skill extraction from JD text
#   score_skills()          — Jaccard-style skill overlap
#   score_role()             — role title token overlap
#   score_experience()       — consultant total experience vs requirement expectation
#   score_employment_type()  — employment_types intersection
#   score_location()         — location / work mode compatibility
#   score_work_auth()        — work authorization compatibility
#   score_match()             — combines all 6 factors per the doc's weights
#   match_requirement()       — scores all active consultants against one requirement,
#                                upserts into requirement_consultant_matches
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import (
    User,
    Consultant,
    RecruiterConsultant,
    ConsultantExperience,
    Requirement,
    RequirementConsultantMatch,
)
from auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "60"))

# ---------------------------------------------------------------------------
# Skill library — same alias-dictionary pattern as phase3.py's _detect_skills
# Kept as its own copy here per Phase 4 doc Task 2's own code example
# (SKILL_ALIASES is defined fresh in Phase 4 scope, mirroring phase3's list).
# ---------------------------------------------------------------------------

SKILL_ALIASES: dict[str, list[str]] = {
    "python": ["python", "python3"],
    "java": ["java", "core java"],
    "javascript": ["javascript", "js", "es6"],
    "typescript": ["typescript", "ts"],
    "c#": ["c#", "csharp"],
    "go": ["golang", "go"],
    "react": ["react", "react.js", "reactjs"],
    "angular": ["angular", "angularjs"],
    "vue.js": ["vue", "vue.js", "vuejs"],
    "next.js": ["next.js", "nextjs"],
    "node.js": ["node.js", "nodejs"],
    "fastapi": ["fastapi"],
    "django": ["django"],
    "flask": ["flask"],
    "spring boot": ["spring boot", "springboot"],
    "postgresql": ["postgresql", "postgres"],
    "mysql": ["mysql"],
    "oracle sql": ["oracle sql", "oracle db", "pl/sql"],
    "mongodb": ["mongodb", "mongo"],
    "redis": ["redis"],
    "elasticsearch": ["elasticsearch"],
    "aws": ["aws", "amazon web services"],
    "azure": ["azure", "microsoft azure"],
    "gcp": ["gcp", "google cloud"],
    "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"],
    "terraform": ["terraform"],
    "ci/cd": ["ci/cd", "cicd"],
    "rest api": ["rest api", "restful"],
    "graphql": ["graphql"],
    "microservices": ["microservices"],
    "machine learning": ["machine learning", "ml"],
    "sql": ["sql", "postgresql", "mysql", "oracle sql"],
    "kafka": ["kafka", "apache kafka"],
    "spark": ["spark", "apache spark", "pyspark"],
    "airflow": ["airflow", "apache airflow"],
    "tailwind": ["tailwind", "tailwindcss"],
    "redux": ["redux"],
    "sap": ["sap"],
    "salesforce": ["salesforce", "sfdc"],
    "servicenow": ["servicenow"],
    "linux": ["linux", "ubuntu"],
    "ansible": ["ansible"],
    "jenkins": ["jenkins"],
}


def extract_skills(text: Optional[str]) -> List[str]:
    """
    Rule/keyword dictionary skill extraction — per doc Task 2.
    Returns sorted set of canonical skills found in text.
    """
    if not text:
        return []
    lower = text.lower()
    found = set()
    for canonical, aliases in SKILL_ALIASES.items():
        if any(alias in lower for alias in aliases):
            found.add(canonical)
    return sorted(found)


def _consultant_skills(consultant: Consultant) -> List[str]:
    """Combine primary + secondary skills text into a single skill list."""
    combined = ", ".join(filter(None, [consultant.primary_skills, consultant.secondary_skills]))
    return extract_skills(combined)


# ---------------------------------------------------------------------------
# Scoring functions — Task 1
# ---------------------------------------------------------------------------

def score_skills(requirement_skills: List[str], consultant_skills: List[str]) -> tuple[float, List[str], List[str]]:
    """
    Jaccard-style overlap: matched / total required skills.
    Returns (score 0-100, matched_skills, missing_skills).
    """
    if not requirement_skills:
        return 100.0, [], []  # no skills extracted from JD — don't penalize

    req_set = set(requirement_skills)
    cons_set = set(consultant_skills)

    matched = sorted(req_set & cons_set)
    missing = sorted(req_set - cons_set)

    score = (len(matched) / len(req_set)) * 100 if req_set else 0.0
    return round(score, 2), matched, missing


def score_role(
    requirement_role: Optional[str],
    consultant_preferred_roles: Optional[str],
    experiences: Optional[List[ConsultantExperience]] = None,
) -> float:
    """
    Role title token overlap — simple word-set comparison.

    BUG FIX: previously only compared against consultant.preferred_roles,
    an optional free-text profile field that's rarely filled in — meaning
    this factor silently scored 0 for nearly every consultant, dragging
    every match's total below MATCH_THRESHOLD regardless of actual fit.
    Now also pulls comparison tokens from the consultant's real job
    titles (ConsultantExperience.role_title), which is populated far
    more reliably than the preference field.
    """
    if not requirement_role:
        return 0.0

    req_tokens = set(requirement_role.lower().split())
    if not req_tokens:
        return 0.0

    pref_tokens = set()
    if consultant_preferred_roles:
        pref_tokens |= set(consultant_preferred_roles.lower().replace(",", " ").split())
    if experiences:
        for exp in experiences:
            if exp.role_title:
                pref_tokens |= set(exp.role_title.lower().split())

    if not pref_tokens:
        return 0.0

    overlap = req_tokens & pref_tokens
    return round((len(overlap) / len(req_tokens)) * 100, 2)


def _calculate_total_experience_years(experiences: List[ConsultantExperience]) -> float:
    """Sum experience durations from consultant_experience rows."""
    total_days = 0
    today = date.today()
    for exp in experiences:
        if not exp.start_date:
            continue
        end = today if exp.is_present else (exp.end_date or today)
        total_days += max((end - exp.start_date).days, 0)
    return round(total_days / 365.25, 1)


def _parse_min_years_required(requirement: Requirement) -> Optional[float]:
    """
    Extract the minimum years of experience the requirement is asking for,
    from parser.py's extract_experience() output stored in
    requirement.parsed_fields['experience'] (e.g. "5+ years", "3-5 years",
    "10 years"). Returns None if the requirement never stated one.
    """
    exp_text = None
    if requirement.parsed_fields:
        exp_text = requirement.parsed_fields.get("experience")
    if not exp_text:
        return None
    m = re.search(r"(\d+)", exp_text)
    if not m:
        return None
    return float(m.group(1))


def score_experience(requirement: Requirement, consultant: Consultant, experiences: List[ConsultantExperience]) -> float:
    """
    Score based on how the consultant's total experience compares to what
    the requirement is actually asking for.

    BUG FIX: previously scored the consultant's absolute years on a flat
    0-8yr scale with NO reference to the requirement at all — a posting
    asking for 10+ years and one asking for 1+ year scored a given
    consultant identically, and a very senior consultant capped out at
    100 regardless of whether the role wanted a junior. Now compares
    against parser.py's extracted parsed_fields['experience'] minimum
    when the requirement stated one, falling back to the old absolute
    scale only when it didn't.
    """
    years = float(consultant.total_experience_years or 0)
    if years <= 0 and experiences:
        years = _calculate_total_experience_years(experiences)

    if years <= 0:
        return 0.0

    required_years = _parse_min_years_required(requirement)
    if required_years is None or required_years <= 0:
        # Requirement didn't state a minimum — fall back to absolute scale
        if years >= 8:
            return 100.0
        return round((years / 8) * 100, 2)

    if years >= required_years:
        return 100.0
    # Below the stated minimum — partial credit proportional to how close
    return round((years / required_years) * 100, 2)


def score_employment_type(requirement_types: Optional[List[str]], consultant_types: Optional[List[str]]) -> float:
    """
    Employment type intersection — C2C/W2/FULLTIME.

    BUG FIX: requirement_types defaults to ["UNKNOWN"] (see parser.py)
    whenever the source email didn't clearly state an employment type —
    this previously scored 0 for that case, identical to a genuine
    mismatch, silently zeroing this factor for every ambiguously-worded
    posting. Treat "not specified" as "don't penalize" instead, the same
    way score_skills() already does when a JD has no extracted skills.
    """
    if not requirement_types or requirement_types == ["UNKNOWN"]:
        return 100.0

    if not consultant_types:
        return 0.0

    req_set = set(t.upper() for t in requirement_types)
    cons_set = set(t.upper() for t in consultant_types)

    overlap = req_set & cons_set
    return 100.0 if overlap else 0.0


def score_location(requirement: Requirement, consultant: Consultant, experiences: List[ConsultantExperience]) -> float:
    """
    Location/work mode compatibility.
    REMOTE requirement matches any consultant fully (location-agnostic).
    Otherwise compare requirement.location against consultant.preferred_locations
    and work_mode against the consultant's most recent experience entry.
    """
    req_work_mode = (requirement.work_mode or "").upper()

    if req_work_mode == "REMOTE":
        return 100.0

    score = 0.0

    # Location match
    if requirement.location and consultant.preferred_locations:
        req_loc = requirement.location.lower()
        pref_locs = consultant.preferred_locations.lower()
        if req_loc in pref_locs:
            score += 60.0

    # Work mode match — compare against most recent experience entry's work_mode
    if req_work_mode and experiences:
        latest = sorted(
            [e for e in experiences if e.work_mode],
            key=lambda e: e.start_date or date.min,
            reverse=True,
        )
        if latest and (latest[0].work_mode or "").upper() == req_work_mode:
            score += 40.0

    return round(min(score, 100.0), 2)


def score_work_auth(requirement: Requirement, consultant: Consultant) -> float:
    """
    Work authorization compatibility.
    Requirement doesn't have an explicit work-auth field in current schema,
    so this checks employment_types for C2C/W2 implications:
    - FULLTIME roles typically require US_CITIZEN or GC
    - C2C is open to most work authorizations including H1B
    """
    if not consultant.work_authorization:
        return 0.0

    req_types = set((requirement.employment_types or []))
    auth = consultant.work_authorization.upper()

    if "FULLTIME" in req_types and auth not in {"US_CITIZEN", "GREEN_CARD", "GC"}:
        return 0.0

    return 100.0


def score_match(
    requirement: Requirement,
    consultant: Consultant,
    experiences: List[ConsultantExperience],
) -> dict:
    """
    Combine all 6 factors per doc Task 1 weights:
      skill 40%, role 20%, experience 15%, employment 10%, location 10%, auth 5%
    Returns dict with total score, breakdown, matched/missing skills, and reason.
    """
    requirement_skills = extract_skills(requirement.job_description)
    consultant_skills = _consultant_skills(consultant)

    skill_raw, matched_skills, missing_skills = score_skills(requirement_skills, consultant_skills)
    role_raw = score_role(requirement.role, consultant.preferred_roles, experiences)
    exp_raw = score_experience(requirement, consultant, experiences)
    employment_raw = score_employment_type(requirement.employment_types, consultant.preferred_employment_types)
    location_raw = score_location(requirement, consultant, experiences)
    auth_raw = score_work_auth(requirement, consultant)

    skill_score = skill_raw * 0.40
    role_score = role_raw * 0.20
    exp_score = exp_raw * 0.15
    employment_score = employment_raw * 0.10
    location_score = location_raw * 0.10
    auth_score = auth_raw * 0.05

    total = round(skill_score + role_score + exp_score + employment_score + location_score + auth_score, 2)

    reason_parts = []
    if matched_skills:
        reason_parts.append(f"Matched skills: {', '.join(matched_skills)}")
    if missing_skills:
        reason_parts.append(f"Missing skills: {', '.join(missing_skills)}")
    if employment_raw == 0:
        reason_parts.append("Employment type mismatch")
    if role_raw > 0:
        reason_parts.append(f"Role title overlap: {role_raw}%")

    match_reason = "; ".join(reason_parts) if reason_parts else "No strong signals found"

    return {
        "total": total,
        "skill_score": round(skill_score, 2),
        "role_score": round(role_score, 2),
        "experience_score": round(exp_score, 2),
        "employment_score": round(employment_score, 2),
        "location_score": round(location_score, 2),
        "auth_score": round(auth_score, 2),
        "matched_skills": matched_skills,
        "missing_skills": missing_skills,
        "match_reason": match_reason,
    }


# ---------------------------------------------------------------------------
# Matching worker — Task 3
# ---------------------------------------------------------------------------

async def match_requirement(db: AsyncSession, requirement_id: int) -> int:
    """
    Score all active consultants against one requirement.
    Upserts into requirement_consultant_matches for scores >= MATCH_THRESHOLD.
    Rerunning does not duplicate — UNIQUE constraint on (requirement_id, consultant_id)
    combined with explicit existence check ensures idempotency.
    Returns count of assignments created or updated.

    PERFORMANCE: batches all per-consultant lookups into 2 queries total
    (experiences, existing matches) regardless of consultant count, instead of
    issuing one query per consultant inside the loop. This keeps the query count
    constant — O(1) round trips — whether there are 10 or 10,000 active consultants.
    """
    req_result = await db.execute(select(Requirement).where(Requirement.id == requirement_id))
    requirement = req_result.scalars().first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    consultants_result = await db.execute(
        select(Consultant).where(Consultant.status == "ACTIVE")
    )
    consultants = consultants_result.scalars().all()

    if not consultants:
        logger.info("No active consultants found — skipping match for requirement_id=%s", requirement_id)
        return 0

    consultant_ids = [c.id for c in consultants]

    # ── Batch query 1: ALL experience rows for ALL consultants in ONE query ──
    exp_result = await db.execute(
        select(ConsultantExperience).where(ConsultantExperience.consultant_id.in_(consultant_ids))
    )
    experiences_by_consultant: dict[int, list[ConsultantExperience]] = {}
    for exp in exp_result.scalars().all():
        experiences_by_consultant.setdefault(exp.consultant_id, []).append(exp)

    # ── Batch query 2: ALL existing matches for this requirement in ONE query ──
    existing_result = await db.execute(
        select(RequirementConsultantMatch).where(
            RequirementConsultantMatch.requirement_id == requirement_id,
            RequirementConsultantMatch.consultant_id.in_(consultant_ids),
        )
    )
    existing_matches_by_consultant: dict[int, RequirementConsultantMatch] = {
        m.consultant_id: m for m in existing_result.scalars().all()
    }

    assignment_count = 0

    # ── Scoring loop — pure in-memory computation, zero DB round trips per iteration ──
    for consultant in consultants:
        experiences = experiences_by_consultant.get(consultant.id, [])

        result = score_match(requirement, consultant, experiences)

        if result["total"] < MATCH_THRESHOLD:
            continue

        existing = existing_matches_by_consultant.get(consultant.id)

        if existing:
            existing.match_score = result["total"]
            existing.skill_score = result["skill_score"]
            existing.role_score = result["role_score"]
            existing.experience_score = result["experience_score"]
            existing.employment_score = result["employment_score"]
            existing.location_score = result["location_score"]
            existing.auth_score = result["auth_score"]
            existing.matched_skills = result["matched_skills"]
            existing.missing_skills = result["missing_skills"]
            existing.match_reason = result["match_reason"]
        else:
            db.add(RequirementConsultantMatch(
                requirement_id=requirement_id,
                consultant_id=consultant.id,
                match_score=result["total"],
                skill_score=result["skill_score"],
                role_score=result["role_score"],
                experience_score=result["experience_score"],
                employment_score=result["employment_score"],
                location_score=result["location_score"],
                auth_score=result["auth_score"],
                matched_skills=result["matched_skills"],
                missing_skills=result["missing_skills"],
                match_reason=result["match_reason"],
                status="ASSIGNED",
            ))

        assignment_count += 1

    # BUG FIX: match_requirement() upserted rows into
    # requirement_consultant_matches correctly, but never wrote back to
    # requirements.ats_match_count — the column the admin Requirements
    # table actually displays. Matching genuinely worked; the visible
    # count just never reflected it (stuck at whatever seed.py's random
    # demo value or the column default of 0 was). assignment_count here
    # is exactly "consultants meeting MATCH_THRESHOLD in this run", which
    # is the correct current match count for this requirement.
    requirement.ats_match_count = assignment_count

    await db.commit()
    logger.info(
        "Matched requirement_id=%s — %d consultants scored, %d assignments created/updated (3 total queries)",
        requirement_id, len(consultants), assignment_count,
    )
    return assignment_count


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class MatchedRequirementResponse(BaseModel):
    id: str
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
    match_reason: Optional[str] = None
    received_date: Optional[str] = None


class RematchResponse(BaseModel):
    requirement_id: str
    assignments_created_or_updated: int


class MatchAllResponse(BaseModel):
    requirements_processed: int
    total_assignments: int


class NewMatchesCountResponse(BaseModel):
    new_matches: int
    days: int


# ---------------------------------------------------------------------------
# Helpers
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


def _match_to_response(match: RequirementConsultantMatch, requirement: Requirement) -> MatchedRequirementResponse:
    return MatchedRequirementResponse(
        id=str(requirement.id),
        role=requirement.role,
        vendor=requirement.vendor,
        client=requirement.client,
        location=requirement.location,
        work_mode=requirement.work_mode,
        employment_types=requirement.employment_types,
        rate=requirement.rate,
        status=requirement.status,
        match_score=float(match.match_score),
        match_status=match.status,
        matched_skills=match.matched_skills or [],
        missing_skills=match.missing_skills or [],
        match_reason=match.match_reason,
        received_date=requirement.received_date.isoformat() if requirement.received_date else None,
    )


# ---------------------------------------------------------------------------
# Assignment APIs — Task 4
#
# NOTE: GET /api/consultant/requirements and
# GET /api/recruiter/consultants/{consultant_id}/requirements were originally
# built here, but have been superseded by phase5.py's versions, which were
# verified field-by-field against the actual frontend service files
# (services/consultantService.ts and lib/api/recruiter.api.ts) and include
# the resume/eligibility data those frontend files require. Removed here to
# avoid a route conflict — phase5.py's versions are registered in main.py.
# ---------------------------------------------------------------------------

@router.post(
    "/api/admin/requirements/{requirement_id}/rematch",
    response_model=RematchResponse,
    summary="Re-run matching for a single requirement (admin only)",
)
async def rematch_requirement(
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Admin-triggered synchronous matching run for one requirement.
    Substitutes for a background worker until Phase 2's Celery/scheduler exists.
    """
    _require_role(current_user, "ADMIN")
    count = await match_requirement(db, requirement_id)
    return RematchResponse(requirement_id=str(requirement_id), assignments_created_or_updated=count)


@router.post(
    "/api/admin/requirements/match-all",
    response_model=MatchAllResponse,
    summary="Run matching for all requirements (admin only)",
)
async def match_all_requirements(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Admin-triggered bulk matching run across every requirement in the table.
    Substitutes for a background worker until Phase 2's Celery/scheduler exists.
    """
    _require_role(current_user, "ADMIN")

    result = await db.execute(select(Requirement))
    requirements = result.scalars().all()

    total_assignments = 0
    for requirement in requirements:
        # BUG FIX: previously had no per-requirement error isolation — a DB
        # failure on any single requirement (bad data, constraint violation,
        # etc.) crashed the entire bulk run with an unhandled 500, silently
        # dropping every requirement after it, and left the shared session
        # in an aborted-transaction state for anything that followed.
        # Isolate + log + continue, matching the pattern already used by
        # sync_pending_emails() and the email queue worker loop.
        try:
            count = await match_requirement(db, requirement.id)
            total_assignments += count
        except Exception as e:
            await db.rollback()
            print(f"[match_all_requirements] FAILED requirement_id={requirement.id}: {e}")
            from error_logger import log_db_error
            await log_db_error(
                stage="match_all_requirements",
                error=e,
                source_type="requirement",
                source_id=requirement.id,
            )
            continue

    return MatchAllResponse(
        requirements_processed=len(requirements),
        total_assignments=total_assignments,
    )


@router.get(
    "/api/admin/requirements/new-matches-count",
    response_model=NewMatchesCountResponse,
    summary="Count requirements that picked up a new match in the last N days (admin only)",
)
async def get_new_matches_count(
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Backs the admin dashboard's "New Matches (7d)" stat card. This endpoint
    never existed before — the frontend (admin.api.ts) had it hardcoded to
    0 with a comment explaining there was nothing real to call. Counts
    DISTINCT requirements with at least one match row created (not just
    updated) in the window — re-running match-all touches updated_at on
    existing rows too, so filtering on created_at specifically counts
    genuinely NEW matches, not re-scores of old ones.
    """
    _require_role(current_user, "ADMIN")

    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(func.count(func.distinct(RequirementConsultantMatch.requirement_id)))  # pylint: disable=not-callable  # pyright: ignore[reportOptionalCall, reportCallIssue]  # noqa: E1102
        .where(RequirementConsultantMatch.created_at >= since)
    )
    count = result.scalar_one()

    return NewMatchesCountResponse(new_matches=count, days=days)