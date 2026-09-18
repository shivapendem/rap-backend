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
import asyncio
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

# BUG FIX (Run Engine timing out at 300s): making the matching engine
# re-validate every EXISTING row on every run — not just skip it via
# existing_pairs the way it used to — was the right fix for stale rows
# never getting caught, but it meant re-scoring the ENTIRE existing
# dataset every single click, every single time, regardless of whether
# anything actually changed. With thousands of requirements now in the
# system, that's tens of thousands of full re-validations per click,
# which is exactly what pushed past the request timeout.
#
# MATCHING_LOGIC_VERSION tags every row with the code version it was last
# validated under (stored in the existing JSONB score_breakdown /
# matching_info fields — no schema migration needed). A row already
# tagged with the CURRENT version gets skipped fast, restoring the old
# performance for the common case (nothing changed since the last run).
# A row from before a logic change (untagged, or tagged with an older
# version) still gets the full re-check exactly once — bump this string
# whenever scoring/gate logic changes, and every affected row gets
# re-validated on the next run, then stays skipped until the next bump.
MATCHING_LOGIC_VERSION = "2026-09-05-role-mandatory-no-ai-skill-topup"

# BUG FIX (rap-backend crash loop — SIGABRT under pm2, hundreds of
# restarts): PostgreSQL's wire protocol caps bind parameters at 32,767
# per query (a signed int16 field in the Bind message). match_consultant()
# below can build an .in_(ids_needing_scoring) clause covering every open
# requirement — 44,000+ in production — right after a MATCHING_LOGIC_VERSION
# bump, or on a cold cache. A single query at that scale isn't just slow,
# it's outright invalid for the protocol; asyncpg does not surface this as
# a clean, catchable Python exception in every case, which is what was
# taking the whole process down rather than just failing one request.
#
# _chunk_ids() lets any .in_(some_id_list) call stay well under that limit
# regardless of how large the id list gets, by splitting it into fixed-size
# batches and letting the caller merge results — same final data, more
# (cheap) round trips instead of one query the database can't accept.
_SQL_IN_CHUNK_SIZE = 5000


def _chunk_ids(ids: list[int], size: int = _SQL_IN_CHUNK_SIZE):
    """Yield `ids` in fixed-size slices, preserving order. size=5000 keeps
    every chunk far under Postgres's 32,767-bind-parameter hard limit
    while still keeping the query count small (44,000 ids -> ~9 queries)."""
    for i in range(0, len(ids), size):
        yield ids[i:i + size]

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


def _alias_matches(alias: str, text: str) -> bool:
    """
    Word-boundary-aware check for whether `alias` genuinely appears in
    `text`, not just as a substring of a longer, unrelated word.

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
    return re.search(pattern, text) is not None


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
        if any(_alias_matches(alias, lower) for alias in aliases):
            found.add(canonical)
    return sorted(found)


def _consultant_skills(consultant: Consultant) -> List[str]:
    """Combine primary + secondary skills text into a single skill list."""
    combined = ", ".join(filter(None, [consultant.primary_skills, consultant.secondary_skills]))
    return extract_skills(combined)


def _requirement_skills(requirement: Requirement) -> List[str]:
    """
    Canonical skill list for a requirement — shared by score_match() and
    validate_match() so the extraction logic exists in exactly one place.
    Prefers parser.py's tightly-scoped parsed_fields['skills'] (mapped
    through SKILL_ALIASES to canonical names); falls back to scanning the
    first 1500 chars of the raw JD when parsed_fields has nothing usable.
    """
    requirement_skills: List[str] = []
    if requirement.parsed_fields and requirement.parsed_fields.get("skills"):
        raw_skills = requirement.parsed_fields.get("skills")
        canonical_req = set()
        for raw_skill in raw_skills:
            lower = str(raw_skill).lower()
            for canonical, aliases in SKILL_ALIASES.items():
                # BUG FIX: same substring-collision bug as extract_skills()
                # (see _alias_matches() docstring) — a raw skill like
                # "JavaScript" would false-match the "java" alias via
                # plain substring containment. Reuses the same
                # word-boundary-aware check.
                if any(_alias_matches(alias, lower) for alias in aliases) or lower == canonical:
                    canonical_req.add(canonical)
        requirement_skills = sorted(canonical_req)

    if not requirement_skills:
        jd_text = (requirement.job_description or "")[:1500]
        requirement_skills = extract_skills(jd_text)

    return requirement_skills


# ---------------------------------------------------------------------------
# Role-matching vocabulary — role-matching-fix spec.
#
# GENERIC_ROLE_WORDS separates a role title into a "domain" (specialization)
# part and a "generic" part, so score_role() can tell "Java Developer" vs
# "Python Developer" apart instead of matching on the shared word "Developer"
# alone. SYNONYMS expands common acronyms into their spelled-out words
# BEFORE domain/generic tokens are compared, so e.g. "SRE" and "Site
# Reliability Engineer" land on overlapping token sets. ADJACENT_ROLES is a
# small, hand-curated, extendable table of role phrases treated as a partial
# (60%) match for each other even with zero direct domain-word overlap.
# ---------------------------------------------------------------------------

GENERIC_ROLE_WORDS: set[str] = {
    "developer", "engineer", "analyst", "consultant", "admin", "administrator",
    "lead", "specialist", "manager", "architect", "coordinator", "associate",
    "programmer", "tester", "dev",
    # Merged from a parallel fix on this same file — same principle, a few
    # more generic job-title nouns that carry no specialization signal on
    # their own (e.g. "Data Scientist" vs "Research Scientist" sharing
    # "scientist" alone shouldn't count as a domain match).
    "professional", "expert", "scientist", "researcher",
    # Structural/connector words — describe a JOB-TITLE PATTERN, not a
    # technology or specialization, so they carry no real domain signal on
    # their own. Without these, a consultant whose Preferred Roles field is
    # a long multi-phrase list (a common real-world pattern — e.g. twenty
    # Salesforce role variants, one of which happens to be "Salesforce Full
    # Stack Developer") leaks "full"/"stack"/"web" into their token pool,
    # which then falsely counts as domain overlap against a COMPLETELY
    # unrelated posting like "Java Full Stack Developer" — these generic
    # structural phrases are used identically across every tech stack, so
    # sharing them proves nothing about actual specialization match.
    "full", "stack", "web", "application", "platform", "integration",
    "integrations", "customization", "implementation", "migration",
    "support", "technical", "solution", "solutions",
    # BUG FIX ("Data Analyst" consultant matched "Data Architect" at 100%
    # role overlap): a broad category word like "data" appears across
    # genuinely unrelated specializations — Data Analyst, Data Architect,
    # Data Engineer, Data Scientist, Database Administrator are all
    # different job functions that happen to share this one word. When a
    # requirement's title reduces to JUST "data" after generic-stripping
    # (e.g. "Data Architect" -> {"data"} once "architect" is stripped),
    # req_domain has exactly one token — so any single shared word gives
    # ratio = 1/1 = 100%, the same single-token-inflation bug the
    # structural-connector-word fix above exists to prevent, just with a
    # domain-sounding word instead of a structural one. Same principle:
    # too broad on its own to signal real specialization.
    "data",
}

# Bare single-letter language names that the length filter (len(t) > 1)
# would otherwise silently drop — "C" and "R" are real, meaningful domain
# tokens on their own, not noise.
SHORT_DOMAIN_TOKENS: set[str] = {"c", "r"}

SYNONYMS: dict[str, set[str]] = {
    "qa": {"quality", "assurance"},
    "sre": {"site", "reliability", "engineer"},
    "etl": {"extract", "transform", "load"},
    "ba": {"business", "analyst"},
    "pm": {"project", "manager"},
    "ui": {"user", "interface"},
    "ux": {"user", "experience"},
    "ml": {"machine", "learning"},
    "ai": {"artificial", "intelligence"},
    "devops": {"development", "operations"},
}

# Key phrase -> set of adjacent phrases considered partial matches for it.
# Checked symmetrically (either side can hold the key phrase or an adjacent
# phrase) inside _adjacent_role_credit().
ADJACENT_ROLES: dict[str, set[str]] = {
    "devops engineer": {"sre", "site reliability engineer", "platform engineer"},
    "business analyst": {"data analyst", "systems analyst", "product analyst"},
    "qa engineer": {"sdet", "test engineer"},
}

# ---------------------------------------------------------------------------
# Stage 2 — Work Authorization batches (post-role-match filter pipeline spec)
#
#   Batch 1 = F1 / STEM OPT            (least restrictive requirement)
#   Batch 2 = H1B
#   Batch 3 = USC / GC / GC EAD / L1 / TN / U Visa   (most restrictive)
#
# Push rule: a requirement asking for Batch 1 or Batch 2 work auth pushes to
# ALL consultants regardless of batch (everyone is eligible to be considered
# for an F1- or H1B-friendly role). A requirement asking for a Batch 3 work
# auth ONLY pushes to Batch 3 consultants — Batch 1/2 consultants are
# filtered out, since USC/GC-only roles genuinely cannot take them.
# ---------------------------------------------------------------------------

WORK_AUTH_BATCH_1: set[str] = {"F1", "STEMOPT"}
WORK_AUTH_BATCH_2: set[str] = {"H1B"}
WORK_AUTH_BATCH_3: set[str] = {"USC", "GC", "GCEAD", "L1", "TN", "UVISA"}


def get_batch(work_auth_value: Optional[str]) -> Optional[int]:
    """Normalize a work-authorization string (spaces/hyphens stripped,
    uppercased) and return its batch number (1/2/3), or None if it's
    empty or doesn't map to a known batch."""
    if not work_auth_value:
        return None
    v = work_auth_value.upper().replace(" ", "").replace("-", "")
    if v in WORK_AUTH_BATCH_1:
        return 1
    if v in WORK_AUTH_BATCH_2:
        return 2
    if v in WORK_AUTH_BATCH_3:
        return 3
    return None


