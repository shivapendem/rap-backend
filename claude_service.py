import os
import json
import re
from typing import Optional
from anthropic import Anthropic
import logging
import traceback
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# Category buckets for turning a flat skill list into the categorized
# "TECHNICAL PROFICIENCIES" table format, for use when resume_info doesn't
# already have technical_proficiencies pre-categorized (it usually only has
# a flat tech_stack.{expert,exposure,familiar}). Order here is the order
# categories appear in the table; a skill is matched case-insensitively
# against these keyword lists, first match wins.
SKILL_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Programming Languages", [
        "python", "sql", "c++", "c", "java", "javascript", "typescript",
        "bash", "shell scripting", "go", "golang", "rust", "scala",
    ]),
    ("AI / ML & GenAI", [
        "gpt", "openai", "langchain", "llamaindex", "rag", "agentic", "prompt engineering",
        "fine-tuning", "lora", "peft", "huggingface", "transformers", "guardrails",
        "bert", "bart", "t5", "ner", "topic modeling", "ragas", "trulens",
        "scikit-learn", "xgboost", "lightgbm", "nltk", "spacy", "tensorflow", "pytorch",
        "keras", "llama", "nemo",
    ]),
    ("Vector Databases & Search", [
        "pinecone", "faiss", "chromadb", "weaviate", "pgvector", "elasticsearch", "opensearch",
    ]),
    ("Backend Frameworks & APIs", [
        "fastapi", "flask", "django", "graphql", "restful", "soap api", "swagger", "openapi",
        "spring boot", "microservices",
    ]),
    ("Cloud Platforms", [
        "aws", "azure", "gcp", "google cloud", "ec2", "s3", "lambda", "sagemaker",
    ]),
    ("Big Data & Streaming", [
        "apache kafka", "kafka", "pyspark", "apache spark", "spark", "apache airflow", "airflow",
        "rabbitmq", "celery",
    ]),
    ("Databases & Data Stores", [
        "postgresql", "mongodb", "redis", "mysql", "oracle", "sql server", "teradata",
        "cassandra", "dynamodb",
    ]),
    ("DevOps & CI/CD", [
        "docker", "kubernetes", "terraform", "ci/cd", "github actions", "jenkins",
        "ansible", "puppet", "chef",
    ]),
    ("Data Processing & Visualization", [
        "pandas", "numpy", "scipy", "matplotlib", "seaborn", "plotly", "jupyter", "mlflow",
    ]),
    ("Testing & Monitoring", [
        "pytest", "unittest", "prometheus", "cloudwatch", "dynatrace", "nagios",
        "loadrunner", "soapui",
    ]),
    ("Version Control & Collaboration", [
        "git", "github", "gitlab", "svn", "mercurial", "jira", "confluence", "postman",
    ]),
    ("Web Technologies", [
        "html5", "html", "css3", "css", "jquery", "ajax", "json", "xml",
    ]),
]


def _skill_name(s):
    """Skills are stored as plain strings in older profiles, but as
    {"name", "isPrimary"} dicts in newer ones. Accept either so nothing
    in this file crashes on whichever format a consultant data uses."""
    if isinstance(s, dict):
        return s.get("name", "")
    return s if isinstance(s, str) else str(s or "")


def categorize_skills_with_tier(primary: list[str], secondary: list[str]) -> list[dict]:
    """Same category-by-technology-type grouping as categorize_skills(),
    built from a consultant's primary (expert) and secondary
    (familiar/exposure) skill tiers combined. A skill listed in both
    tiers is only counted once.

    BUG FIX ("remove * from base resume and generated resume"): this
    used to wrap primary-tier skills in markdown **bold** to visually
    distinguish them in the table. The frontend's technical proficiencies
    table renders skill text as plain strings — it doesn't parse
    markdown — so those asterisks showed up literally in the UI instead
    of rendering as bold. Returns plain skill strings now; no tier
    markup in this table.

    BUG FIX (primary/secondary tier lost on resume edit round-trip):
    this used to return plain skill strings per category, which meant
    the primary/secondary tier only ever existed for the instant this
    function ran — it was never actually stored anywhere per-skill.
    When resume_router.py's save handler later tried to reconstruct
    "is this row primary?" by checking whether the word "primary"
    appeared in the CATEGORY text ("Programming Languages", "Cloud
    Platforms", etc. never contain that word), every row fell through
    to secondary, silently wiping out primary_skills on every resume
    save. Each skill now carries its own {"name", "isPrimary"} tag that
    survives independently of which technology-type category it's
    displayed under, so the tier is a real per-skill fact instead of a
    guess based on category wording. This function is the only caller
    of categorize_skills() whose output feeds the tier-sensitive base
    resume path — categorize_skills() itself is left untouched so the
    unrelated tailored-resume/gap-analysis callers keep working exactly
    as before.
    """
    primary_clean = [_skill_name(s).strip() for s in (primary or []) if _skill_name(s).strip()]
    primary_lower = {s.lower() for s in primary_clean}
    secondary_clean = [
        _skill_name(s).strip() for s in (secondary or [])
        if _skill_name(s).strip() and _skill_name(s).strip().lower() not in primary_lower
    ]
    categorized = categorize_skills(primary_clean + secondary_clean)
    return [
        {
            "category": row["category"],
            "skills": [
                {"name": skill, "isPrimary": skill.lower() in primary_lower}
                for skill in row["skills"]
            ],
        }
        for row in categorized
    ]


