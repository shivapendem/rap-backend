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
#   match_consultant()        — inverse of match_requirement: scores one consultant
#                                against all open requirements, upserts matches
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


def _canonical_requirement_skills(requirement: Requirement) -> List[str]:
    """
    Map a requirement's raw extracted skills (parser.py's parsed_fields,
    falling back to a JD text scan) to the same canonical SKILL_ALIASES
    vocabulary _consultant_skills() already uses. Shared by validate_match()
    (generic-title skill fallback) and score_match() (skill score), so both
    always compare like-for-like canonical names instead of raw recruiter
    wording on one side and canonical keys on the other.
    """
    raw_skills = []
    if requirement.parsed_fields and requirement.parsed_fields.get("skills"):
        raw_skills = requirement.parsed_fields.get("skills")

    canonical = set()
    for raw_skill in raw_skills:
        lower = str(raw_skill).lower()
        for canon, aliases in SKILL_ALIASES.items():
            if any(alias in lower for alias in aliases) or lower == canon:
                canonical.add(canon)

    if not canonical:
        jd_text = (requirement.job_description or "")[:1500]
        canonical = set(extract_skills(jd_text))

    return sorted(canonical)


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


def _role_signal_tokens(text: Optional[str]) -> set:
    """
    Clean punctuation, lowercase, split, and strip words that carry no
    real matching signal on their own: scheduling/logistics noise
    ("remote", "contract", "urgent") and generic job-title nouns
    ("developer", "engineer", "analyst", ...). Shared by both sides of
    score_role()'s comparison — see the BUG FIX note there for why both
    sides need the same filtering, not just the requirement's.
    """
    if not text:
        return set()
    clean = re.sub(r'[^a-zA-Z0-9\s]', ' ', text).lower()
    raw_tokens = set(clean.split())
    noise_words = {
        "remote", "onsite", "hybrid", "contract", "months", "years", "w2", "c2c",
        "c2h", "h1b", "urgently", "urgent", "hiring", "immediate", "sr", "senior",
        "jr", "junior", "mid", "level", "role", "position",
        "developer", "engineer", "consultant", "analyst", "architect", "manager",
        "specialist", "tester", "admin", "associate", "professional", "lead",
        "expert", "programmer", "administrator", "scientist", "researcher",
    }
    return {t for t in raw_tokens if t not in noise_words and not t.isdigit() and len(t) > 1}


def score_role(
    requirement_role: Optional[str],
    consultant_preferred_roles: Optional[str],
    experiences: Optional[List[ConsultantExperience]] = None,
    requirement_skills: Optional[List[str]] = None,
    consultant_skills: Optional[List[str]] = None,
) -> float:
    """
    Role title token overlap — smarter token-based comparison.

    BUG FIX: filters out punctuation and common noise words (e.g. 'remote', 'senior',
    'contract') from the requirement role so that a consultant doesn't get heavily
    penalized just because the JD title string was noisy (e.g. "Sr. Java Developer
    (Remote) - 6 months" vs "Java Developer").

    BUG FIX ("matching every requirement with 'developer' in the title" /
    "consultant title is just Developer"): generic job-title nouns were
    treated as real matching tokens, not noise, and only the requirement
    side was ever filtered — the consultant's own preferred_roles and
    experience titles went in completely raw. That created two mirrored
    problems: (1) any requirement titled "X Developer" shared the bare
    word "developer" with a consultant whose title also said "developer",
    regardless of actual technology, and (2) a consultant whose own title
    was itself just "Developer" (with real skills like Java/Spring Boot
    sitting in their skills fields, not their title) could never score
    well against even an exact-tech-match requirement, since "developer"
    alone carried no real signal to compare.

    Now: both sides are filtered through the same noise/generic-word list
    via _role_signal_tokens(), and whenever EITHER side ends up with zero
    signal tokens after filtering, this falls back to comparing
    requirement_skills against consultant_skills directly instead of
    guessing from leftover generic words. If there's no skill data to
    fall back on either, it lands at a neutral 50% — genuinely unknown,
    neither a confident pass nor fail.
    """
    if not requirement_role:
        return 0.0

    req_tokens = _role_signal_tokens(requirement_role)

    pref_tokens = set()
    if consultant_preferred_roles:
        pref_tokens |= _role_signal_tokens(consultant_preferred_roles)
    if experiences:
        for exp in experiences:
            if exp.role_title:
                pref_tokens |= _role_signal_tokens(exp.role_title)

    if not req_tokens or not pref_tokens:
        # Either side's title carries zero specific signal — fall back to
        # skills instead of reintroducing noise/generic words (the old
        # bug) or defaulting to a blind 0.
        if requirement_skills and consultant_skills:
            req_skill_set = set(requirement_skills)
            if req_skill_set:
                overlap_skills = req_skill_set & set(consultant_skills)
                return round((len(overlap_skills) / len(req_skill_set)) * 100, 2)
        return 50.0  # Genuinely unknown — neutral, not a confident pass or fail.

    overlap = req_tokens & pref_tokens
    
    # Calculate ratio against meaningful tokens
    ratio = len(overlap) / len(req_tokens)
    
    # Boost: if they matched the core technology (e.g. "java") which is usually 1-2 words.
    # If they match 2+ tokens, it's usually a solid match.
    if len(overlap) >= 2 and ratio < 0.8:
        ratio = min(1.0, ratio + 0.3) # 30% boost for matching multiple core tokens
        
    # If it's a 1-token requirement and they matched it
    if len(req_tokens) == 1 and len(overlap) == 1:
        ratio = 1.0

    return round(ratio * 100, 2)


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