def work_auth_passes(requirement_work_auth: Optional[str], consultant_work_auth: Optional[str]) -> tuple[bool, str]:
    """
    Stage 2 — Work Authorization push rule (batched, see module docstring
    above). N/A/empty on EITHER side passes everyone for this field — same
    wildcard rule as every other Stage 1-4 filter. Only the 3 defined
    batches (F1/STEM OPT, H1B, USC/GC/GC EAD/L1/TN/U Visa) are recognized;
    a value outside them fails rather than falling back to a guess, with a
    warning logged so an unmapped value gets noticed instead of silently
    matching one way or the other.
    Returns (passes, reason) — reason is used for the stage-rejection audit
    log in validate_match().
    """
    if not requirement_work_auth or requirement_work_auth.strip().upper() == "N/A":
        return True, "requirement work_auth is N/A — passes all"

    if not consultant_work_auth or consultant_work_auth.strip().upper() == "N/A":
        return True, "consultant work_authorization is N/A — matches requirement"

    req_batch = get_batch(requirement_work_auth)

    if req_batch is None:
        logger.warning(
            "work_auth_passes: unmapped requirement work_auth value %r — treating as no match",
            requirement_work_auth,
        )
        return False, f"unmapped requirement work_auth {requirement_work_auth!r} — no known batch, fails"

    if req_batch in (1, 2):
        return True, f"requirement work_auth is Batch {req_batch} — pushes to all batches"

    # req_batch == 3 — only a consultant whose own value maps to Batch 3
    # passes. No fallback for an unmapped-but-stated consultant value —
    # only the 3 defined batches count.
    cons_batch = get_batch(consultant_work_auth)
    if cons_batch == 3:
        return True, "requirement is Batch 3, consultant is Batch 3 — match"
    return False, f"requirement requires Batch 3; consultant is Batch {cons_batch or 'unmapped'} ({consultant_work_auth!r})"


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


def _tokenize_role(text: Optional[str]) -> set[str]:
    """
    Clean punctuation, lowercase, split, strip the existing noise words
    (remote/onsite/contract/h1b/senior/junior/etc — unchanged from before),
    then expand any SYNONYMS acronym into its spelled-out words. The
    original token is kept alongside its expansion (union, not replace) so
    an exact acronym-to-acronym match still works on its own.

    BUG FIX (single-char/symbol language names silently dropped): the
    punctuation-stripping regex used to remove '#' and '+' entirely before
    splitting, so "C#" became "c" and "C++" became "c" — then the length
    filter (len(t) > 1) discarded that single leftover character, and bare
    "C"/"R" (no symbol at all) were dropped outright too. A title whose
    ONLY domain word was one of these ("C# Developer") lost its sole
    specialization signal and fell through to score_role()'s bare-generic
    branch. '#' and '+' are now preserved through the regex so "c#"/"c++"
    survive as their own tokens, and SHORT_DOMAIN_TOKENS whitelists bare
    single-letter language names past the length filter.
    """
    if not text:
        return set()
    clean = re.sub(r'[^a-zA-Z0-9\s#+]', ' ', text).lower()
    raw_tokens = {
        t for t in clean.split()
        if not t.isdigit() and (len(t) > 1 or t in SHORT_DOMAIN_TOKENS)
    }

    noise_words = {
        "remote", "onsite", "hybrid", "contract", "months", "years", "w2", "c2c",
        "c2h", "h1b", "urgently", "urgent", "hiring", "immediate", "sr", "senior",
        "jr", "junior", "mid", "level", "role", "position",
    }
    tokens = {t for t in raw_tokens if t not in noise_words}
    # BUG FIX (all-noise titles used noise words as fake domain signal): a
    # role field that was ENTIRELY noise words (e.g. "Senior Remote
    # Contract") used to fall back to treating those noise words
    # themselves as real domain tokens ("tokens = raw_tokens"). A
    # consultant title also containing "remote" or "contract" would then
    # register as genuine specialization overlap — meaningless signal
    # masquerading as a real match. Removed: an all-noise title now
    # correctly yields an empty set, which score_role()'s existing "no
    # data" neutral branches already handle correctly on their own.

    expanded = set(tokens)
    for t in tokens:
        if t in SYNONYMS:
            expanded |= SYNONYMS[t]
    return expanded


def _adjacent_role_credit(req_tokens: set[str], pref_tokens: set[str]) -> bool:
    """
    Step 3(d) exception — hand-curated partial credit for role phrases that
    describe closely related work even with zero direct domain-word overlap
    (e.g. "DevOps Engineer" vs "SRE"). Checked symmetrically: either side
    can hold the ADJACENT_ROLES key phrase.

    The final overlap check is restricted to DOMAIN words only (generic
    words stripped from both the adjacent phrase and the other side) —
    without this, two unrelated roles that merely share a generic word
    (e.g. "QA Engineer" and "Site Reliability Engineer" both containing
    "Engineer") would trivially satisfy the adjacency check via
    "Test Engineer" from the qa-engineer entry's own adjacent set, which
    is exactly the kind of generic-word-only false match this whole
    fix exists to eliminate.
    """
    for key_phrase, adjacent_set in ADJACENT_ROLES.items():
        key_tokens = set(key_phrase.split())
        for source_tokens, other_tokens in ((req_tokens, pref_tokens), (pref_tokens, req_tokens)):
            if key_tokens.issubset(source_tokens):
                other_domain = other_tokens - GENERIC_ROLE_WORDS
                for adj_phrase in adjacent_set:
                    adj_tokens = set(adj_phrase.split())
                    for t in list(adj_tokens):
                        if t in SYNONYMS:
                            adj_tokens |= SYNONYMS[t]
                    adj_domain = adj_tokens - GENERIC_ROLE_WORDS
                    if adj_domain & other_domain:
                        return True
    return False