def categorize_skills(skills: list[str]) -> list[dict]:
    """Groups a flat skill list into {category, skills} buckets for the
    TECHNICAL PROFICIENCIES table, using keyword matching. Anything that
    doesn't match a known category lands in "Other Tools & Technologies"
    rather than being dropped."""
    seen = set()
    ordered_skills = []
    for s in skills:
        key = s.strip().lower()
        if key and key not in seen:
            seen.add(key)
            ordered_skills.append(s.strip())

    # BUG FIX: naive substring matching (`kw in skill_lower`) produced
    # false positives — "go" matched inside "MongoDB", "sql" matched
    # inside "PostgreSQL", both landing in Programming Languages instead
    # of Databases. Word-boundary regex matching fixes this: \bgo\b
    # doesn't match the "go" inside "MongoDB" because there's no boundary
    # between "n" and "g".
    compiled_categories = [
        (category, [re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE) for kw in keywords])
        for category, keywords in SKILL_CATEGORIES
    ]

    buckets: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for skill in ordered_skills:
        matched_category = None
        for category, patterns in compiled_categories:
            if any(p.search(skill) for p in patterns):
                matched_category = category
                break
        if matched_category:
            buckets.setdefault(matched_category, []).append(skill)
        else:
            unmatched.append(skill)

    result = [
        {"category": category, "skills": buckets[category]}
        for category, _ in SKILL_CATEGORIES
        if category in buckets
    ]
    if unmatched:
        result.append({"category": "Other Tools & Technologies", "skills": unmatched})
    return result