def validate_match(
    requirement: Requirement,
    consultant: Consultant,
    experiences: List[ConsultantExperience],
    *,
    requirement_skills: Optional[List[str]] = None,
) -> bool:
    """
    Strict step-by-step validation pipeline.
    A candidate must pass all gates to be considered for a match.

    PERFORMANCE: requirement_skills can be precomputed ONCE per requirement
    by bulk callers (matching_router.py's run_matching_for_requirement,
    match_requirement()/match_consultant() below) and passed in here,
    instead of every single consultant in the loop re-running
    _canonical_requirement_skills() (a parsed_fields/JD scan) on the exact
    same requirement. This is what previously caused a real timeout on a
    dataset with 37,000+ open requirements — see the identical note on
    score_match() below.
    """
    # 1. Title Validation
    # BUG FIX: the old approach only special-cased an EXACT string match
    # against a fixed list of generic titles ("developer", "software
    # developer", etc.), prepending requirement skills as raw words into
    # the role string before scoring. That missed noise-word-prefixed
    # variants entirely — "Senior Developer", "Sr Developer", "Remote
    # Developer" never matched the exact-string check — and used a
    # cruder scoring path than a real skill comparison. score_role()
    # itself now handles this generally: whenever the title carries zero
    # specific signal after noise-word filtering, regardless of exact
    # phrasing, it falls back to comparing requirement_skills against
    # consultant_skills directly. Just supply both skill lists.
    req_skills = requirement_skills if requirement_skills is not None else _canonical_requirement_skills(requirement)
    consultant_skills = _consultant_skills(consultant)

    role_raw = score_role(
        requirement.role,
        consultant.preferred_roles,
        experiences,
        requirement_skills=req_skills,
        consultant_skills=consultant_skills,
    )
    if role_raw < 70.0:
        return False

    # 2. Employment Type Validation
    # BUG FIX: only "N/A" was excluded here, but parser.py's real default
    # when a JD never states an employment type is the literal string
    # "UNKNOWN" — never "N/A". That let "UNKNOWN" survive this filter,
    # which made req_types non-empty, which activated the gate below as if
    # the requirement definitely required contract work (C2C/C2B), wrongly
    # rejecting FULLTIME/W2-only consultants on requirements that never
    # actually stated an employment type at all. Treat "UNKNOWN" the same
    # as "N/A" here — both mean "not really specified".
    req_types = [t.upper() for t in (requirement.employment_types or []) if t and t.upper() not in ("N/A", "UNKNOWN")]
    if req_types:
        cons_types = [t.upper() for t in (consultant.preferred_employment_types or []) if t]
        is_fulltime = "FULLTIME" in req_types
        if not is_fulltime:
            # Contract-based job: candidate must support C2C or C2B
            if "C2C" not in cons_types and "C2B" not in cons_types:
                return False
        else:
            # BUG FIX: full-time requirements never checked the consultant's
            # own preference at all — every consultant passed automatically,
            # including one who only wants contract work (C2C/C2B) and
            # explicitly does not support FULLTIME. Mirror the contract-side
            # check above: reject only when the consultant HAS stated a
            # preference and FULLTIME isn't in it. An empty/unspecified
            # preference is treated as open to anything, consistent with
            # how N/A/UNKNOWN is handled everywhere else in this function.
            if cons_types and "FULLTIME" not in cons_types:
                return False

    # 3. Visa / Work Auth Validation
    jd = (requirement.job_description or "").lower()
    req_batch = 0 # 0 means N/A (passes all)
    
    # Simple regex/keyword scan for work auth in JD
    # BUG FIX: bare "TN" (the far more common way recruiters actually write
    # it, e.g. "Must have TN status") was never detected as batch 3 — only
    # the two-word phrase "tn visa" was, which real JDs rarely use. That
    # silently misclassified these requirements as req_batch=0 (N/A), which
    # passes every consultant through with no work-auth restriction at all,
    # instead of correctly restricting to batch-3 consultants only. The
    # consultant's own work_authorization field already treats bare "TN" as
    # batch 3 (see cons_batch mapping below) — this brings the requirement
    # side in line with that.
    if re.search(r'\b(usc|gc|green card|us citizen|citizens only|citizen|gc ead|tn|tn visa|l1|u visa)\b', jd):
        req_batch = 3
    elif re.search(r'\b(h1b|h1-b)\b', jd):
        req_batch = 2
    elif re.search(r'\b(f1|opt|cpt|stem opt)\b', jd):
        req_batch = 1
        
    if req_batch > 0:
        cons_auth = (consultant.work_authorization or "").upper().replace(" ", "").replace("-", "")
        cons_batch = 0
        if cons_auth in ["F1", "OPT", "CPT", "STEMOPT", "F1OPT"]:
            cons_batch = 1
        elif cons_auth in ["H1B"]:
            cons_batch = 2
        elif cons_auth in ["USC", "USCITIZEN", "CITIZEN", "GC", "GREENCARD", "GCEAD", "L1", "TN", "UVISA"]:
            cons_batch = 3
        elif cons_auth:
            # For unmapped known consultant auths, assume batch 3 (strictest/safest)
            cons_batch = 3
        
        # If requirement needs F1 or H1B (1 or 2), it pushes to all batches (1, 2, 3)
        # If requirement needs Batch 3, it only pushes to Batch 3 candidates.
        if req_batch == 3 and cons_batch < 3:
            return False

    # 4. Experience Validation
    required_years = _parse_min_years_required(requirement)
    if required_years is not None and required_years > 0:
        years = float(consultant.total_experience_years or 0)
        if years <= 0 and experiences:
            years = _calculate_total_experience_years(experiences)
        
        # Candidate must be within -2 years of requirement. N/A allows all.
        lower_bound = max(0, required_years - 2)
        if years < lower_bound:
            return False

    # 5. Location Validation
    # N/A defaults to passing all. (Skipped for now)

    return True