def _known_generic_phrase_domain(req_tokens: set[str]) -> bool:
    """
    BUG FIX (generic-only titles like "Platform Engineer" always scored a
    flat neutral 50): GENERIC_ROLE_WORDS strips "platform" as a structural
    connector word, so a requirement titled exactly "Platform Engineer"
    reduces to zero domain words (both tokens generic) — hitting
    score_role()'s bare-generic branch and returning a flat neutral 50 for
    every consultant, even though ADJACENT_ROLES already lists "platform
    engineer" as a real, distinct specialization (adjacent to DevOps/SRE).
    The adjacency table entry meant for exactly this case never got a
    chance to fire, because the bare-generic branch returned before
    _adjacent_role_credit() was ever consulted.

    Checked before falling back to the neutral 50: does the WHOLE title's
    token set exactly match a known phrase (either an ADJACENT_ROLES key
    or one of its listed adjacent phrases)? If so, the phrase as a whole
    is a real, known specialization — score_role() should treat it as
    domain-bearing and let the normal domain-overlap / adjacency-credit
    logic actually run, instead of giving up early.
    """
    for key_phrase, adjacent_set in ADJACENT_ROLES.items():
        known_phrases = {key_phrase} | adjacent_set
        for phrase in known_phrases:
            if set(phrase.split()) == req_tokens:
                return True
    return False


def _title_embedded_skills(requirement_role: Optional[str]) -> List[str]:
    """
    Skills named INSIDE the requirement's role/title text itself (e.g.
    "ai", "react" out of "Full-Stack Developer with AI & React") — a
    narrower, more targeted set than _requirement_skills()'s full-JD scan.
    Reuses the same SKILL_ALIASES dictionary/extract_skills() so a skill
    word means the same thing everywhere in this file. Deliberately reads
    the FULL raw title (not the clear-role-only text below) — a skill
    word can appear anywhere in the title, not just after a "with"/"using"
    marker, so this stays broad even though role tokenization doesn't.
    """
    return extract_skills(requirement_role)


# BUG FIX ("Full Stack Developer with AI & React" role-matched against a
# consultant's role tokens as if "ai"/"react" were themselves role words):
# score_role()'s domain-word comparison used to tokenize the requirement's
# ENTIRE role/title text, including any skills tacked onto it after a
# "with"/"using" clause — so those skill words became part of req_domain
# and could accidentally register as a role-name match (e.g. against a
# consultant whose preferred role happened to literally be "AI Engineer").
# That's backwards: role matching should compare JOB TITLES only; skills
# mentioned in the title are a separate, secondary signal
# (_title_embedded_skills() above / the top-up mechanism below), never
# blended into the primary role-name tokens. This strips a trailing
# "with/using/w/ <skills...>" clause BEFORE tokenization so only the
# clear job title (e.g. "Full Stack Developer") ever feeds the domain-word
# comparison — the skill words still get considered, just later and only
# as a top-up, never as if they were role-name overlap.
_ROLE_SKILL_CLAUSE_PATTERN = re.compile(r'\s+(?:with|using|w/)\s+', re.IGNORECASE)


def _clear_role_text(requirement_role: str) -> str:
    """
    The job-title-only portion of a requirement's role/title text, with
    any trailing "with/using <skills>" clause removed. Only the FIRST
    such marker is used as the cut point; if none is present the text is
    returned unchanged (nothing to strip, e.g. "React Developer" with no
    "with" clause at all).
    """
    match = _ROLE_SKILL_CLAUSE_PATTERN.search(requirement_role)
    if match:
        cleared = requirement_role[:match.start()].strip()
        return cleared if cleared else requirement_role
    return requirement_role