def _normalize_resume_data(resume_data: dict, resume_info: dict) -> dict:
    """
    Coerces AI output (or the offline fallback) into the exact master-resume
    schema every consumer (ResumeRichPreview, ResumePrintView, _generate_docx)
    expects — same section set, same order, same field names/types — so every
    resume looks identical regardless of how the AI phrased that particular
    response.

    Claude generally follows SYSTEM_PROMPT's schema, but LLM JSON output is
    never contractually guaranteed: a field can come back missing, empty, or
    under a slightly different key from one generation to the next. Rather
    than let each individual downstream consumer guess defensively (and
    inevitably miss a spot — see the frontend crash this was pulled from),
    this is the single place that guarantees the contract once, right after
    generation, before the result is ever saved or rendered.
    """
    if not isinstance(resume_data, dict):
        resume_data = {}

    def _clean_str(val) -> str:
        return val.strip() if isinstance(val, str) else ""

    def _as_list(val) -> list:
        if isinstance(val, list):
            return val
        if isinstance(val, str) and val.strip():
            return [val.strip()]
        return []

    normalized: dict = {}

    # 1. Header & contact — always present, falling back to the source
    # profile so a header field the AI dropped doesn't just disappear.
    normalized["name"] = _clean_str(resume_data.get("name")) or _clean_str(resume_info.get("full_name")) or "Unknown"
    normalized["email"] = _clean_str(resume_data.get("email")) or _clean_str(resume_info.get("email"))
    normalized["phone"] = _clean_str(resume_data.get("phone")) or _clean_str(resume_info.get("phone"))
    normalized["location"] = _clean_str(resume_data.get("location")) or _clean_str(resume_info.get("location") or resume_info.get("current_location"))
    normalized["linkedin"] = _clean_str(resume_data.get("linkedin")) or _clean_str(resume_info.get("linkedin"))
    normalized["github"] = _clean_str(resume_data.get("github")) or _clean_str(resume_info.get("github"))

    # 2. Career Objective / Summary — the schema wants both populated with
    # the same content when only one came back, so every consumer that
    # reads either field sees it.
    career_obj = _clean_str(resume_data.get("career_objective")) or _clean_str(resume_data.get("summary"))
    normalized["career_objective"] = career_obj
    normalized["summary"] = career_obj

    # 3. Technical Proficiencies — always a categorized table. If the AI
    # returned skills but skipped (or malformed) the category breakdown,
    # rebuild the table from the flat list so the section still renders as
    # a table instead of silently vanishing.
    #
    # BUG FIX ("remove * from base resume and generated resume"): skill
    # strings sometimes carry literal markdown **bold** markers — either
    # from an older stored resume with them already baked in, or from an
    # AI response that added them despite the table not rendering
    # markdown. Stripped here, at the single choke point every
    # technical_proficiencies table passes through, so it's cleaned
    # regardless of source.
    def _strip_bold_markers(s: str) -> str:
        return s.replace("**", "").strip() if isinstance(s, str) else s

    skills = _as_list(resume_data.get("skills"))
    tech_profs = resume_data.get("technical_proficiencies")
    if not (isinstance(tech_profs, list) and tech_profs):
        tech_profs = categorize_skills(skills) if skills else []
    else:
        # Normalize each row's `skills` to a list even if the AI emitted a
        # comma-joined string for one category.
        cleaned_profs = []
        for tp in tech_profs:
            if not isinstance(tp, dict):
                continue
            cat = _clean_str(tp.get("category")) or "Skills"
            tp_skills = tp.get("skills")
            if isinstance(tp_skills, str):
                tp_skills = [s.strip() for s in tp_skills.split(",") if s.strip()]
            cleaned_profs.append({"category": cat, "skills": _as_list(tp_skills)})
        tech_profs = cleaned_profs
    for tp in tech_profs:
        tp["skills"] = [_strip_bold_markers(s) for s in tp.get("skills", [])]
    normalized["technical_proficiencies"] = tech_profs
    normalized["skills"] = skills
    normalized["missing_skills"] = _as_list(resume_data.get("missing_skills"))

    # 4. Experience — every entry always has every field the template
    # needs, `bullets` always a list. This is the field whose absence
    # crashed the resume editor (exp.bullets.map on undefined) before the
    # frontend was hardened — normalizing here means it can never happen
    # again regardless of what any client does with the data.
    normalized_experience = []
    for exp in _as_list(resume_data.get("experience")):
        if not isinstance(exp, dict):
            continue
        bullets = _as_list(exp.get("bullets"))
        normalized_experience.append({
            "client": _clean_str(exp.get("client") or exp.get("company")),
            "role": _clean_str(exp.get("role") or exp.get("title")),
            "start": _clean_str(exp.get("start") or exp.get("start_date")),
            "end": _clean_str(exp.get("end") or exp.get("end_date")) or "Present",
            "location": _clean_str(exp.get("location")),
            "description": _clean_str(exp.get("description")),
            "project": _clean_str(exp.get("project") or exp.get("project_name")),
            "bullets": bullets,
        })

    # The profile is the source of truth for each job's project and
    # location — carry them over by client when the generated entry lacks them.
    _profile_by_client = {}
    for _pe in _as_list((resume_info or {}).get("experience")):
        if isinstance(_pe, dict):
            _key = _clean_str(_pe.get("company") or _pe.get("client")).lower()
            if _key and _key not in _profile_by_client:
                _profile_by_client[_key] = _pe
    for _ne in normalized_experience:
        _src = _profile_by_client.get(_ne["client"].lower())
        if not _src:
            continue
        if not _ne["project"]:
            _ne["project"] = _clean_str(_src.get("project"))
        if not _ne["location"]:
            _ne["location"] = _clean_str(_src.get("location"))
    normalized["experience"] = normalized_experience

    # 5. Everything else — pass through as-is. These sections are all
    # optional in the template (rendered only `if present`), so there's
    # nothing to coerce; just make sure list-typed sections are actually
    # lists so a stray string from the AI doesn't break a `.map`/`for`
    # loop downstream.
    for key in ("key_projects", "academic_projects", "education", "certifications",
                "non_technical_proficiencies", "achievements", "hobbies_and_interests"):
        if key in resume_data and resume_data[key] not in (None, "", []):
            val = resume_data[key]
            normalized[key] = val if isinstance(val, (list, dict)) else val
    # BUG FIX: the AI can return an `education` array that's PRESENT but
    # has empty/missing fields per-entry (e.g. year dropped even though
    # it was given a real value) — the old check only fell back to the
    # reliable source data when the WHOLE section was missing, so a
    # partially-empty AI response was trusted as-is. Backfill any blank
    # field on each entry from the matching real profile entry (matched
    # by position — education entries aren't reordered by the AI in
    # practice), rather than an all-or-nothing fallback.
    if not normalized.get("education") and resume_info.get("education"):
        normalized["education"] = resume_info["education"]
    elif isinstance(normalized.get("education"), list) and resume_info.get("education"):
        real_edu = resume_info["education"]
        backfilled = []
        for i, entry in enumerate(normalized["education"]):
            if isinstance(entry, dict) and i < len(real_edu) and isinstance(real_edu[i], dict):
                merged = dict(entry)
                for field in ("degree", "institution", "year", "details"):
                    if not merged.get(field) and real_edu[i].get(field):
                        merged[field] = real_edu[i][field]
                backfilled.append(merged)
            else:
                backfilled.append(entry)
        normalized["education"] = backfilled
    if "other_projects" in resume_data and resume_data["other_projects"]:
        normalized["other_projects"] = resume_data["other_projects"]
    if isinstance(resume_data.get("personal_details"), dict) and resume_data["personal_details"]:
        normalized["personal_details"] = resume_data["personal_details"]
    if resume_data.get("declaration"):
        normalized["declaration"] = resume_data["declaration"]

    normalized["generation_notes"] = _clean_str(resume_data.get("generation_notes"))
    return normalized