def score_match(
    requirement: Requirement,
    consultant: Consultant,
    experiences: List[ConsultantExperience],
    *,
    requirement_skills: Optional[List[str]] = None,
) -> dict:
    """
    Combine all 6 factors per doc Task 1 weights:
      skill 40%, role 20%, experience 15%, employment 10%, location 10%, auth 5%
    Returns dict with total score, breakdown, matched/missing skills, and reason.

    PERFORMANCE: requirement_skills is IDENTICAL for every consultant scored
    against the same requirement (it's a pure function of the requirement).
    Bulk callers now compute it ONCE and pass it in via this kwarg, instead
    of every consultant in the loop silently re-running the same
    parsed_fields/JD scan. Still defaults to None and gets computed
    internally when not supplied, so any other caller keeps working
    unchanged.
    """
    # BUG FIX: this duplicated the same raw-skill-to-canonical-name mapping
    # now shared via _canonical_requirement_skills() (see validate_match(),
    # which needs the identical list for its generic-title skill fallback).
    # Using one shared helper keeps both places in sync instead of two
    # copies that could silently drift apart.
    if requirement_skills is None:
        requirement_skills = _canonical_requirement_skills(requirement)
    consultant_skills = _consultant_skills(consultant)

    skill_raw, matched_skills, missing_skills = score_skills(requirement_skills, consultant_skills)
    # BUG FIX: role_raw here previously never received requirement_skills/
    # consultant_skills, so score_match()'s actual displayed/ranked score
    # never benefited from the generic-title skill fallback — only
    # validate_match()'s pass/fail gate did (and only for exact-string
    # generic matches, per the fix there). Passing skills through here
    # closes that gap so the score used for ranking/display and the score
    # used for the hard gate are now computed consistently.
    role_raw = score_role(
        requirement.role,
        consultant.preferred_roles,
        experiences,
        requirement_skills=requirement_skills,
        consultant_skills=consultant_skills,
    )
    exp_raw = score_experience(requirement, consultant, experiences)
    employment_raw = score_employment_type(requirement.employment_types, consultant.preferred_employment_types)
    location_raw = score_location(requirement, consultant, experiences)
    auth_raw = score_work_auth(requirement, consultant)

    skill_score = skill_raw * 0.20
    role_score = role_raw * 0.50
    exp_score = exp_raw * 0.10
    employment_score = employment_raw * 0.05
    location_score = location_raw * 0.10
    auth_score = auth_raw * 0.05

    total = round(skill_score + role_score + exp_score + employment_score + location_score + auth_score, 2)

    # If the role match is extremely low, penalize the entire match.
    # We do not want to surface a 70% match just because skills/location match
    # when the role is completely wrong.
    if role_raw < 15.0:
        total = round(total * 0.2, 2)  # 80% penalty for completely missing the role

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
        # Raw, pre-weight percentages (0-100) for each factor — lets the UI
        # show WHY a total came out a certain way, e.g. "Role: 100% (raw) →
        # 50.0 pts (weighted)", instead of just a single blended percentage.
        "score_breakdown": {
            "skill": {"raw": round(skill_raw, 2), "weight": 0.20, "weighted": round(skill_score, 2)},
            "role": {"raw": round(role_raw, 2), "weight": 0.50, "weighted": round(role_score, 2)},
            "experience": {"raw": round(exp_raw, 2), "weight": 0.10, "weighted": round(exp_score, 2)},
            "employment": {"raw": round(employment_raw, 2), "weight": 0.05, "weighted": round(employment_score, 2)},
            "location": {"raw": round(location_raw, 2), "weight": 0.10, "weighted": round(location_score, 2)},
            "auth": {"raw": round(auth_raw, 2), "weight": 0.05, "weighted": round(auth_score, 2)},
        },
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

    # PERFORMANCE: compute once per requirement, reuse for every consultant
    # instead of every consultant in the loop recomputing the identical
    # skill list — see the PERFORMANCE note on validate_match()/score_match().
    requirement_skills = _canonical_requirement_skills(requirement)

    # ── Scoring loop — pure in-memory computation, zero DB round trips per iteration ──
    for consultant in consultants:
        experiences = experiences_by_consultant.get(consultant.id, [])

        # BUG FIX: this pipeline (match_requirement/match_consultant, backing
        # /rematch and /match-all) previously went straight to score_match()
        # with no gate at all — unlike matching_router.py's pipeline, which
        # runs validate_match()'s 4 hard checks (role >=70%, employment type,
        # work auth batch, experience floor) first. That gap let mismatched
        # candidates clear this pipeline's 60% weighted-total threshold on
        # combined soft signals alone — e.g. a Java consultant scoring 50%
        # on a "Python Developer" role (shared word "developer") could still
        # total past 60% if location/employment/experience scored well,
        # even though the same candidate correctly fails matching_router.py's
        # stricter 70% role gate. validate_match() is defined in this same
        # file, so no import is needed here (unlike matching_router.py,
        # which imports it from phase4). Bringing both pipelines in line so
        # results don't differ depending on which entry point ran the match.
        if not validate_match(requirement, consultant, experiences, requirement_skills=requirement_skills):
            continue

        result = score_match(requirement, consultant, experiences, requirement_skills=requirement_skills)

        if result["total"] < MATCH_THRESHOLD:
            continue

        existing = existing_matches_by_consultant.get(consultant.id)

        from sqlalchemy.exc import IntegrityError
        try:
            async with db.begin_nested():
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
                    existing.score_breakdown = result["score_breakdown"]
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
                        score_breakdown=result["score_breakdown"],
                        status="ASSIGNED",
                    ))
                await db.flush()
        except IntegrityError:
            stmt = select(RequirementConsultantMatch).where(
                RequirementConsultantMatch.requirement_id == requirement_id,
                RequirementConsultantMatch.consultant_id == consultant.id
            )
            res = await db.execute(stmt)
            existing = res.scalars().first()
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
                existing.score_breakdown = result["score_breakdown"]
                await db.flush()


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