def score_role(
    requirement_role: Optional[str],
    consultant_preferred_roles: Optional[str],
    experiences: Optional[List[ConsultantExperience]] = None,
    requirement_skills: Optional[List[str]] = None,
    consultant_skills: Optional[List[str]] = None,
) -> float:
    """
    Role title match — deterministic, word/domain-based. Role is the
    MANDATORY, primary signal; skills named inside the title text are a
    secondary top-up, never a substitute for a genuine role match.

    CHANGE (explicit product decision — "role with skill should take high
    priority", AI role-matching removed): this used to call out to Claude
    (claude_service.evaluate_role_match_with_ai) first, whenever the
    consultant had any role data at all, and returned whatever score the
    AI gave — the deterministic word/domain logic below only ever ran as
    a fallback for when the AI call failed. That's been removed entirely.
    Role matching is now ALWAYS this deterministic word/domain comparison
    — same result every time for the same inputs, no live API call, no
    opaque "job function" judgment that could score a role low even when
    the requirement's own title contains the consultant's exact skill
    words (e.g. "...with AI & React").

    NEW — title-embedded-skill top-up: a title's domain words don't
    always show up verbatim in a consultant's PREFERRED ROLE or job
    TITLES (e.g. a requirement titled "...with AI & React" vs a
    consultant whose preferred role is just "Full Stack Developer" but
    whose SKILLS list genuinely includes React) — that's a real signal
    the pure role-name comparison below can't see on its own. So:
      1. ROLE MATCH IS COMPUTED FIRST AND IS MANDATORY — same
         domain-word-overlap logic as before (GENERIC_ROLE_WORDS
         stripped, SYNONYMS-expanded). This is always the base score.
      2. Skills named literally in the requirement's title text
         (extract_skills(requirement_role) — the SAME dictionary used for
         the general skill match, so "AI"/"React" etc. are recognized)
         are compared against the consultant's actual skill list. This is
         a RATIO — matched-title-skills / total-title-skills — never a
         single-word-is-enough credit. A title naming 3 skills where the
         consultant has 1 only fills a third of the gap, not all of it.
      3. Skills only ever TOP UP an ALREADY-PARTIAL role match — they
         close part of the remaining distance to 100, never push a score
         down, and never apply once role match is already 100 (path c
         with full domain overlap) since there's no gap left to fill.
         They also do NOT rescue a genuine, total specialization mismatch
         (path d with zero domain overlap and no adjacent-role credit,
         or the bare-generic-title no-match branch) — role is mandatory,
         so a requirement whose stated domain is flatly different from
         anything in the consultant's role history stays a hard 0
         regardless of any coincidental skill overlap in the title.
         General JD-wide skill matching (score_skills(), the separate
         Matched/Missing skills shown in the UI) is UNCHANGED and still
         carries 0 weight in score_match() per the earlier explicit
         decision — this top-up only ever applies to skill words that
         appear in the title text specifically, not the whole JD.

    requirement_skills is accepted for signature compatibility with
    existing callers but is not used here (that's the full-JD skill list,
    a different, still-0-weighted factor) — only consultant_skills is
    used, to check against the title-extracted skill set.
    """
    # (a) No requirement role text at all — no name to compare against,
    # genuinely unknown either way.
    if not requirement_role or not requirement_role.strip():
        return 50.0

    # Build the consultant's role-token pool (preferred_roles + every
    # experience row's role_title).
    pref_tokens: set[str] = set()
    if consultant_preferred_roles:
        pref_tokens |= _tokenize_role(consultant_preferred_roles)
    if experiences:
        for exp in experiences:
            if exp.role_title:
                pref_tokens |= _tokenize_role(exp.role_title)

    # (b) Consultant has no role data at all — no name to compare against
    # on their side either, genuinely unknown.
    if not pref_tokens:
        return 50.0

    def _title_skill_topup(base_score: float, cap: float = 100.0) -> float:
        """Close part of the gap to `cap` using title-embedded-skill
        overlap — only when there IS a gap (base_score < cap) and the
        consultant has a real skill list to check against.

        `cap` distinguishes two very different "not fully matched"
        situations per the role-is-mandatory rule:
          - Genuine partial role signal already exists (some domain
            overlap, or a hand-curated adjacent-role match) — skills can
            top this all the way up to 100, there's real role evidence
            underneath.
          - Zero role signal or a stated domain CONFLICT (e.g. a
            Salesforce-only consultant against an AI/React requirement,
            or a consultant whose role text has no domain word at all) —
            skills can still move the needle (so it's not stuck at a flat
            0 forever), but are capped well below a real match. Role is
            mandatory: a title's skill words alone — even ALL of them —
            must never make a fundamentally different specialization look
            like a full role match.
        """
        if base_score >= cap or not consultant_skills:
            return min(base_score, cap)
        title_skills = _title_embedded_skills(requirement_role)
        if not title_skills:
            return base_score
        matched = set(title_skills) & set(consultant_skills)
        skill_ratio = len(matched) / len(title_skills)
        topped_up = base_score + (cap - base_score) * skill_ratio
        return round(min(topped_up, cap), 2)

    # Cap for the "no real role signal at all" branches — high enough that
    # a strong title-skill overlap visibly moves a 0 somewhere useful, low
    # enough that it can never read as a confident role match on its own.
    NO_ROLE_SIGNAL_CAP = 55.0
    # Cap for the "genuinely bare/unknown" case specifically (no domain
    # word stated on the requirement side at all — e.g. plain "Full Stack
    # Developer" once its "with <skills>" clause is stripped out by
    # _clear_role_text() above). Not a conflict like NO_ROLE_SIGNAL_CAP's
    # cases — there's no stated specialization to disagree with, just
    # none stated at all — so a strong skill backing is allowed a bit
    # more room to move the neutral 50 baseline, while still staying
    # below the 70-point PASS tier: title skills alone still can't turn
    # an unstated role into a confident match, only a stronger maybe.
    BARE_GENERIC_CAP = 65.0

    req_tokens = _tokenize_role(_clear_role_text(requirement_role))
    req_domain = req_tokens - GENERIC_ROLE_WORDS
    req_generic = req_tokens & GENERIC_ROLE_WORDS

    # (e) Requirement title is bare-generic (no domain word at all, e.g.
    # just "Developer" or "Consultant") — no real specialization stated to
    # compare a name against, genuinely unknown. EXCEPT: the whole title
    # might still be a known, real specialization spelled entirely with
    # words GENERIC_ROLE_WORDS treats as structural on their own (e.g.
    # "Platform Engineer") — check that before giving up.
    if not req_domain:
        if req_tokens and _known_generic_phrase_domain(req_tokens):
            # BUG FIX ("Platform Engineer" scored 42.5 — a real partial
            # match — against "Network Engineer", "QA Engineer", "Data
            # Engineer", and any other unrelated "*Engineer" title): this
            # used to fall through to the normal domain_overlap/ratio
            # flow with req_domain = set(req_tokens) — dumping the
            # GENERIC component of the phrase ("engineer") into the
            # domain-overlap pool alongside the genuinely domain-specific
            # word ("platform"). Any title merely sharing that one
            # generic word then registered as a real specialization
            # match, exactly the single-generic-word-inflation problem
            # GENERIC_ROLE_WORDS exists to prevent everywhere else. A
            # known compound phrase like "Platform Engineer" should only
            # be credited when the OTHER side recognizes the SAME
            # phrase (exact) or a genuinely adjacent one —
            # _adjacent_role_credit() already does exactly that
            # comparison on domain-only tokens, which is the mechanism
            # this branch's own docstring says it was meant to reach in
            # the first place. Score directly here instead of merging
            # into the generic ratio-based flow below.
            if req_tokens.issubset(pref_tokens) or _adjacent_role_credit(req_tokens, pref_tokens):
                return _title_skill_topup(85.0)
            # No real role signal for this known phrase — capped topup,
            # not a flat 0 and not a full match either.
            return _title_skill_topup(0.0, cap=NO_ROLE_SIGNAL_CAP)
        else:
            # No domain word stated at all (e.g. plain "Full Stack
            # Developer" once its skill clause is stripped) — genuinely
            # unknown, not a conflict. Skills can nudge the neutral
            # baseline up toward a soft maybe, capped below a confident
            # match.
            return _title_skill_topup(50.0, cap=BARE_GENERIC_CAP)

    domain_overlap = req_domain & pref_tokens
    generic_overlap = req_generic & pref_tokens

    # (c) Real specialization overlap — score normally, then let
    # title-embedded skills top up whatever gap remains.
    if domain_overlap:
        ratio = len(domain_overlap) / len(req_domain)
        generic_ratio = (len(generic_overlap) / len(req_generic)) if req_generic else 0.0
        score = ratio * 85 + (generic_ratio * 15 if req_generic else 0.0)
        # BUG FIX (test case #11, ETL Developer vs Extract Transform Load
        # Engineer): requiring len(domain_overlap) >= 2 alone let a
        # partial match (3 of 4 domain tokens, ratio=0.75, score=63.75)
        # get boosted to 78.75 — crossing the 70 NEAR_MISS gate even
        # though only 3/4 of the stated specialization actually matched.
        # The boost is meant to reward a near-COMPLETE domain match, not
        # just "2 or more tokens out of however many" — requiring
        # ratio >= 0.8 too keeps it from being a threshold-crossing
        # loophole for partial matches while still applying to every case
        # it was originally meant for (verified: of the 12 role-matching
        # spec test cases, this boost only ever fires for #11 either way).
        if len(domain_overlap) >= 2 and ratio >= 0.8 and score < 80:
            score = min(100.0, score + 15)
        score = round(min(score, 100.0), 2)
        return _title_skill_topup(score)

    # (d) A stated domain word exists but nothing overlaps at all — a real
    # specialization mismatch (Python Dev vs Java Dev). Role is mandatory:
    # a stated, different specialization is a real signal that title
    # skill-word overlap does NOT get to override. Exception (unchanged):
    # a hand-curated adjacent-role match earns 60% partial credit instead
    # of a hard 0 — that's a genuine partial role signal (not a skills
    # coincidence), so IT can still be topped up by title skills.
    if _adjacent_role_credit(req_tokens, pref_tokens):
        # 60% of the domain component only (no generic-word bonus) — the
        # adjacency substitutes for a direct domain-word match, it isn't a
        # coincidental extra generic-word overlap on top of one.
        return _title_skill_topup(round(85.0 * 0.6, 2))

    # No domain overlap at all and no adjacency credit — either the
    # consultant's role text stated no domain word at all (no signal) or
    # it stated a genuinely different one (a real conflict, e.g.
    # Salesforce vs AI/React). Either way there's no real role evidence,
    # so this stays capped low regardless of title-skill overlap — skills
    # can nudge it up from a flat 0, but role is mandatory: they can never
    # make this look like a confident match on their own.
    return _title_skill_topup(0.0, cap=NO_ROLE_SIGNAL_CAP)


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

    # BUG FIX (merged from a parallel fix on this same file): a consultant
    # with NO stated employment-type preference at all used to fail this
    # check outright (0.0) against every requirement that named a specific
    # type — treated as a hard mismatch rather than "unspecified". That's
    # inconsistent with how an unstated value is handled on the
    # requirement side just above (and everywhere else in the Stage 0-4
    # pipeline — see the N/A-wildcard handling in employment_type_passes()/
    # work_auth_passes()/experience_passes()/location_passes()). Treat it
    # the same way here: no preference stated = open to anything.
    if not consultant_types:
        return 100.0

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

    BUG FIX (soft score disagreed with the hard gate on the exact same
    consultant): location_passes() — the actual eligibility GATE this
    score feeds a ranking for — already treats a consultant with no
    preferred_locations stated as N/A and passes them, same "unspecified
    = don't penalize" wildcard rule documented on every other Stage 0-4
    filter and on score_employment_type()'s own matching fix above. This
    function never got that same treatment: it only ever awarded the 60
    location points when BOTH requirement.location AND
    consultant.preferred_locations were present, so a consultant who
    correctly passed the gate specifically BECAUSE they have no location
    constraint still lost up to 10 weighted points (location is 10% of
    the total in score_match()) on their ranking score for having
    "failed" a location match that was never actually evaluated against
    them. Now mirrors location_passes(): no stated consultant preference
    counts as an open match, same as the requirement-side REMOTE case
    above already does.
    """
    req_work_mode = (requirement.work_mode or "").upper()

    if req_work_mode == "REMOTE":
        return 100.0

    score = 0.0

    # Location match
    if requirement.location:
        if not consultant.preferred_locations:
            score += 60.0
        else:
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

    BUG FIX (two stacked issues): "if not consultant.work_authorization:
    return 0.0" ran FIRST, unconditionally — before even checking whether
    the requirement needed FULLTIME at all. A consultant with no stated
    work_authorization got zeroed on this factor against every C2C/
    contract posting too, even though this function's own docstring says
    those are "open to most work authorizations" and don't need to know
    citizenship status in the first place. And even in the genuine
    FULLTIME case, zeroing an unspecified consultant value outright
    disagreed with work_auth_passes() — the actual Stage 2 eligibility
    GATE this score feeds a ranking for — which already treats an N/A
    consultant work_authorization as passing (same wildcard rule as
    every other Stage 1-4 filter). A consultant who correctly passed the
    gate for exactly that reason still lost this factor's full weight in
    their ranking score. Now: no FULLTIME requirement means this factor
    doesn't apply at all (100.0, matching the docstring's own stated
    intent), and an unspecified consultant value gets the same
    unspecified-is-neutral treatment used everywhere else in this file —
    only a STATED value that's actually incompatible with a genuine
    FULLTIME requirement reduces the score.
    """
    req_types = set((requirement.employment_types or []))
    if "FULLTIME" not in req_types:
        return 100.0

    if not consultant.work_authorization:
        return 100.0

    auth = consultant.work_authorization.upper()

    # BUG FIX: consultant self-service My Profile now saves "USC" (not
    # "US_CITIZEN") — see phase3.py's validate_work_auth. Keeping
    # US_CITIZEN/GREEN_CARD here too so any consultant row saved under
    # the OLD dropdown before this change still passes correctly until
    # they resave. GC EAD (a pending green-card case's work permit, not
    # actual permanent residency) is deliberately NOT included — it's
    # not treated as equivalent to USC/GC for a direct full-time hire.
    if auth not in {"USC", "US_CITIZEN", "GC", "GREEN_CARD"}:
        return 0.0

    return 100.0