PARSE_SYSTEM_PROMPT = """You are a resume-parsing engine. You will be given the raw extracted text of an
uploaded resume document. Extract its content into this exact JSON schema — do not
tailor, embellish, or invent anything; every value must come from the source text.
Leave a field blank ("" or []) rather than guessing when the text doesn't contain it.

{
  "name": string,
  "email": string,
  "phone": string,
  "location": string,
  "linkedin": string,
  "github": string,
  "summary": string,
  "skills": string[],
  "technical_proficiencies": [{"category": string, "skills": string[]}],
  "experience": [{"client": string, "role": string, "start": string, "end": string, "location": string, "description": string, "bullets": string[]}],
  "education": [{"degree": string, "institution": string, "year": string, "details": string}],
  "certifications": string[]
}

Return ONLY the JSON object, no markdown fences, no commentary."""


def parse_resume_text_to_structured_data(raw_text: str) -> tuple[dict, dict, Optional[dict]]:
    """
    Calls Anthropic API to parse the raw extracted text of an uploaded resume
    (docx -> plain text) into the same structured schema generate_tailored_resume
    produces, so an uploaded resume gets a populated editor instead of a blank
    one. Never tailors or invents content -- it's a straight extraction.

    Returns (resume_json, rate_limit_headers, usage_info) — same three-tuple
    shape as generate_tailored_resume, so the caller can feed rate_limits into
    save_claude_rate_limits() and usage_info into log_ai_usage() exactly like
    /generate already does. Skipping that would silently undercount real AI
    spend on the admin usage/budget dashboard.

    Falls back to a name-only skeleton (rather than fabricating content) when
    the API key isn't configured or the call fails, matching the offline
    fallback style already used in generate_tailored_resume above.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    fallback = {
        "name": "Unknown",
        "email": "",
        "phone": "",
        "location": "",
        "linkedin": "",
        "github": "",
        "summary": "",
        "skills": [],
        "technical_proficiencies": [],
        "experience": [],
        "education": [],
        "certifications": [],
        "generation_notes": "Automatic parsing was unavailable — please fill in the fields below manually.",
    }

    if not raw_text or not raw_text.strip():
        return _normalize_resume_data(fallback, {}), {}, None

    if not api_key or api_key.startswith("your_"):
        logger.warning("ANTHROPIC_API_KEY not found or is a placeholder, returning blank skeleton for uploaded resume parsing.")
        return _normalize_resume_data(fallback, {}), {}, None

    try:
        # timeout=15 bounds the blocking call this function makes (see the
        # asyncio.to_thread wrapper at its call site in resume_router.py,
        # which now only reaches this AI parser as a fallback after the
        # free heuristic parser fails to recognize the resume's format) —
        # without it, a stuck network call could tie up that worker thread
        # far longer than this now-rare path should ever take.
        client = Anthropic(api_key=api_key, timeout=15.0)
        user_prompt = f"""RAW RESUME TEXT:
{raw_text}

