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
MATCHING_LOGIC_VERSION = "2026-08-22-data-generic-word-fix"

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


from disk_cache import PersistentDiskCache
import json
_ROLE_MATCH_CACHE = PersistentDiskCache("role_match_cache.json")

def score_role(
    requirement_role: Optional[str],
    consultant_preferred_roles: Optional[str],
    experiences: Optional[List[ConsultantExperience]] = None,
    requirement_skills: Optional[List[str]] = None,
    consultant_skills: Optional[List[str]] = None,
) -> float:
    """
    Role title match — domain-word (specialization) aware, name-based only.

    BUG FIX: the old version treated a single shared GENERIC word (e.g. both
    titles containing "Developer") as a near-100% match on its own, even
    when the actual specialization was unrelated ("Java Developer" vs
    "Python Developer") — since role is weighted 50% of the total score,
    this alone was often enough to clear the match threshold regardless of
    real skill fit. This version splits tokens into a domain (specialization)
    part and a generic part (GENERIC_ROLE_WORDS) and only a genuine
    domain-word overlap earns a high score (path c). A requirement that
    states a domain word with NO overlap at all is a real mismatch signal,
    not missing data, and scores 0 outright — with a small hand-curated
    ADJACENT_ROLES exception for genuinely related specializations (path d).

    BUG FIX #2: role matching must be based on the actual role NAME, never
    substituted by a skill-overlap coincidence — skills and role are two
    separate factors in score_match()'s blend for a reason. The previous
    version fell back to comparing skill lists whenever there was no title
    to compare (blank title, no consultant role data, or a bare-generic
    title with no domain word) — removed. Every one of those cases below
    (a/b/e) now returns a plain neutral 50 instead: genuinely unknown,
    neither a confident pass nor fail, decided purely on whether a real
    name comparison was even possible — never on skills.

    requirement_skills/consultant_skills are still accepted for signature
    compatibility with existing callers but are no longer used by this
    function — role scoring is name-only. Callers may stop passing them
    to score_role() specifically at any point without changing behavior;
    they're still needed elsewhere (score_skills()'s own factor).
    """
    # (a) No requirement role text at all — no name to compare against,
    # genuinely unknown either way.
    if not requirement_role or not requirement_role.strip():
        return 50.0

    # Attempt AI evaluation first
    raw_pref_roles = []
    if consultant_preferred_roles:
        raw_pref_roles.append(consultant_preferred_roles.strip())
    if experiences:
        for exp in experiences:
            if exp.role_title:
                raw_pref_roles.append(exp.role_title.strip())

    if raw_pref_roles:
        unique_roles = tuple(sorted(set(raw_pref_roles)))
        req_role_clean = requirement_role.strip()

        # BUG FIX: cache_key had no version component, so a prompt fix to
        # ROLE_MATCH_SYSTEM_PROMPT (e.g. the job-function-before-domain
        # gating fix) had zero effect on any (requirement_role,
        # consultant_roles) pair already scored under the old prompt —
        # it would just keep returning the stale wrong score from disk
        # forever, cache hit every time. JobMatch rows already
        # self-invalidate on MATCHING_LOGIC_VERSION changes; this gives
        # the AI role-match cache the same self-healing behavior.
        from claude_service import ROLE_MATCH_PROMPT_VERSION
        cache_key = json.dumps([ROLE_MATCH_PROMPT_VERSION, req_role_clean, list(unique_roles)])

        cached_score = _ROLE_MATCH_CACHE.get(cache_key)
        if cached_score is not None:
            return cached_score

        try:
            from claude_service import evaluate_role_match_with_ai
            ai_score = evaluate_role_match_with_ai(req_role_clean, list(unique_roles))
            if ai_score is not None:
                _ROLE_MATCH_CACHE.set(cache_key, ai_score)
                return ai_score
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"AI role match failed: {e}")

    # Build the consultant's role-token pool (preferred_roles + every
    # experience row's role_title), same sources as before.
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

    req_tokens = _tokenize_role(requirement_role)
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
                return 85.0
            return 0.0
        else:
            return 50.0

    domain_overlap = req_domain & pref_tokens
    generic_overlap = req_generic & pref_tokens

    # (c) Real specialization overlap — score normally.
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
        return round(min(score, 100.0), 2)

    # (d) A stated domain word exists but nothing overlaps at all — a real
    # specialization mismatch (Python Dev vs Java Dev). Do NOT fall back to
    # skills here; a stated, different specialization is a real signal.
    # Exception: a hand-curated adjacent-role match earns 60% partial credit
    # instead of a hard 0.
    if _adjacent_role_credit(req_tokens, pref_tokens):
        # 60% of the domain component only (no generic-word bonus) — the
        # adjacency substitutes for a direct domain-word match, it isn't a
        # coincidental extra generic-word overlap on top of one.
        return round(85.0 * 0.6, 2)

    return 0.0


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