def employment_type_passes(
    requirement_types: Optional[List[str]], consultant_types: Optional[List[str]]
) -> tuple[bool, str]:
    """
    Stage 1 — Employment Type filter. requirement_types N/A/UNKNOWN matches
    everyone. Reuses score_employment_type()'s existing intersection logic
    unchanged, converted from a weighted score into a boolean pass/fail.
    NOTE: score_employment_type() has no partial-credit path today — it only
    ever returns 0.0 or 100.0 — so this conversion loses no information.
    """
    score = score_employment_type(requirement_types, consultant_types)
    return score > 0, f"employment_type score={score}"


# BUG FIX (work-authorization matching ignoring the real column and having
# no negation awareness): _requirement_work_auth_text() below used to
# ALWAYS re-derive the JD's work-authorization requirement by scanning
# requirement.job_description with a flat keyword search -- its own
# docstring claimed "The Requirement model has no explicit work-
# authorization column", which is no longer true now that models.py/
# dedup.py's save_requirement() store a real, precisely-extracted
# requirement.work_authorization column (see parser.py's
# extract_work_authorization()). Ignoring that in favor of a cruder
# re-scan of the raw JD text threw away real signal AND had no negation
# handling at all: the keyword search checks USC/GC-group terms before
# H1B/F1 with no regard for polarity, so a JD literally saying "H1B and
# OPT welcome — no US-citizen restriction" would still resolve to the
# most restrictive USC batch, purely because the words "US-citizen"
# appear in an EXCLUSION clause. These three helpers give
# _requirement_work_auth_text() below the same negation-aware per-batch
# scan parser.py's own extract_work_authorization() already uses for the
# same class of problem, applied to whichever text it's given (the
# structured field first, the raw JD as a fallback for older rows that
# predate the column being populated).
_WA_BATCH1_TOKENS = re.compile(r'(?i)\bstem\s*opt\b|\bopt\b|\bcpt\b|\bf-?1\b')
_WA_BATCH2_TOKENS = re.compile(r'(?i)\bh-?1-?b\b')
_WA_BATCH3_TOKENS = re.compile(
    r'(?i)\busc\b|\bu\.?s\.?\s*citizens?\b|\bcitizens?\s+only\b|\bgreen\s*card\b|'
    r'\bgc[\s\-]*ead\b|\bgc\b|\bl-?1\b|\bl-?2\b|\btn\s*visa\b|\btn\b|\bu\s*visa\b'
)
_WA_NEGATION_CUE = re.compile(
    r'(?i)\b(?:no|not|except|excluding|won\'?t\s+accept|cannot\s+accept)\b(?:\s+[\w/\-]+){0,3}?\s*$'
)


def _batch_mentioned_positively(text: str, pattern: re.Pattern) -> bool:
    """True if `pattern` matches somewhere in `text` in a NON-negated
    context — i.e. actually required/accepted, not explicitly excluded
    ("No H1B", "not accepting OPT"). Checks a short window immediately
    before each match for a negation cue, same approach and window size
    as parser.py's own extract_work_authorization()."""
    for m in pattern.finditer(text):
        window_before = text[max(0, m.start() - 30):m.start()]
        if not _WA_NEGATION_CUE.search(window_before):
            return True
    return False


