# phase4.py
# ---------------------------------------------------------------------------
# Phase 4 — Consultant Matching Engine and Assignment Workflow
#
# Architecture: single flat file in project root, same pattern as phase3.py.
# Reuses get_db, get_current_user from auth.py — no circular dependency.
#
# Endpoints:
#
#   POST /api/admin/requirements/{requirement_id}/rematch    queue one requirement for cron rematch
#   POST /api/admin/requirements/match-all                   queue open requirements for cron rematch
#   GET  /api/admin/requirements/match-all/status            poll status of cron rematch queue
#   GET  /api/admin/requirements/new-matches-count           count requirements with new matches
#
# Core logic:
#   extract_skills() — alias-dictionary skill extraction from JD text (shared with claude_service.py)
#   Actual matching runs exclusively in rap_python_cron.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import (
    User,
    Requirement,
    RequirementConsultantMatch,
)
from auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

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
    # BUG FIX (role-title skill top-up never fired for "...with AI &
    # React"-style titles): "ai" only existed as a SYNONYMS entry for role
    # TOKEN expansion (score_role's old token comparison), never as a
    # recognized canonical SKILL — so extract_skills() on a title
    # containing "AI" found nothing, and the title-skill top-up in
    # score_role() (see below) had no skill to credit even when the
    # consultant's actual skill list included AI/ML work.
    "ai": ["ai", "artificial intelligence", "genai", "generative ai"],
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


def _alias_matches(alias: str, text_str: str) -> bool:
    """
    Word-boundary-aware check for whether `alias` genuinely appears in
    `text_str`, not just as a substring of a longer, unrelated word.

    BUG FIX: extract_skills() used to check `alias in lower` — plain
    substring containment — which meant a short alias could false-positive
    match inside a completely different word: "java" (the alias for
    canonical "java") is literally a substring of "JavaScript", and "ml"
    (the alias for "machine learning") is a substring of "HTML"/"DHTML".
    A consultant listing only JavaScript/HTML/DHTML — nothing Java or ML
    related at all — would get credited with both skills, silently
    inflating their skill-match score against completely unrelated
    requirements. Using negative lookbehind/lookahead for alphanumeric
    characters (rather than \\b, since some aliases contain characters
    like "#" or "." where \\b's word-character definition gets murky)
    ensures the alias is only counted when it's not glued to more letters
    or digits on either side — "java" still matches "Core Java" or
    "Java/Spring" fine, just not "JavaScript".
    """
    pattern = r'(?<![a-zA-Z0-9])' + re.escape(alias) + r'(?![a-zA-Z0-9])'
    return re.search(pattern, text_str) is not None


def extract_skills(text_str: Optional[str]) -> List[str]:
    """
    Rule/keyword dictionary skill extraction — per doc Task 2.
    Returns sorted set of canonical skills found in text.
    """
    if not text_str:
        return []
    lower = text_str.lower()
    found = set()
    for canonical, aliases in SKILL_ALIASES.items():
        if any(_alias_matches(alias, lower) for alias in aliases):
            found.add(canonical)
    return sorted(found)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class RematchResponse(BaseModel):
    requirement_id: str
    assignments_created_or_updated: int
    message: Optional[str] = None
    status: Optional[str] = None


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
    summary="Queue a single requirement for rematch (done by the cron worker)",
)
async def rematch_requirement(
    requirement_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Matching runs only in rap_python_cron. This endpoint marks the
    requirement row with status = 'REMATCH_REQUESTED' so the cron worker
    picks it up and re-matches it without touching gmail_emails.status_desc.
    """
    _require_role(current_user, "ADMIN")

    req_result = await db.execute(select(Requirement).where(Requirement.id == requirement_id))
    requirement = req_result.scalars().first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    requirement.status = "REMATCH_REQUESTED"
    await db.commit()
    logger.info("Rematch queued for requirement_id=%s by user=%s", requirement_id, current_user.email)

    return RematchResponse(
        requirement_id=str(requirement_id),
        assignments_created_or_updated=0,
        message="Rematch queued — matches will refresh in a few seconds.",
        status="REMATCH_REQUESTED",
    )


@router.post(
    "/api/admin/requirements/match-all",
    summary="Queue open requirements for matching (done by the cron worker)",
)
async def match_all_requirements(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Matching runs only in rap_python_cron. This endpoint marks open
    requirements with status = 'REMATCH_REQUESTED' so the cron worker
    picks them up and re-matches them without touching gmail_emails.status_desc.
    """
    _require_role(current_user, "ADMIN")

    result = await db.execute(
        text("""
            UPDATE requirements
            SET status = 'REMATCH_REQUESTED'
            WHERE status NOT IN ('CLOSED', 'REJECTED', 'REMATCH_REQUESTED', 'REMATCHING')
            RETURNING id
        """)
    )
    queued_count = len(result.all())
    await db.commit()

    logger.info("Match-all queued (%d requirements) by user=%s", queued_count, current_user.email)
    return {
        "success": True,
        "started": True,
        "status": "completed",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "total_requirements": queued_count,
        "processed_requirements": queued_count,
        "total_assignments": 0,
        "error": None,
        "message": "Matching queued for cron worker.",
    }


@router.get(
    "/api/admin/requirements/match-all/status",
    summary="Poll progress of the bulk match-all run (admin only)",
)
async def get_match_all_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, "ADMIN")
    pending_res = await db.execute(
        text(
            "SELECT COUNT(*) FROM requirements "
            "WHERE status IN ('REMATCH_REQUESTED', 'REMATCHING')"
        )
    )
    pending = pending_res.scalar() or 0
    return {
        "status": "running" if pending > 0 else "completed",
        "started_at": None,
        "finished_at": None if pending > 0 else datetime.now(timezone.utc).isoformat(),
        "total_requirements": pending,
        "processed_requirements": 0,
        "total_assignments": 0,
        "error": None,
    }


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