async def match_consultant(db: AsyncSession, consultant_id: int) -> int:
    """
    Inverse of match_requirement: score ONE consultant against all
    still-open requirements and upsert into requirement_consultant_matches.
    Called automatically when a consultant updates their profile so their
    matches reflect the new skills/roles/etc. without an admin re-run.
    Returns the number of requirements where they now meet MATCH_THRESHOLD.
    """
    cons_result = await db.execute(select(Consultant).where(Consultant.id == consultant_id))
    consultant = cons_result.scalars().first()
    if not consultant or consultant.status != "ACTIVE":
        return 0

    # Open requirements only — skip terminal states.
    reqs_result = await db.execute(
        select(Requirement).where(Requirement.status.notin_(["CLOSED", "REJECTED"]))
    )
    requirements = reqs_result.scalars().all()
    if not requirements:
        return 0

    exp_result = await db.execute(
        select(ConsultantExperience).where(ConsultantExperience.consultant_id == consultant_id)
    )
    experiences = exp_result.scalars().all()

    req_ids = [r.id for r in requirements]
    existing_result = await db.execute(
        select(RequirementConsultantMatch).where(
            RequirementConsultantMatch.consultant_id == consultant_id,
            RequirementConsultantMatch.requirement_id.in_(req_ids),
        )
    )
    existing_by_req = {m.requirement_id: m for m in existing_result.scalars().all()}

    match_count = 0
    for requirement in requirements:
        # BUG FIX: same gap as match_requirement() above — this inverse
        # pipeline (runs automatically when a consultant updates their
        # profile) also skipped validate_match()'s hard gates entirely.
        # Applying the same fix here so a consultant's auto-refreshed
        # matches are validated identically to a requirement-triggered
        # rematch, regardless of which direction triggered the run.
        if not validate_match(requirement, consultant, experiences):
            continue

        result = score_match(requirement, consultant, experiences)

        if result["total"] < MATCH_THRESHOLD:
            continue

        existing = existing_by_req.get(requirement.id)

        from sqlalchemy.exc import IntegrityError
        try:
            async with db.begin_nested():
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
                    existing.score_breakdown = result["score_breakdown"]
                else:
                    db.add(RequirementConsultantMatch(
                        requirement_id=requirement.id,
                        consultant_id=consultant_id,
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
                        score_breakdown=result["score_breakdown"],
                        status="ASSIGNED",
                    ))
                await db.flush()
        except IntegrityError:
            stmt = select(RequirementConsultantMatch).where(
                RequirementConsultantMatch.requirement_id == requirement.id,
                RequirementConsultantMatch.consultant_id == consultant_id
            )
            res = await db.execute(stmt)
            existing = res.scalars().first()
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
                existing.score_breakdown = result["score_breakdown"]
                await db.flush()

        match_count += 1

    await db.commit()
    logger.info(
        "Auto-matched consultant_id=%s across %d open requirements — %d matches",
        consultant_id, len(requirements), match_count,
    )
    return match_count


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