def _requirement_work_auth_text(requirement: Requirement) -> Optional[str]:
    """
    Returns a representative batch label ("F1"/"H1B"/"USC") derived from
    the requirement's work-authorization requirement, or None if it
    doesn't state one at all (N/A — passes everyone).

    Prefers requirement.work_authorization — the field parser.py already
    extracts precisely (see extract_work_authorization()) — scanned with
    negation awareness so an explicitly-excluded status never wins over
    an explicitly-accepted one. Falls back to a negation-aware scan of
    the raw job_description only when that column is missing/blank
    (older rows saved before it was populated, or rows where extraction
    genuinely found nothing) — never silently prefers the cruder source
    when the precise one is available.
    """
    structured = (requirement.work_authorization or "").strip()
    if structured and structured.upper() != "N/A":
        if _batch_mentioned_positively(structured, _WA_BATCH3_TOKENS):
            return "USC"
        if _batch_mentioned_positively(structured, _WA_BATCH2_TOKENS):
            return "H1B"
        if _batch_mentioned_positively(structured, _WA_BATCH1_TOKENS):
            return "F1"
        # Every mention in the structured field was negated (e.g. "No
        # H1B, No OPT" with nothing stated as actually required) —
        # nothing is positively restricted, so this is N/A, not a batch.
        return None

    # No structured field to work with — fall back to the same
    # negation-aware scan applied to the raw JD text instead. Bare "TN"
    # (e.g. "Must have TN status") is included in _WA_BATCH3_TOKENS
    # directly, matching get_batch()'s treatment of it on the consultant
    # side, unlike this fallback's predecessor which only recognized the
    # rarer two-word "tn visa" phrase.
    jd = (requirement.job_description or "").lower()
    if _batch_mentioned_positively(jd, _WA_BATCH3_TOKENS):
        return "USC"
    if _batch_mentioned_positively(jd, _WA_BATCH2_TOKENS):
        return "H1B"
    if _batch_mentioned_positively(jd, _WA_BATCH1_TOKENS):
        return "F1"
    return None



def experience_passes(
    requirement: Requirement, consultant: Consultant, experiences: List[ConsultantExperience]
) -> tuple[bool, str]:
    """
    Stage 3 — Experience filter. N/A on EITHER side matches everyone —
    same wildcard rule as every other Stage 1-4 filter. Otherwise the
    consultant must be within -2 years of the stated minimum (inclusive
    at the floor); no upper cap — an over-qualified consultant always
    passes.
    """
    required_years = _parse_min_years_required(requirement)
    if required_years is None or required_years <= 0:
        return True, "requirement experience is N/A — passes all"

    # Consultant-side N/A: truly no data on file (not a stated 0, which is
    # a real value and still gets checked against the floor normally).
    if consultant.total_experience_years is None and not experiences:
        return True, "consultant experience is N/A — matches requirement"

    years = float(consultant.total_experience_years or 0)
    if years <= 0 and experiences:
        years = _calculate_total_experience_years(experiences)

    lower_bound = max(0, required_years - 2)
    if years < lower_bound:
        return False, f"consultant has {years}y, needs >= {lower_bound}y (required {required_years}y - 2)"
    return True, f"consultant has {years}y, meets >= {lower_bound}y floor"


def location_passes(
    requirement: Requirement, consultant: Consultant, experiences: List[ConsultantExperience]
) -> tuple[bool, str]:
    """
    Stage 4 — Location filter. N/A on EITHER side matches everyone — same
    wildcard rule as every other Stage 1-4 filter. Otherwise reuses
    score_location()'s existing remote/onsite/hybrid compatibility rules
    unchanged, converted from a weighted score into a boolean pass/fail.
    """
    if not requirement.location or requirement.location.strip().upper() == "N/A":
        return True, "requirement location is N/A — passes all"
    if not consultant.preferred_locations or consultant.preferred_locations.strip().upper() == "N/A":
        return True, "consultant location constraint is N/A — matches requirement"
    score = score_location(requirement, consultant, experiences)
    return score > 0, f"location score={score}"