def _requirement_work_auth_text(requirement: Requirement) -> Optional[str]:
    """
    The Requirement model has no explicit work-authorization column — the
    JD's implied requirement is derived by scanning its text for keywords,
    same regex patterns validate_match() already used inline. Returns a
    representative batch label ("F1"/"H1B"/"USC") or None if the JD doesn't
    mention work authorization at all (N/A — passes everyone).
    """
    jd = (requirement.job_description or "").lower()
    # BUG FIX (merged from a parallel fix on this same file): bare "TN"
    # (e.g. "Must have TN status") — the far more common way recruiters
    # actually write it — was never detected, only the two-word phrase
    # "tn visa" was, which real JDs rarely use. That silently misclassified
    # these requirements as work-auth N/A (passes everyone) instead of
    # correctly restricting to Batch 3. get_batch() already treats bare
    # "TN" as Batch 3 on the consultant side — this brings the requirement
    # side in line with that.
    if re.search(r'\b(usc|gc|green card|us citizen|citizens only|citizen|gc ead|tn|tn visa|l1|u visa)\b', jd):
        return "USC"
    if re.search(r'\b(h1b|h1-b)\b', jd):
        return "H1B"
    if re.search(r'\b(f1|opt|cpt|stem opt)\b', jd):
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
    Score all active consultants against one requirement.

    Every consultant runs through validate_match()'s Stage 0-4 eligibility
    pipeline first (role/responsibilities -> employment type -> work auth ->
    experience -> location, each short-circuiting). Only consultants who
    pass ALL stages get a RequirementConsultantMatch row at all — MATCH_THRESHOLD
    is no longer a row-creation gate; score_match()'s weighted total is now
    purely a RANKING signal among already-eligible consultants (it still
    decides ASSIGNED vs NEAR_MISS for the narrow role-score band, see below).
    Rerunning does not duplicate — UNIQUE constraint on (requirement_id, consultant_id)
    combined with explicit existence check ensures idempotency. A consultant
    who used to qualify but no longer does on this rerun has their stale row
    DELETED rather than left untouched (previously: stale rows were never
    cleaned up, so a disqualified consultant kept passing the "is this
    requirement assigned to them" existence check used elsewhere).
    Returns count of ASSIGNED (non-NEAR_MISS) matches created or updated.

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
    near_miss_count = 0

    # PERFORMANCE (merged from a parallel fix on this same file): compute
    # once per requirement, reuse for every consultant in the loop below —
    # requirement_skills is a pure function of the requirement alone, so
    # recomputing it per-consultant (as before) was pure waste that got
    # worse the more consultants there were. See the note on
    # validate_match()/score_match() above.
    requirement_skills = _requirement_skills(requirement)

    # ── Scoring loop — pure in-memory computation, zero DB round trips per iteration ──
    for consultant in consultants:
        experiences = experiences_by_consultant.get(consultant.id, [])
        existing = existing_matches_by_consultant.get(consultant.id)

        # BUG FIX: this had no guard at all before validating/deleting an
        # existing row — a match already advanced to RESUME_GENERATED,
        # READY_TO_APPLY, APPLIED, or REJECTED could be silently DELETED
        # outright if a rescore later decided it no longer qualifies
        # (e.g. after a scoring-logic change). Same protective check
        # already applied in matching_router.py's Pipeline B — only rows
        # still owned by the matching engine itself (ASSIGNED, NEAR_MISS,
        # or a previously INVALIDATED one) are ever re-evaluated.
        if existing and existing.status in ("RESUME_GENERATED", "READY_TO_APPLY", "APPLIED", "REJECTED"):
            if existing.status == "ASSIGNED":
                assignment_count += 1
            continue

        # PERFORMANCE (Run Engine/Match All timing out at 300s): skip the
        # full validate_match()+score_match() recomputation for a row
        # already checked under the CURRENT matching logic — see
        # MATCHING_LOGIC_VERSION above. Only a row from before a logic
        # change (untagged, or tagged with an older version) pays the
        # full re-check cost; everything already up to date stays fast,
        # restoring the old existing_pairs-skip performance for the
        # common case where nothing has actually changed since last run.
        if existing and existing.score_breakdown and existing.score_breakdown.get("_version") == MATCHING_LOGIC_VERSION:
            if existing.status == "NEAR_MISS":
                near_miss_count += 1
            elif existing.status == "ASSIGNED":
                assignment_count += 1
            continue

        validation = validate_match(requirement, consultant, experiences, requirement_skills=requirement_skills)

        if not validation["eligible"]:
            logger.info(
                "match_requirement: requirement_id=%s consultant_id=%s REJECTED at stage=%s (%s)",
                requirement_id, consultant.id, validation["stage_failed"], validation["reason"],
            )
            # BUG FIX: match history is mandatory — never delete a row,
            # mark it INVALIDATED instead so it drops out of the counted
            # ASSIGNED/NEAR_MISS totals (and out of the admin Requirements
            # page's ats_match_count) while the row and its original
            # reasoning stay in the table. Same fix already applied to
            # Pipeline B (matching_router.py).
            if existing and existing.status != "INVALIDATED":
                existing.status = "INVALIDATED"
                existing.match_reason = (
                    f"No longer eligible — failed at stage '{validation['stage_failed']}': {validation['reason']}"
                )
                await db.flush()
            continue

        result = score_match(requirement, consultant, experiences, requirement_skills=requirement_skills)
        result["score_breakdown"]["_version"] = MATCHING_LOGIC_VERSION

        # NEAR_MISS_CANDIDATE (soft 10-70% role match) only actually becomes
        # a NEAR_MISS row if the final blended score ALSO misses threshold —
        # if other factors compensated for the imperfect role match, it's a
        # legitimate normal pass instead. A role tier of PASS (>=70%) is
        # always a normal pass regardless of the final total, same as the
        # role-matching-fix spec states.
        if validation["tier"] == "NEAR_MISS_CANDIDATE" and result["total"] < MATCH_THRESHOLD:
            new_status = "NEAR_MISS"
        else:
            new_status = "ASSIGNED"

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
                    # Never clobber a workflow status an admin/recruiter has
                    # already advanced (RESUME_GENERATED, READY_TO_APPLY,
                    # APPLIED, REJECTED) — only move between the two
                    # matching-engine-owned statuses themselves.
                    if existing.status in ("ASSIGNED", "NEAR_MISS", "INVALIDATED"):
                        existing.status = new_status
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
                        status=new_status,
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
                if existing.status in ("ASSIGNED", "NEAR_MISS", "INVALIDATED"):
                    existing.status = new_status
                await db.flush()

        if new_status == "NEAR_MISS":
            near_miss_count += 1
        else:
            assignment_count += 1

    # BUG FIX: match_requirement() upserted rows into
    # requirement_consultant_matches correctly, but never wrote back to
    # requirements.ats_match_count — the column the admin Requirements
    # table actually displays. Matching genuinely worked; the visible
    # count just never reflected it (stuck at whatever seed.py's random
    # demo value or the column default of 0 was). assignment_count here
    # counts only normal ASSIGNED matches (not NEAR_MISS) — NEAR_MISS rows
    # are meant for a separate view/tab per the role-matching-fix spec, so
    # they're deliberately kept out of the headline admin count.
    requirement.ats_match_count = assignment_count

    await db.commit()
    logger.info(
        "Matched requirement_id=%s — %d consultants scored, %d ASSIGNED, %d NEAR_MISS (3 total queries)",
        requirement_id, len(consultants), assignment_count, near_miss_count,
    )
    return assignment_count


async def match_consultant(db: AsyncSession, consultant_id: int) -> int:
    """
    Inverse of match_requirement: score ONE consultant against all
    still-open requirements and upsert into requirement_consultant_matches.
    Called automatically when a consultant updates their profile so their
    matches reflect the new skills/roles/etc. without an admin re-run.
    Returns the number of requirements where they now meet MATCH_THRESHOLD.

    PERFORMANCE FIX (root cause of "saving one field on My Profile takes
    8+ seconds / freezes the whole app"): match_requirement() above scales
    with ACTIVE CONSULTANT count (typically small), but this function
    scales with OPEN REQUIREMENT count — 44,000+ in production. The old
    version pulled every open Requirement as a FULL ORM object (every
    column, including large JSON/text fields) and looked up existing
    matches via `.in_(req_ids)` with all 44,000+ ids as literal SQL
    parameters — on every single save, even though the very next check
    (the MATCHING_LOGIC_VERSION tag) was about to skip almost all of them
    anyway.

    Fix: a first lightweight JOIN fetches only (id, status,
    score_breakdown) — no large columns, no giant IN-list — to cheaply
    decide which requirements actually need (re)scoring, still reading
    the version tag out of score_breakdown's JSON exactly as before (no
    schema change). Full Requirement objects are then hydrated ONLY for
    that smaller subset. Skip/count semantics are unchanged — same
    protected-status guard, same version-tag skip, same counting — this
    only changes what gets fetched, and how much of it.

    A second, separate fix below (the periodic `await asyncio.sleep(0)`)
    addresses a related but distinct problem: validate_match()/
    score_match() are synchronous CPU-bound Python with few or no
    `await` points inside a long run of rejections. Since Python's
    asyncio event loop is single-threaded, a long uninterrupted stretch
    of that work blocks EVERYTHING else on the process — including
    sending back the HTTP response for the save that triggered this
    background task — not just this task itself.
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
    # consultant still comes back (status/score_breakdown as NULL/None),
    # matching the original "existing = None" case exactly.
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
        .where(Requirement.status.notin_(["CLOSED", "REJECTED"]))
    )
    lightweight_rows = lightweight_result.all()
    if not lightweight_rows:
        await db.commit()
        return 0

    match_count = 0
    near_miss_count = 0
    ids_needing_scoring: list[int] = []

    for req_id, existing_status, existing_score_breakdown in lightweight_rows:
        # BUG FIX: same protective guard as match_requirement() above —
        # never touch a row already advanced to RESUME_GENERATED,
        # READY_TO_APPLY, APPLIED, or REJECTED. (Identical to the
        # original per-row check — just evaluated here, before deciding
        # whether a full Requirement object is even needed.)
        if existing_status in ("RESUME_GENERATED", "READY_TO_APPLY", "APPLIED", "REJECTED"):
            if existing_status == "ASSIGNED":
                match_count += 1
            continue

        # PERFORMANCE: same version-tag skip as match_requirement() above —
        # skip full re-validation for a row already checked under the
        # current matching logic.
        if existing_score_breakdown and existing_score_breakdown.get("_version") == MATCHING_LOGIC_VERSION:
            if existing_status == "NEAR_MISS":
                near_miss_count += 1
            elif existing_status == "ASSIGNED":
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

    # Full Requirement objects ONLY for the (typically much smaller)
    # subset that genuinely needs scoring — this is the expensive fetch,
    # now scoped to a delta instead of every open requirement.
    reqs_result = await db.execute(select(Requirement).where(Requirement.id.in_(ids_needing_scoring)))
    requirements = reqs_result.scalars().all()

    existing_result = await db.execute(
        select(RequirementConsultantMatch).where(
            RequirementConsultantMatch.consultant_id == consultant_id,
            RequirementConsultantMatch.requirement_id.in_(ids_needing_scoring),
        )
    )
    existing_by_req = {m.requirement_id: m for m in existing_result.scalars().all()}

    for i, requirement in enumerate(requirements):
        # PERFORMANCE FIX (root cause of "the whole app stalls during a
        # save"): validate_match()/score_match() are plain synchronous
        # CPU-bound Python — no `await` inside them — and a rejected
        # requirement with no existing row to touch skips db.flush()
        # entirely, so a long run of those has NO yield points at all.
        # Yielding briefly every 50 items costs nothing measurable (this
        # loop is now typically just the delta needing rescoring) but
        # lets the event loop interleave other pending work — like
        # finishing an HTTP response — instead of freezing everything
        # else for the loop's entire duration.
        if i % 50 == 0:
            await asyncio.sleep(0)

        existing = existing_by_req.get(requirement.id)

        validation = validate_match(requirement, consultant, experiences)

        if not validation["eligible"]:
            logger.info(
                "match_consultant: consultant_id=%s requirement_id=%s REJECTED at stage=%s (%s)",
                consultant_id, requirement.id, validation["stage_failed"], validation["reason"],
            )
            if existing and existing.status != "INVALIDATED":
                existing.status = "INVALIDATED"
                existing.match_reason = (
                    f"No longer eligible — failed at stage '{validation['stage_failed']}': {validation['reason']}"
                )
                await db.flush()
            continue

        result = score_match(requirement, consultant, experiences)
        result["score_breakdown"]["_version"] = MATCHING_LOGIC_VERSION

        if validation["tier"] == "NEAR_MISS_CANDIDATE" and result["total"] < MATCH_THRESHOLD:
            new_status = "NEAR_MISS"
        else:
            new_status = "ASSIGNED"

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
                    if existing.status in ("ASSIGNED", "NEAR_MISS", "INVALIDATED"):
                        existing.status = new_status
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
                        status=new_status,
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
                if existing.status in ("ASSIGNED", "NEAR_MISS", "INVALIDATED"):
                    existing.status = new_status
                await db.flush()

        if new_status == "NEAR_MISS":
            near_miss_count += 1
        else:
            match_count += 1

    await db.commit()
    logger.info(
        "Auto-matched consultant_id=%s across %d open requirements — %d ASSIGNED, %d NEAR_MISS",
        consultant_id, len(requirements), match_count, near_miss_count,
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
    result = await db.execute(
        select(Requirement).where(Requirement.status.notin_(["CLOSED", "REJECTED"]))
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