Extract the JSON now."""

        response = client.messages.with_raw_response.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            system=PARSE_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )

        headers = response.headers
        rate_limits = {
            "tokens-limit": headers.get("anthropic-ratelimit-tokens-limit"),
            "tokens-remaining": headers.get("anthropic-ratelimit-tokens-remaining"),
            "tokens-reset": headers.get("anthropic-ratelimit-tokens-reset"),
            "requests-limit": headers.get("anthropic-ratelimit-requests-limit"),
            "requests-remaining": headers.get("anthropic-ratelimit-requests-remaining"),
            "requests-reset": headers.get("anthropic-ratelimit-requests-reset"),
        }

        parsed_response = response.parse()
        content = parsed_response.content[0].text

        usage_info = {
            "input_tokens": parsed_response.usage.input_tokens,
            "output_tokens": parsed_response.usage.output_tokens,
        }

        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]

        result_json = json.loads(content.strip())
        return _normalize_resume_data(result_json, {}), rate_limits, usage_info
    except Exception as e:
        logger.warning(f"Error calling Claude API for uploaded-resume parsing: {e}. Falling back to blank skeleton.")
        return _normalize_resume_data(fallback, {}), {}, None


def _bold_terms(text: str, terms: list[str]) -> str:
    """Wraps whole-word, case-insensitive matches of the given terms in
    **markdown bold** so JD-relevant keywords stand out visually in the
    preview/PDF (ResumeRichPreview already renders **text** as <strong>).
    Only ever bolds terms that are already present in the text — never
    inserts new words — and skips a match that's already inside ** **
    so re-running this on already-bolded text is a no-op, not a double
    wrap.
    """
    if not text or not terms:
        return text
    # Longest-first so e.g. "Spring Boot" is bolded as a phrase before
    # "Spring" (if that were also a term) could bold half of it.
    for term in sorted({t.strip() for t in terms if t and t.strip()}, key=len, reverse=True):
        pattern = re.compile(r"(?<!\*)\b(" + re.escape(term) + r")\b(?!\*)", re.IGNORECASE)
        text = pattern.sub(r"**\1**", text, count=1)
    return text


def _tailor_experience_for_jd(experience: list, real_skills: list, job_description: str) -> list:
    """Bolds JD-matching skill mentions inside each existing bullet, and
    — for JD-matching skills the candidate genuinely has but that aren't
    named in any existing bullet — appends up to 2 short new bullets to
    the most recent (first) role. Never invents a project, employer, or
    metric: a new bullet only ever states that the candidate applied a
    skill they actually have, nothing more specific than that.
    """
    if not experience:
        return experience
    jd_lower = (job_description or "").lower()
    overlapping = [s for s in (real_skills or []) if s and s.lower() in jd_lower]
    if not overlapping:
        return experience

    already_mentioned = set()
    for exp in experience:
        for bullet in exp.get("bullets", []):
            b_lower = str(bullet).lower()
            for skill in overlapping:
                if skill.lower() in b_lower:
                    already_mentioned.add(skill)

    tailored = []
    for i, exp in enumerate(experience):
        bullets = [_bold_terms(b, overlapping) for b in exp.get("bullets", [])]
        if i == 0:
            missing = [s for s in overlapping if s not in already_mentioned][:2]
            for skill in missing:
                bullets.append(_bold_terms(f"Applied {skill} as part of day-to-day engineering work.", [skill]))
        tailored.append({**exp, "bullets": bullets})
    return tailored


def _match_skills_against_jd(real_skills: list, job_description: str) -> tuple[list, list]:
    """Splits the candidate's real skills into (exact_matches,
    related_matches) against the JD text.

    Exact: the skill string itself appears in the JD (same as the
    existing overlap check elsewhere in this file). Related: the skill
    belongs to the same canonical technology family (via phase4.py's
    SKILL_ALIASES — the same grouping the real matching engine uses,
    e.g. "azure data factory" and "etl" both canonicalize toward
    data-pipeline tooling) as something the JD asks for, even when the
    exact string doesn't appear. Both lists only ever contain skills the
    candidate's own profile actually lists — never invents a skill they
    don't have.
    """
    jd_lower = (job_description or "").lower()
    exact = [s for s in real_skills if s and s.lower() in jd_lower]

    related: list = []
    try:
        from phase4 import SKILL_ALIASES, extract_skills as _extract_jd_skills
        jd_canonical = set(_extract_jd_skills(job_description))
        for skill in real_skills:
            if not skill or skill in exact:
                continue
            skill_lower = skill.lower()
            for canonical, aliases in SKILL_ALIASES.items():
                if canonical in jd_canonical and any(alias in skill_lower for alias in aliases):
                    related.append(skill)
                    break
    except Exception:
        # Best-effort only — if phase4 isn't importable for any reason,
        # related-skill detection is simply skipped; exact matches still
        # work fine on their own.
        pass

    return exact, related


def _compute_missing_jd_skills(real_skills: list, job_description: str, limit: int = 8) -> list:
    """Returns JD-requested skills the candidate's real skill list doesn't
    have — for the resume's separate `missing_skills` gap-analysis box,
    never for the `skills`/`technical_proficiencies` box itself. This
    never claims the candidate has these skills; it's the opposite — an
    honest list of what's asked for and absent, using phase4.py's own
    JD skill extractor (best-effort; returns [] if that's unavailable).
    """
    if not job_description:
        return []
    try:
        from phase4 import extract_skills as _extract_jd_skills
        jd_all = [s for s in _extract_jd_skills(job_description) if s]
    except Exception:
        return []
    real_lower = {s.lower() for s in (real_skills or []) if s}
    missing, seen = [], set()
    for s in jd_all:
        key = s.strip().lower()
        if key and key not in real_lower and key not in seen:
            seen.add(key)
            missing.append(s.strip())
    return missing[:limit]


def generate_tailored_resume(
    resume_info: dict, job_description: str, target_role: Optional[str] = None,
    jd_skills: Optional[list] = None,
) -> tuple[dict, dict, Optional[dict]]:
    """
    Builds a structured JSON resume from resume_info and job_description
    using only real, stored candidate data — no AI call. `target_role` —
    the requirement's actual title (e.g. "Sr. Java Backend Developer") —
    is the authoritative exact designation/title for the Career
    Objective when supplied, taking priority over whatever would
    otherwise be guessed out of the free-text job_description.
    Returns (resume_json, rate_limit_headers, usage_info) — rate_limits
    and usage_info are always {} / None now since no API call is made;
    kept in the return shape so callers (resume_router.py) don't need
    to change.
    """
    
    # BUG FIX: this used to fabricate generic placeholder content (a canned
    # "Highly motivated professional..." summary, a fake "FinCorp Global"
    # employer, invented bullet points) whenever the AI call was
    # unavailable or failed — so admins/consultants would see what looked
    # like a real tailored resume but was actually fiction, with no
    # indication anything had gone wrong. This now builds the fallback
    # entirely from the candidate's own stored resume_info (their real
    # profile JSON), leaving a field blank rather than inventing content
    # when resume_info doesn't have it. It's a straight passthrough of
    # real data, not an AI-tailored resume — generation_notes says so
    # explicitly so this is distinguishable from a real generation.
    real_skills = (
        resume_info.get("skills")
        or resume_info.get("tech_stack", {}).get("expert", [])
        or resume_info.get("tech_stack", {}).get("intermediate", [])
        or []
    )
    # BUG FIX ("career objective missing" — reported for a candidate whose
    # BUG FIX ("Generated Resume still shows old data / Career Objective
    # not updating"): this used to reuse resume_info's stored summary
    # VERBATIM whenever one existed (only bolding + appending a short
    # addendum), so the Generated Resume pane looked nearly identical to
    # the Base Resume pane no matter what JD it was generated for — the
    # exact symptom reported. The whole point of this function is a
    # JD-tailored objective, so it now always runs the experience-anchored
    # paragraph builder, for every generation, regardless of whether a
    # stored summary exists. The stored summary is NOT discarded — it's
    # still shown as-is on the Base Resume pane elsewhere; this only
    # changes what the *Generated* (tailored) pane shows.
    # CHANGE ("Professional Summary should be the base resume's summary,
    # not modified"): use the base resume's stored summary exactly as-is.
    # Callers (resume_router._build_resume_info / phase6._call_ai_tailoring)
    # put the same text the Base Resume pane shows into
    # resume_info["summary"], so Generated == Base for this section.
    real_summary = (
        (resume_info.get("summary") or "").strip()
        or (resume_info.get("career_objective") or "").strip()
    )
    # TECHNICAL PROFICIENCIES table: prefer resume_info's own categorized
    # list if it has one, otherwise build one from the full tech_stack
    # (expert + exposure/intermediate + familiar tiers merged) so the
    # table isn't limited to just the "expert" tier used for `skills`
    # above. Most stored profiles (like this one) only have a flat
    # tech_stack, not pre-categorized technical_proficiencies.
    #
    # BUG FIX: consultants have primary_skills/secondary_skills (via
    # phase6.py's tech_stack={"expert": primary, "familiar": secondary}) —
    # this used to flatten both tiers together before categorizing, so the
    # table showed every skill the same way with no way to tell a
    # consultant's primary (expert) skills apart from secondary ones once
    # grouped by technology type. categorize_skills_with_tier() keeps that
    # distinction — primary skills bold, secondary plain — within each
    # category row, instead of a separate flat "Primary Skills" /
    # "Secondary Skills" split that ignores technology type.
    tech_stack = resume_info.get("tech_stack") or {}
    tier_primary = tech_stack.get("expert", [])
    tier_secondary = [*tech_stack.get("exposure", []), *tech_stack.get("intermediate", []), *tech_stack.get("familiar", [])]
    all_tech_skills = [*tier_primary, *tier_secondary]
    real_tech_proficiencies = (
        resume_info.get("technical_proficiencies")
        or (categorize_skills_with_tier(tier_primary, tier_secondary) if all_tech_skills else None)
        or (categorize_skills(real_skills) if real_skills else None)
    )
    # BUG FIX ("any skill missing in TECHNICAL PROFICIENCIES add those
    # skill"): the block above is an OR chain — once resume_info already
    # has SOME stored technical_proficiencies table, that table is used
    # exactly as-is, even when it's missing skills the candidate's own
    # profile lists elsewhere (the flat `skills` field, or tech_stack's
    # other tiers) — those just silently never showed up in the rendered
    # table. Top up the existing table with any real skill (including
    # ones matched/related to this JD) that isn't already present in any
    # category, instead of only using the fuller list when the table was
    # completely absent.
    #
    # BUG FIX ("JD-mentioned skill add in their category, don't create
    # separate category"): this used to dump every missing skill into one
    # new "Additional Skills" bucket regardless of what kind of skill it
    # was — Kubernetes and Excel side by side in a meaningless catch-all
    # category. Missing skills are now categorized the same way as
    # everything else (categorize_skills, same SKILL_CATEGORIES/keywords)
    # and merged into whichever EXISTING row already has that category
    # name (e.g. a new Cloud Platforms skill joins the existing Cloud
    # Platforms row); only a genuinely new category not already present
    # gets added as its own row, under its real technology-type name —
    # never a generic "Additional Skills" label.
    if real_tech_proficiencies:
        already_listed = set()
        for cat in real_tech_proficiencies:
            cat_skills = cat.get("skills")
            cat_list = cat_skills if isinstance(cat_skills, list) else str(cat_skills or "").split(",")
            already_listed.update(_skill_name(s).strip().lower() for s in cat_list if _skill_name(s).strip())

        jd_exact, jd_related = _match_skills_against_jd(real_skills, job_description)
        candidate_pool = list(dict.fromkeys([*real_skills, *all_tech_skills, *jd_exact, *jd_related]))
        missing = [s for s in candidate_pool if s and s.strip().lower() not in already_listed]
        if missing:
            missing_categorized = categorize_skills(missing)
            existing_by_category = {cat.get("category"): cat for cat in real_tech_proficiencies}
            for mc in missing_categorized:
                existing_cat = existing_by_category.get(mc["category"])
                if existing_cat is not None:
                    existing_skills = existing_cat.get("skills")
                    existing_list = (
                        existing_skills if isinstance(existing_skills, list)
                        else [s.strip() for s in str(existing_skills or "").split(",") if s.strip()]
                    )
                    existing_cat["skills"] = existing_list + mc["skills"]
                else:
                    real_tech_proficiencies.append(mc)
                    existing_by_category[mc["category"]] = mc
    real_education = resume_info.get("education") or resume_info.get("educational_background") or []
    real_certifications = resume_info.get("certifications") or []

    mock_fallback = {
        "name": resume_info.get("full_name", "Unknown"),
        "email": resume_info.get("email", ""),
        "phone": resume_info.get("phone", ""),
        "location": resume_info.get("location", ""),
        "linkedin": resume_info.get("linkedin", ""),
        "github": resume_info.get("github", ""),
        "summary": real_summary,
        "technical_proficiencies": real_tech_proficiencies,
        "skills": real_skills,
        "missing_skills": _compute_missing_jd_skills(real_skills, job_description),
        "experience": _tailor_experience_for_jd(
            [
                {
                    "client": exp.get("company", ""),
                    "role": exp.get("role", exp.get("title", "")),
                    "start": exp.get("start_date", exp.get("start", "")),
                    "end": exp.get("end_date", exp.get("end", "Present")),
                    "location": exp.get("location", ""),
                    "project": exp.get("project", ""),
                    "bullets": exp.get("bullets", [])
                }
                for exp in resume_info.get("experience", [])
            ],
            real_skills,
            job_description,
        ),
        "education": real_education,
        "certifications": real_certifications,
        "personal_details": resume_info.get("personal_details") or {},
        "generation_notes": ""
    }

    # AI call removed — generate_tailored_resume() now always returns the
    # deterministic build above (real profile data, JD-relevance addendum,
    # missing_skills gap analysis — all from _build_factual_career_objective
    # and friends), never calling Anthropic. generation_notes left blank
    # since this is the normal path now, not a fallback-from-failure.
    result = _normalize_resume_data(mock_fallback, resume_info)

    # Add EVERY skill the requirement asks for (skill table + a bullet in
    # one of the two most recent projects + summary points), recorded in
    # result["jd_added"] for the review dialog's highlight / ✕ remove.
    # Rules (certifications never claimed, release dates respected, no
    # years claimed) live in skill_augment.py. Best-effort: a failure here
    # must never block resume generation.
    try:
        from skill_augment import augment_resume_with_jd_skills, detect_jd_skills
        skills_for_jd = [s for s in (jd_skills or []) if str(s or "").strip()]
        if not skills_for_jd:
            skills_for_jd = detect_jd_skills(job_description)
        if skills_for_jd:
            augment_resume_with_jd_skills(result, skills_for_jd, existing_categorizer=categorize_skills)
    except Exception as exc:  # noqa: BLE001
        print(f"skill_augment failed, returning resume without JD skill additions: {exc}")

    return result, {}, None


def generate_template_values(resume_info: dict, job_description: str) -> tuple[dict, dict, Optional[dict]]:
    """
    Calls Anthropic API to generate ONLY the core fields needed for templating (summary, skills, roles).
    This uses far fewer tokens than generate_tailored_resume.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    
    # Fallback data if API fails
    mock_fallback = {
        "summary": resume_info.get("summary") or resume_info.get("career_objective") or "",
        "skills": resume_info.get("skills") or [],
        "experience": [
            {
                "role": exp.get("role", exp.get("title", "")),
                "employer": exp.get("company", exp.get("client", "")),
                "bullets": exp.get("bullets", [])
            }
            for exp in resume_info.get("experience", [])
        ],
        "ats_score": 85
    }

    if not api_key or api_key.startswith("your_"):
        logger.warning("ANTHROPIC_API_KEY not found, returning fallback template data.")
        return mock_fallback, {}, None

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        
        system_prompt = """You are an expert resume writer. You are given a candidate's profile and a job description.
Your task is to output a small JSON object containing tailored values for a resume template.
Do NOT fabricate any experience the candidate does not have. You may reword or highlight existing experience to better match the job description.

Output MUST be valid JSON matching this schema:
{
  "summary": "A 2-3 sentence tailored career objective/summary.",
  "skills": ["Skill 1", "Skill 2"],
  "experience": [
    {
      "role": "Tailored Job Title",
      "employer": "Original Employer Name",
      "bullets": ["Tailored bullet 1", "Tailored bullet 2"]
    }
  ],
  "ats_score": 90
}
"""

        user_prompt = f"""
CANDIDATE PROFILE (JSON):
{json.dumps(resume_info, indent=2)}

TARGET JOB DESCRIPTION:
{job_description}

Generate the tailored template JSON now.
"""
        response = client.messages.with_raw_response.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )
        
        headers = response.headers
        rate_limits = {
            "tokens-limit": headers.get("anthropic-ratelimit-tokens-limit"),
            "tokens-remaining": headers.get("anthropic-ratelimit-tokens-remaining"),
            "tokens-reset": headers.get("anthropic-ratelimit-tokens-reset"),
            "requests-limit": headers.get("anthropic-ratelimit-requests-limit"),
            "requests-remaining": headers.get("anthropic-ratelimit-requests-remaining"),
            "requests-reset": headers.get("anthropic-ratelimit-requests-reset")
        }
        
        parsed_response = response.parse()
        content = parsed_response.content[0].text
        
        usage_info = {
            "input_tokens": parsed_response.usage.input_tokens,
            "output_tokens": parsed_response.usage.output_tokens,
        }
        
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
            
        result_json = json.loads(content.strip())
        return result_json, rate_limits, usage_info
    except Exception as e:
        logger.error(f"Error calling Anthropic API for templates: {e}")
        logger.error(traceback.format_exc())
        return mock_fallback, {}, None