def validate_match(
    requirement: Requirement,
    consultant: Consultant,
    experiences: List[ConsultantExperience],
    *,
    requirement_skills: Optional[List[str]] = None,
) -> dict:
    """
    Stage 0-4 eligibility pipeline.

    Stage 0 (role/responsibilities) is the primary gate, via score_role()'s
    domain-word decision tree — this is the single source of truth for
    role matching; nothing else in this function duplicates that logic.
    Stages 1-4 are sequential hard pass/fail filters that only run once
    Stage 0 clears at all, short-circuiting at the first failure (no need
    to evaluate later stages once one fails). Any requirement field that's
    N/A/empty at a given stage matches every consultant for that field —
    see each stage helper above for its own N/A handling.

    PERFORMANCE (merged from a parallel fix on this same file):
    requirement_skills is a pure function of the requirement alone —
    identical for every consultant scored against it. Bulk callers
    (match_requirement()'s per-consultant loop below) now compute it ONCE
    via _requirement_skills() and pass it in here, instead of every single
    consultant in the loop re-running the same parsed_fields/JD scan on
    the exact same requirement. This is what caused a real timeout on a
    dataset with 37,000+ open requirements. Still defaults to None and
    gets computed internally when not supplied, so any other caller (or a
    one-off call from outside a loop) keeps working unchanged.

    Returns:
      {
        "eligible": bool,            # False only for a REJECTED tier
        "tier": "REJECTED" | "NEAR_MISS_CANDIDATE" | "PASS",
        "stage_failed": str | None,  # "role" / "employment_type" /
                                      # "work_authorization" / "experience" /
                                      # "location", or None if eligible
        "role_raw": float,
        "reason": str,               # human-readable, for the audit log
      }

    "NEAR_MISS_CANDIDATE" means Stage 0 was a soft (10-70%) role match, not
    a hard reject and not a confident pass either — callers should still
    run score_match() and only actually tag the result NEAR_MISS if the
    FINAL blended score also lands below MATCH_THRESHOLD; if other factors
    compensate for the imperfect role match, it's a genuine PASS instead.
    """
    if requirement_skills is None:
        requirement_skills = _requirement_skills(requirement)
    consultant_skills = _consultant_skills(consultant)

    role_raw = score_role(
        requirement.role, consultant.preferred_roles, experiences, requirement_skills, consultant_skills
    )

    if role_raw < 10.0:
        return {
            "eligible": False, "tier": "REJECTED", "stage_failed": "role",
            "role_raw": role_raw, "reason": f"role score {role_raw} < 10 (hard floor)",
        }

    tier = "PASS" if role_raw >= 70.0 else "NEAR_MISS_CANDIDATE"

    # Stage 1 — Employment Type
    passed, reason = employment_type_passes(requirement.employment_types, consultant.preferred_employment_types)
    if not passed:
        return {"eligible": False, "tier": "REJECTED", "stage_failed": "employment_type", "role_raw": role_raw, "reason": reason}

    # Stage 2 — Work Authorization (batched push rule)
    req_work_auth = _requirement_work_auth_text(requirement)
    passed, reason = work_auth_passes(req_work_auth, consultant.work_authorization)
    if not passed:
        return {"eligible": False, "tier": "REJECTED", "stage_failed": "work_authorization", "role_raw": role_raw, "reason": reason}

    # Stage 3 — Experience (-2 years floor)
    passed, reason = experience_passes(requirement, consultant, experiences)
    if not passed:
        return {"eligible": False, "tier": "REJECTED", "stage_failed": "experience", "role_raw": role_raw, "reason": reason}

    # Stage 4 — Location
    passed, reason = location_passes(requirement, consultant, experiences)
    if not passed:
        return {"eligible": False, "tier": "REJECTED", "stage_failed": "location", "role_raw": role_raw, "reason": reason}

    return {"eligible": True, "tier": tier, "stage_failed": None, "role_raw": role_raw, "reason": "passed all stages"}


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

    CHANGE (not a bug fix — explicit instruction, scoped to this ranking
    function ONLY): skill's weight moved from 0.20 -> 0.0, so it no longer
    moves the total score; that 20% shifted onto role (0.50 -> 0.70).
    skill_raw/matched_skills/missing_skills are still computed and
    returned unchanged — kept for informational display, and because
    downstream consumers still read those dict keys / DB columns
    (RequirementConsultantMatch.skill_score in this file's own
    match_requirement()/match_consultant(), and matching_router.py's
    breakdown["skill"]["weighted"]).
    New weights: role 70%, experience 10%, location 10%, employment 5%,
    auth 5%. validate_match()'s Stage 0-4 eligibility gate (the hard
    role_raw < 10.0 floor, the 70.0 tier threshold, employment/work-auth/
    experience/location pass-fail checks) is UNCHANGED — this only
    affects the ranking score of consultants who already passed that gate.

    PERFORMANCE: requirement_skills is IDENTICAL for every consultant scored
    against the same requirement — see the matching note on validate_match()
    above. Bulk callers compute it once and pass it in; any other caller
    still gets it computed automatically when omitted.
    """
    # Prioritize tightly scoped skills extracted by parser.py (if any) —
    # shared with validate_match() via _requirement_skills() so this
    # extraction logic exists in exactly one place.
    if requirement_skills is None:
        requirement_skills = _requirement_skills(requirement)
    consultant_skills = _consultant_skills(consultant)

    skill_raw, matched_skills, missing_skills = score_skills(requirement_skills, consultant_skills)
    role_raw = score_role(
        requirement.role, consultant.preferred_roles, experiences, requirement_skills, consultant_skills
    )
    exp_raw = score_experience(requirement, consultant, experiences)
    employment_raw = score_employment_type(requirement.employment_types, consultant.preferred_employment_types)
    location_raw = score_location(requirement, consultant, experiences)
    auth_raw = score_work_auth(requirement, consultant)

    skill_score = skill_raw * 0.0
    role_score = role_raw * 0.70
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
            "skill": {"raw": round(skill_raw, 2), "weight": 0.0, "weighted": round(skill_score, 2)},
            "role": {"raw": round(role_raw, 2), "weight": 0.70, "weighted": round(role_score, 2)},
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
    Score all active consultants against one requirement. Single engine —
    this file's own validate_match()/score_match() (unchanged) are called
    directly; there is no second table (JobMatch, deleted) and no
    delegation to a second pipeline anymore.

    Every consultant runs through validate_match()'s Stage 0-4 gate.
    Ineligible consultants get status=NOT_ELIGIBLE (row kept for audit,
    never deleted). Eligible consultants get status=MATCHING regardless
    of role-match tier — NEAR_MISS is no longer a status, just an
    informational `tier` field on the row.

    Returns the current count of MATCHING rows for this requirement.
    """
    req_result = await db.execute(select(Requirement).where(Requirement.id == requirement_id))
    requirement = req_result.scalars().first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    from sqlalchemy.exc import IntegrityError

    consultants_result = await db.execute(
        select(Consultant)
        .join(User, Consultant.user_id == User.id)
        .where(
            Consultant.status == "ACTIVE",
            User.role == "CONSULTANT",
            User.is_authorized == True,
        )
    )
    roster = consultants_result.scalars().all()
    roster_ids = {c.id for c in roster}

    if not roster:
        logger.info("No active consultants found — skipping match for requirement_id=%s", requirement_id)
        return 0

    exp_result = await db.execute(
        select(ConsultantExperience).where(ConsultantExperience.consultant_id.in_(list(roster_ids)))
    )
    experiences_by_consultant: dict[int, list[ConsultantExperience]] = {}
    for exp in exp_result.scalars().all():
        experiences_by_consultant.setdefault(exp.consultant_id, []).append(exp)

    existing_result = await db.execute(
        select(RequirementConsultantMatch).where(
            RequirementConsultantMatch.requirement_id == requirement_id
        )
    )
    existing_by_consultant = {m.consultant_id: m for m in existing_result.scalars().all()}

    requirement_skills = _requirement_skills(requirement)

    needs_scoring = []
    for consultant in roster:
        existing = existing_by_consultant.get(consultant.id)
        if existing is None:
            needs_scoring.append(consultant)
            continue
        if existing.status in ("APPLIED", "REJECTED"):
            continue  # frozen — human decision, engine never touches it again
        if existing.status == "MATCHING" and existing.score_breakdown and \
                existing.score_breakdown.get("_version") == MATCHING_LOGIC_VERSION:
            continue  # already current, nothing to do
        needs_scoring.append(consultant)  # stale version, or was NOT_ELIGIBLE

    for consultant in needs_scoring:
        experiences = experiences_by_consultant.get(consultant.id, [])

        validation = validate_match(
            requirement, consultant, experiences,
            requirement_skills=requirement_skills,
        )

        existing = existing_by_consultant.get(consultant.id)

        if not validation["eligible"]:
            if existing is not None:
                existing.status = "NOT_ELIGIBLE"
                existing.match_reason = (
                    f"No longer eligible — failed at stage '{validation['stage_failed']}': {validation['reason']}"
                )
            continue

        result = score_match(
            requirement, consultant, experiences,
            requirement_skills=requirement_skills,
        )
        result["score_breakdown"]["_version"] = MATCHING_LOGIC_VERSION
        tier_value = "NEAR_MISS" if validation["tier"] == "NEAR_MISS_CANDIDATE" else "STRONG"

        try:
            async with db.begin_nested():
                if existing is not None:
                    existing.status = "MATCHING"
                    existing.tier = tier_value
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
                    new_match = RequirementConsultantMatch(
                        requirement_id=requirement_id,
                        consultant_id=consultant.id,
                        status="MATCHING",
                        tier=tier_value,
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
                    )
                    db.add(new_match)
                    existing_by_consultant[consultant.id] = new_match
                await db.flush()
        except IntegrityError:
            stmt = select(RequirementConsultantMatch).where(
                RequirementConsultantMatch.requirement_id == requirement_id,
                RequirementConsultantMatch.consultant_id == consultant.id,
            )
            res = await db.execute(stmt)
            row = res.scalars().first()
            if row:
                row.status = "MATCHING"
                row.tier = tier_value
                row.match_score = result["total"]
                row.skill_score = result["skill_score"]
                row.role_score = result["role_score"]
                row.experience_score = result["experience_score"]
                row.employment_score = result["employment_score"]
                row.location_score = result["location_score"]
                row.auth_score = result["auth_score"]
                row.matched_skills = result["matched_skills"]
                row.missing_skills = result["missing_skills"]
                row.match_reason = result["match_reason"]
                row.score_breakdown = result["score_breakdown"]
                existing_by_consultant[consultant.id] = row
                await db.flush()

    # Sweep: any existing MATCHING row whose consultant fell out of the
    # active roster since last run.
    for consultant_id_key, existing in existing_by_consultant.items():
        if consultant_id_key not in roster_ids and existing.status == "MATCHING":
            existing.status = "NOT_ELIGIBLE"
            existing.match_reason = "consultant no longer active/authorized"

    match_count = sum(1 for m in existing_by_consultant.values() if m.status == "MATCHING")
    requirement.ats_match_count = match_count

    await db.commit()
    logger.info(
        "Matched requirement_id=%s — %d consultants scored, %d MATCHING",
        requirement_id, len(needs_scoring), match_count,
    )
    return match_count


async def match_consultant(db: AsyncSession, consultant_id: int) -> int:
    """
    Inverse of match_requirement: score ONE consultant against all
    still-open requirements. Same single-engine logic as
    match_requirement() above — this file's own validate_match()/
    score_match() are called directly, no second table, no delegation.

    PERFORMANCE (kept from the original fix): this scales with OPEN
    REQUIREMENT count (44,000+ in production), unlike match_requirement()
    which scales with active-consultant count. A first lightweight JOIN
    fetches only (id, status, score_breakdown) — no large columns, no
    giant IN-list — to cheaply decide which requirements actually need
    (re)scoring; full Requirement objects are hydrated ONLY for that
    smaller subset.

    Returns the number of requirements where this consultant now has
    status=MATCHING.
    """
    cons_result = await db.execute(select(Consultant).where(Consultant.id == consultant_id))
    consultant = cons_result.scalars().first()
    if not consultant or consultant.status != "ACTIVE":
        return 0

    exp_result = await db.execute(
        select(ConsultantExperience).where(ConsultantExperience.consultant_id == consultant_id)
    )
    experiences = exp_result.scalars().all()

    # Lightweight pass: which open requirements actually need scoring?
    # LEFT JOIN so a requirement with no existing match row for this
    # consultant still comes back (status/score_breakdown as NULL/None).
    lightweight_result = await db.execute(
        select(
            Requirement.id,
            RequirementConsultantMatch.status,
            RequirementConsultantMatch.score_breakdown,
        )
        .select_from(Requirement)
        .outerjoin(
            RequirementConsultantMatch,
            (RequirementConsultantMatch.requirement_id == Requirement.id)
            & (RequirementConsultantMatch.consultant_id == consultant_id),
        )
        .where(Requirement.status.notin_(Requirement.TERMINAL_STATUSES))
    )
    lightweight_rows = lightweight_result.all()
    if not lightweight_rows:
        await db.commit()
        return 0

    match_count = 0
    ids_needing_scoring: list[int] = []

    for req_id, existing_status, existing_score_breakdown in lightweight_rows:
        if existing_status in ("APPLIED", "REJECTED"):
            continue  # frozen — human decision, engine never touches it again
        if existing_status == "MATCHING" and existing_score_breakdown and \
                existing_score_breakdown.get("_version") == MATCHING_LOGIC_VERSION:
            match_count += 1
            continue
        ids_needing_scoring.append(req_id)

    if not ids_needing_scoring:
        await db.commit()
        logger.info(
            "Auto-matched consultant_id=%s — all %d open requirements already up to date, nothing to rescore",
            consultant_id, len(lightweight_rows),
        )
        return match_count

    from sqlalchemy.exc import IntegrityError

    # Full Requirement objects ONLY for the (typically much smaller)
    # subset that genuinely needs scoring.
    # CHUNKED — ids_needing_scoring can hold every open requirement in
    # the worst case (right after a MATCHING_LOGIC_VERSION bump); a
    # single .in_() at that scale can exceed Postgres's bind-parameter
    # limit.
    requirements: list[Requirement] = []
    for _chunk in _chunk_ids(ids_needing_scoring):
        _chunk_result = await db.execute(select(Requirement).where(Requirement.id.in_(_chunk)))
        requirements.extend(_chunk_result.scalars().all())

    existing_by_req: dict[int, RequirementConsultantMatch] = {}
    for _chunk in _chunk_ids(ids_needing_scoring):
        _chunk_result = await db.execute(
            select(RequirementConsultantMatch).where(
                RequirementConsultantMatch.consultant_id == consultant_id,
                RequirementConsultantMatch.requirement_id.in_(_chunk),
            )
        )
        existing_by_req.update({m.requirement_id: m for m in _chunk_result.scalars().all()})

    for i, requirement in enumerate(requirements):
        # PERFORMANCE: validate_match()/score_match() are synchronous
        # CPU-bound Python with no await points — yield briefly every 50
        # items so this doesn't freeze the whole event loop (including
        # the HTTP response for the profile save that triggered this).
        if i % 50 == 0:
            await asyncio.sleep(0)

        existing = existing_by_req.get(requirement.id)
        requirement_skills = _requirement_skills(requirement)

        validation = validate_match(
            requirement, consultant, experiences,
            requirement_skills=requirement_skills,
        )

        if not validation["eligible"]:
            if existing is not None and existing.status != "NOT_ELIGIBLE":
                existing.status = "NOT_ELIGIBLE"
                existing.match_reason = (
                    f"No longer eligible — failed at stage '{validation['stage_failed']}': {validation['reason']}"
                )
                await db.flush()
            continue

        result = score_match(
            requirement, consultant, experiences,
            requirement_skills=requirement_skills,
        )
        result["score_breakdown"]["_version"] = MATCHING_LOGIC_VERSION
        tier_value = "NEAR_MISS" if validation["tier"] == "NEAR_MISS_CANDIDATE" else "STRONG"

        try:
            async with db.begin_nested():
                if existing:
                    existing.status = "MATCHING"
                    existing.tier = tier_value
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
                        status="MATCHING",
                        tier=tier_value,
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
                    ))
                await db.flush()
        except IntegrityError:
            stmt = select(RequirementConsultantMatch).where(
                RequirementConsultantMatch.requirement_id == requirement.id,
                RequirementConsultantMatch.consultant_id == consultant_id,
            )
            res = await db.execute(stmt)
            row = res.scalars().first()
            if row:
                row.status = "MATCHING"
                row.tier = tier_value
                row.match_score = result["total"]
                row.skill_score = result["skill_score"]
                row.role_score = result["role_score"]
                row.experience_score = result["experience_score"]
                row.employment_score = result["employment_score"]
                row.location_score = result["location_score"]
                row.auth_score = result["auth_score"]
                row.matched_skills = result["matched_skills"]
                row.missing_skills = result["missing_skills"]
                row.match_reason = result["match_reason"]
                row.score_breakdown = result["score_breakdown"]
                await db.flush()

        match_count += 1

    await db.commit()
    logger.info(
        "Auto-matched consultant_id=%s across %d open requirements — %d MATCHING",
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

    # BUG FIX: this had no status filter at all — unlike Pipeline B's own
    # bulk background run (matching_router.py's
    # _run_matching_engine_background, which filters
    # Requirement.status.notin_(["CLOSED", "REJECTED"])), every requirement
    # ever created — including long-closed and rejected ones — got fully
    # scored against every active consultant on each "Match All" click.
    # match_requirement() itself only filters Consultant.status ==
    # "ACTIVE"; nothing anywhere in this call chain excluded the
    # requirement's own status. That's pure wasted compute at the scale
    # this file's own comments describe (37,000+ requirements caused a
    # real timeout before), and could create brand new ASSIGNED/NEAR_MISS
    # rows for a posting that's no longer actually open. Matches Pipeline
    # B's exact filter so both "run everything" entry points agree on
    # what "everything" means.
    #
    # Now reads Requirement.TERMINAL_STATUSES (a single shared constant on
    # the model — see models.py) instead of its own hardcoded copy of the
    # same two strings — matching_router.py's two occurrences of this same
    # filter still use their own literal list; this is the first of the
    # three to move to the shared constant, opportunistically, not a
    # requirement for this fix to work correctly on its own.
    result = await db.execute(
        select(Requirement).where(Requirement.status.notin_(Requirement.TERMINAL_STATUSES))
    )
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