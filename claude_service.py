import os
import json
import re
import time
from typing import Optional
from anthropic import Anthropic, AuthenticationError, BadRequestError, PermissionDeniedError
import logging
import traceback
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# PERF/BUG FIX ("saving profile takes a long time" / "Failed to save —
# changes rolled back", root cause traced to a stuck ANTHROPIC_API_KEY):
# update_own_profile's background re-matching task (see phase3.py) calls
# evaluate_role_match_with_ai / parse_requirement_text below ONCE PER
# consultant/requirement pair being compared. When the API key is invalid,
# the account is out of credits, or a bad model name is configured, every
# single one of those calls still makes a real network round-trip to
# Anthropic before failing — dozens or hundreds of them per matching run,
# each with its own latency. That background task holds one of only 2
# available DB connections per worker open for its ENTIRE duration (see
# database.py's DB_POOL_SIZE), so a matching run stretched out by a wall
# of doomed API calls can starve the connection pool for everything else
# running concurrently — including the actual profile-save request
# itself, which is how an unrelated, purely-cosmetic "AI matching"
# background feature ended up causing "Work Auth save" to hang for 20+
# seconds and roll back.
#
# A bad key/credits/model doesn't fix itself between one call and the
# next a few milliseconds later — so once ANY call hits one of these
# specific, unambiguous "this key/account/config cannot currently make
# calls" errors, skip every further Claude call for a cooldown window
# instead of retrying each one individually. Falls back to whatever the
# caller already does on a None/failed result (these are all "best
# effort, degrade gracefully" call sites), so behavior on a genuinely
# broken key is unchanged except for speed — it now fails fast instead
# of failing slow, hundreds of times in a row.
_CLAUDE_CIRCUIT_BROKEN_UNTIL: float = 0.0
_CLAUDE_CIRCUIT_COOLDOWN_SECONDS = 300  # 5 minutes


def _claude_circuit_is_open() -> bool:
    return time.monotonic() < _CLAUDE_CIRCUIT_BROKEN_UNTIL


def _trip_claude_circuit(reason: str) -> None:
    global _CLAUDE_CIRCUIT_BROKEN_UNTIL
    _CLAUDE_CIRCUIT_BROKEN_UNTIL = time.monotonic() + _CLAUDE_CIRCUIT_COOLDOWN_SECONDS
    logger.warning(
        "Anthropic API circuit breaker tripped (%s) — skipping further "
        "Claude calls for %ss instead of retrying each one individually.",
        reason, _CLAUDE_CIRCUIT_COOLDOWN_SECONDS,
    )


def _is_hard_claude_failure(exc: Exception) -> bool:
    """True for errors that mean 'this key/account cannot make calls right
    now', not a one-off network blip — auth failures, and the specific
    invalid_request_error Anthropic returns for an empty credit balance."""
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return True
    if isinstance(exc, BadRequestError):
        return "credit balance" in str(exc).lower()
    return False

def get_working_anthropic_client():
    """
    Tries each configured Anthropic API key in order (ANTHROPIC_API_KEY,
    ANTHROPIC_API_KEY_2, ANTHROPIC_API_KEY_3, ...) and returns the first
    one that actually works — so a single expired/invalid/out-of-credits
    key doesn't take down resume generation entirely when backup keys
    are available.
    Returns (client, api_key) tuple, or (None, None) if all keys fail
    or none are configured.
    """
    keys_to_try = []
    primary = os.getenv("ANTHROPIC_API_KEY")
    if primary and not primary.startswith("your_"):
        keys_to_try.append(primary)

    i = 2
    while True:
        extra_key = os.getenv(f"ANTHROPIC_API_KEY_{i}")
        if not extra_key:
            break
        keys_to_try.append(extra_key)
        i += 1

    for key in keys_to_try:
        try:
            client = Anthropic(api_key=key)
            client.models.list()
            return client, key
        except Exception as e:
            logger.warning(f"Anthropic API key ending in ...{key[-6:]} failed: {e}")
            continue

    return None, None


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


SYSTEM_PROMPT = """You are an elite Fortune 500 Resume Architect and Principal Technical Recruiter.
Your objective is to transform a candidate's profile into an exceptionally tailored, rock-solid, ATS-optimized master technical resume following the comprehensive Shiva Shankar Master Resume Template.

CRITICAL STANDARDS & INSTRUCTIONS:

1. COMPREHENSIVE MASTER RESUME SCHEMA:
   You must populate all relevant standard sections:
   - Header & Contact Info (name, email, phone, location, linkedin, github)
   - CAREER OBJECTIVE / PROFESSIONAL SUMMARY (Rich 3-4 sentence summary tailored to target role with markdown bolding **like this**)
   - TECHNICAL PROFICIENCIES (Categorized array: Programming Languages, DBMS, Frameworks & Tools, Cloud & DevOps, Scripting & Other)
   - EXPERIENCE (Company/Client, Designation/Role, Dates, Location, Description, 4-6 rich bullet points with **bolded keywords & metrics**)
   - KEY PROJECTS (Numbered detailed projects with title, description, role, responsibilities, team_size, duration, technical_tools)
   - ACADEMIC PROJECTS (Academic/university projects with platform, description, role, responsibilities, team_size, duration, technical_tools)
   - OTHER PROJECTS (Additional work, bug fixes, R&D, porting work)
   - EDUCATIONAL BACKGROUND (Degree, institution, year, details/percentage)
   - CERTIFICATIONS (Professional certifications with issuing body and year, kept separate from ACHIEVEMENTS)
   - NON-TECHNICAL PROFICIENCIES (Soft skills, leadership, administration bullets)
   - ACHIEVEMENTS (Certifications, paper presentations, awards)
   - HOBBIES & INTEREST (Interests bullets)
   - PERSONAL DETAILS (Father's name, DOB, languages known, permanent address, desired work location)
   - DECLARATION (Formal declaration text, place, name)

2. CAREER OBJECTIVE GENERATION — FOLLOW THIS EXACT PROCESS FOR EVERY JD:
   The candidate's provided profile data is the source of truth for the Career Objective, exactly as it is for every other section. It must be freshly written for THIS specific JD every single time — never reuse or lightly reword a previous objective, and never simply copy the candidate's existing summary/objective text if one was provided as background; treat it only as source material to rewrite from, not text to preserve.

   Step 1 — Identify the Target Role: if a TARGET ROLE is supplied explicitly in the user message, that is the exact designation/title to use verbatim — do not paraphrase, shorten, or re-derive it from the JD text. Only when no TARGET ROLE is supplied, extract the primary role/title from the CURRENT job description (e.g. "Java Full Stack Developer", "Senior Data Analyst", "React Developer").

   Step 2 — Analyze the Current JD: identify the most important role requirements, technical skills, programming languages, frameworks, tools, cloud technologies, database technologies, responsibilities, domain requirements, and experience requirements. Prioritize whichever of these matter most for this specific role.

   Step 3 — Analyze the candidate's actual profile data (the single source of truth): current/previous roles, years of experience, technical skills, frameworks, tools, projects, responsibilities, domain experience, achievements, certifications — only what is actually present.

   Step 4 — Match profile against Current JD: find the strongest verified overlap between the candidate's real skills and the JD's required/preferred skills. Select only the top 2-3 strongest VERIFIED matches — do not list more than 3 skills in the objective.

   Step 5 — Generate a NEW Career Objective for this JD, exactly 2-3 lines (roughly 30-50 words total), structured as:
     (a) Open with the exact designation/title from Step 1.
     (b) State the candidate's years of experience and core domain (drawn only from the candidate's actual profile).
     (c) Name the 2-3 top matching skills identified in Step 4 — skills that exist in BOTH the candidate's profile AND the JD.
     (d) Close with one sentence on the specific value the candidate brings, tied to a goal or pain point evident in the JD (e.g. reliability, delivery speed, scale, cost, quality) — grounded in what the candidate's profile actually supports, not a generic claim.
   Keep it ATS-friendly and professional: no fluff, no generic filler phrases ("highly motivated", "team player", "results-oriented"), no keyword stuffing. Do not simply copy sentences from the JD — understand it and incorporate relevant requirements naturally.
   Vary sentence structure and word choice across different candidates/JDs — do not default to the same opening pattern (e.g. always "[Title] with [N]+ years of experience...") every single time. Write it the way a specific, genuine person in this exact role would describe themselves for this exact opportunity, not a template with fields swapped in.

   TRUTHFULNESS RULE: only use skills, experience, technologies, responsibilities, projects, and achievements the candidate's actual profile supports. If the JD wants a skill the profile doesn't have: do NOT claim it, do NOT add it to the objective, do NOT invent experience with it, do NOT inflate years of experience.
     Example — JD wants: Java, Spring Boot, AWS, Kubernetes. Profile has: Java, Spring Boot, Microservices.
     Correct (2-3 lines): "Senior Java Developer with 6+ years of experience in backend and microservices development. Skilled in Java, Spring Boot, and Microservices architecture. Brings proven ability to design scalable, maintainable backend systems that reduce production issues and speed up delivery."
     Incorrect: "...expertise in Spring Boot, microservices, AWS, and Kubernetes." — AWS and Kubernetes aren't supported by the profile and must not be claimed.

   Also strategically align experience role titles (`role`) and bullet points so they reflect the target domain while keeping authentic company names and dates unchanged.
   **IMPORTANT FOR JOB TITLES**: Do NOT use the exact same job title for every single experience. Vary the job titles dynamically (e.g. Senior Software Engineer, Lead Developer, Software Engineer, Backend Developer) based on the target role and the progression of the candidate's actual experience. Ensure they are relevant but distinct so they do not all look identical.

3. DETAILED CONTEXT & STRICT TRUTHFULNESS (applies to every section, not just Career Objective):
   - Generate detailed bullet points following: [Action Verb] + [Specific Tech Stack / Frameworks] + [Business / System Context] + [Quantified Impact Metric].
   - **CRITICAL**: Maintain strict accuracy of the candidate's actual experience. Do NOT invent fake projects, clients, skills, or hallucinate arbitrary metrics. Only include metrics if they are reasonably derived from the provided profile. If a JD skill is entirely absent from the candidate's history, mark it as missing; do not fabricate experience with it. Do NOT add false or unsupported experience simply to force a higher match percentage. Truthfulness always outranks JD matching, which always outranks keyword optimization.

4. KEYWORD MARKDOWN BOLDING (`**text**`):
   - Wrap key technical terms, framework names, target role titles, tools, and metrics in markdown bold tags (`**term**`).

Return EXACTLY this JSON structure with no markdown code fences:
{
  "name": "string",
  "email": "string",
  "phone": "string",
  "location": "string",
  "linkedin": "string",
  "github": "string",
  "career_objective": "string (exactly 2-3 lines / ~30-50 words, freshly written for THIS JD per the process above — exact JD title, years+domain, top 2-3 matching skills, one value-proposition line — with **bolded keywords**)",
  "summary": "string",
  "technical_proficiencies": [
    {"category": "Programming Languages", "skills": ["C", "C++", "Java", "Python", "SQL"]},
    {"category": "DBMS", "skills": ["PostgreSQL", "Oracle", "MySQL"]},
    {"category": "Frameworks & Tools", "skills": ["React", "Spring Boot", "Docker", "Kubernetes", "Git"]},
    {"category": "Cloud & DevOps", "skills": ["AWS", "Azure", "CI/CD", "Terraform"]}
  ],
  "skills": ["C", "C++", "Java", "Python", "SQL", "React", "Spring Boot", "AWS", "Docker"],
  "missing_skills": ["skill_not_in_profile"],
  "experience": [
    {
      "client": "string (Company Name)",
      "role": "string (Strategically Aligned Job Title)",
      "start": "string (e.g. Feb 2011)",
      "end": "string (e.g. Present)",
      "location": "string",
      "description": "string (Overview of role & responsibilities)",
      "bullets": [
        "string (Rich 25-45 word bullet with **bolded keywords & metrics**)",
        "string"
      ]
    }
  ],
  "key_projects": [
    {
      "title": "string (Project / Game Title)",
      "description": "string (Project description)",
      "role": "string (Role in project)",
      "responsibilities": ["string", "string"],
      "team_size": "string (e.g. 1)",
      "duration": "string (e.g. 2 Months)",
      "technical_tools": ["tool1", "tool2"]
    }
  ],
  "academic_projects": [
    {
      "title": "string",
      "platform": "string",
      "description": "string",
      "role": "string",
      "responsibilities": ["string"],
      "team_size": "string",
      "duration": "string",
      "technical_tools": ["tool1"]
    }
  ],
  "education": [
    {
      "degree": "string",
      "institution": "string",
      "year": "string",
      "details": "string"
    }
  ],
  "certifications": ["string (e.g. AWS Certified Solutions Architect – Associate, 2023)"],
  "non_technical_proficiencies": ["bullet1", "bullet2"],
  "achievements": ["achievement1", "achievement2"],
  "hobbies_and_interests": ["hobby1", "hobby2"],
  "personal_details": {
    "father_name": "string",
    "dob": "string",
    "marital_status": "string",
    "languages_known": "string",
    "permanent_address": "string",
    "desired_work_location": "string"
  },
  "declaration": {
    "text": "I hereby declare that the above facts given by me are true to the best of my knowledge and belief.",
    "place": "string"
  },
  "generation_notes": "Brief notes on how you tailored the resume."
}"""

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
            "bullets": bullets,
        })
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


_ROLE_HINT_RE = re.compile(r"(?im)^\s*(?:role|position|job title)\s*:\s*(.+)$")

# Marks a span of text as "genuinely new for this requirement" so the
# frontend (ResumeRichPreview.tsx) can highlight only that span instead
# of the whole Career Objective block — matching how skills/bullets/
# certifications are already highlighted per-item, not per-section.
# Plain HTML the frontend already trusts (dangerouslySetInnerHTML) and
# passes through untouched; renderFormattedText only rewrites **bold**.
_MARK_OPEN = '<mark style="background:#fef9c3;border-radius:2px;padding:0 2px;">'
_MARK_CLOSE = "</mark>"


def _extract_role_hint(job_description: str) -> Optional[str]:
    """Pulls a short role/position name straight out of the JD text (e.g.
    a 'Role: AI-Native Java Developer' line, which is how requirements
    are formatted upstream — see phase6.py's jd_context). Returns None
    if no such line is present; never guesses or invents a title.
    """
    if not job_description:
        return None
    match = _ROLE_HINT_RE.search(job_description)
    if not match:
        return None
    role = match.group(1).strip()
    return role[:80] if role else None


def _pick_index(seed_text: str, pool_size: int) -> int:
    """Deterministically picks one of `pool_size` equivalent phrasing
    options from a hash of `seed_text` (e.g. the JD + matched skills).
    Deterministic (not random) so re-generating the same resume for the
    same requirement is stable/reproducible, while different JDs or
    different candidates land on different phrasing — avoiding the
    "every fallback objective reads like a copy-pasted template" problem
    without introducing actual randomness into a code path that's
    otherwise fully deterministic.
    """
    import hashlib
    if pool_size <= 1:
        return 0
    digest = hashlib.md5(seed_text.encode("utf-8")).hexdigest()
    return int(digest, 16) % pool_size


def _build_jd_relevance_addendum(real_skills: list, job_description: str) -> str:
    """Appends a short, factual line tying the Career Objective to THIS
    specific job requirement — so it's never byte-identical to the base
    resume's objective, and never identical across two different
    requirements either.

    Prefers naming which of the candidate's real skills overlap with the
    JD (same keyword-overlap approach as _build_factual_career_objective,
    same rule: only ever names skills the candidate actually has). When
    there's no skill overlap at all, this used to return "" — meaning the
    objective silently stayed frozen for that requirement, contradicting
    "every new requirement needs a new career objective". Falls back to
    naming the JD's own role/position line (if present) instead, which is
    real requirement data, not a fabrication, and differs per requirement
    by construction. Only returns "" if neither is available at all.

    Sentence wording is picked from a small pool of equivalent phrasings
    (deterministically, via a hash of the skills+JD) rather than always
    using the same fixed sentence — so two different requirements for the
    same candidate don't read as a copy-pasted template even when the
    matched skills are similar.

    The returned sentence is wrapped in _MARK_OPEN/_MARK_CLOSE — this is
    the only actually-new text being appended to an existing stored
    summary, so it's the only part that should render highlighted.
    """
    if not job_description or job_description.strip().lower() in ("", "general role"):
        return ""
    jd_lower = job_description.lower()
    overlapping = [s for s in (real_skills or []) if s and s.lower() in jd_lower]
    if overlapping:
        top = overlapping[:5]
        skills_str = ", ".join(top)
        templates = [
            f"This experience directly aligns with the target role's emphasis on {skills_str}.",
            f"These strengths map closely to what this position requires, particularly {skills_str}.",
            f"Well positioned for this opportunity given hands-on depth in {skills_str}.",
        ]
        sentence = templates[_pick_index(job_description + skills_str, len(templates))]
        return _MARK_OPEN + _bold_terms(sentence, top) + _MARK_CLOSE
    role_hint = _extract_role_hint(job_description)
    if role_hint:
        templates = [
            f"Excited to bring this background to the {role_hint} opportunity.",
            f"Looking forward to applying this experience to the {role_hint} role.",
            f"A strong fit for the {role_hint} opening based on this track record.",
        ]
        sentence = templates[_pick_index(job_description + role_hint, len(templates))]
        return _MARK_OPEN + _bold_terms(sentence, [role_hint]) + _MARK_CLOSE
    # Last resort: neither a skill overlap nor a "Role:"-style line was
    # found. Rather than give up and leave the objective frozen, quote a
    # short literal fragment of this JD's own opening text — it's the
    # requirement's real text, not invented, and guarantees the sentence
    # differs across requirements even in this edge case.
    jd_snippet = " ".join(job_description.split())[:60].rstrip(",.;: ")
    if jd_snippet:
        return _MARK_OPEN + f'Reviewed this opportunity closely: "{jd_snippet}…"' + _MARK_CLOSE
    return ""


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


def _join_natural(items: list) -> str:
    """Joins a list into 'A, B, and C' / 'A and B' / 'A' — natural prose
    joining instead of a bare comma list, used for skills mentioned
    inside a flowing paragraph rather than a bullet-style field."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _find_best_matching_experience(experience: list, jd_skills_lower: set) -> Optional[dict]:
    """Picks whichever real experience entry's role/description/bullets
    overlap the JD's matched skills the most, so the paragraph anchors
    on the job actually most relevant to this JD. Falls back to the
    most recent entry (index 0) when no entry has any overlap — never
    invents an experience, only selects among what's genuinely in the
    candidate's own profile."""
    if not experience:
        return None
    best, best_score = None, -1
    for exp in experience:
        if not isinstance(exp, dict):
            continue
        text = " ".join([
            str(exp.get("role") or exp.get("title") or ""),
            str(exp.get("description") or ""),
            " ".join(str(b) for b in (exp.get("bullets") or [])),
        ]).lower()
        score = sum(1 for skill in jd_skills_lower if skill in text)
        if score > best_score:
            best_score, best = score, exp
    if best_score > 0:
        return best
    first = experience[0]
    return first if isinstance(first, dict) else None


def _build_factual_career_objective(
    resume_info: dict, real_skills: list, job_description: str, target_role: Optional[str] = None
) -> str:
    """Constructs a single flowing paragraph career objective from only
    real profile data — no AI call, pure deterministic templating — used
    when there's no stored summary AND the real Claude call itself
    failed. Never invents a job title, employer, or achievement the
    profile doesn't actually have.

    Follows a fixed 5-part paragraph structure (the same shape approved
    for the "Case 1" example):
      1) Opener — role + years of experience
      2) Anchor — one concrete, real fact pulled from whichever actual
         experience entry best overlaps this JD's matched skills (falls
         back to the most recent entry when nothing overlaps)
      3) JD-matched skills named explicitly, in natural prose — both
         exact matches and same-technology-family related skills (via
         phase4.py's SKILL_ALIASES, same grouping the real matching
         engine uses)
      4) An honest gap clause naming up to 2 JD-requested skills the
         candidate's own profile doesn't have — never hidden or glossed
         over — only added when there IS a real skill match to sit
         alongside it, so it reads as an honest caveat, not a standalone
         admission
      5) Closing line tying the above back to this specific role

    Each line is chosen from a small pool of equivalent phrasings (via
    _pick_index, deterministic per candidate+JD) so the paragraph reads
    as genuinely written rather than one fixed template with fields
    swapped in, while staying fully reproducible for the same pair.
    """
    years = resume_info.get("years_experience") or resume_info.get("total_experience_years")
    domain = resume_info.get("domain") or resume_info.get("core_domain")
    all_skills = [s for s in (real_skills or []) if s]

    exact_matched, related_matched = _match_skills_against_jd(all_skills, job_description)
    top_skills = (exact_matched + [s for s in related_matched if s not in exact_matched])[:5]

    years_str = None
    if years not in (None, ""):
        try:
            years_num = float(years)
            years_str = f"{years_num:.0f}+ years" if years_num == int(years_num) else f"{years_num}+ years"
        except (TypeError, ValueError):
            years_str = None

    role = target_role or _extract_role_hint(job_description)
    experience = resume_info.get("experience") or []
    jd_skills_lower = {s.lower() for s in top_skills}
    anchor_exp = _find_best_matching_experience(experience, jd_skills_lower)

    seed = f"{role}|{years_str}|{domain}|{','.join(top_skills)}|{job_description[:200]}"

    # --- 1) Opener ---
    if role and years_str:
        opener_templates = [
            f"{role} with {years_str} of experience designing and delivering {domain or 'production'} systems.",
            f"Experienced {role} with {years_str} of hands-on delivery in {domain or 'software engineering'}.",
        ]
    elif role:
        opener_templates = [f"{role} with hands-on professional experience."]
    elif years_str:
        opener_templates = [f"Technology professional with {years_str} of experience in {domain or 'software engineering'}."]
    else:
        opener_templates = ["Technology professional with hands-on delivery experience."]
    line_1 = opener_templates[_pick_index(seed + "1", len(opener_templates))]

    # --- 2) Anchor — real company + a real bullet/role, preferring a
    # bullet that itself mentions one of the matched skills ---
    line_2 = ""
    if anchor_exp:
        company = anchor_exp.get("client") or anchor_exp.get("company") or ""
        bullets = anchor_exp.get("bullets") or []
        achievement = next(
            (b for b in bullets if any(skill in str(b).lower() for skill in jd_skills_lower)),
            bullets[0] if bullets else None,
        )
        if company and achievement:
            rest = str(achievement).strip().rstrip(".")
            rest = (rest[0].lower() + rest[1:]) if rest else rest
            line_2 = f"At {company}, {rest}."
        elif company:
            exp_role = anchor_exp.get("role") or anchor_exp.get("title") or ""
            line_2 = f"At {company}, worked as {exp_role}." if exp_role else f"Brings direct experience from time spent at {company}."

    # --- 3) JD-matched skills, named explicitly in prose (bolded) ---
    line_3 = ""
    if top_skills:
        skills_str = _join_natural([f"**{s}**" for s in top_skills])
        match_templates = [
            f"Matches this role's core requirements directly — hands-on expertise in {skills_str}, gained through real production work rather than isolated projects.",
            f"Brings direct, production-level experience with {skills_str}, closely aligned with what this role requires.",
        ]
        line_3 = match_templates[_pick_index(seed + "3", len(match_templates))]
    elif all_skills:
        skills_str = _join_natural([f"**{s}**" for s in all_skills[:3]])
        line_3 = f"Brings related hands-on experience with {skills_str} applicable to this role."

    # --- 4) Honest gap clause — only named JD-requested skills the
    # candidate genuinely doesn't have, never hidden ---
    gap_clause = ""
    if top_skills:
        try:
            from phase4 import extract_skills as _extract_jd_skills
            jd_all = [s for s in _extract_jd_skills(job_description) if s]
            real_lower = {s.lower() for s in all_skills}
            missing = [s for s in jd_all if s.lower() not in real_lower][:2]
            if missing:
                verb = "falls" if len(missing) == 1 else "fall"
                gap_clause = f" {_join_natural(missing)} {verb} outside this background so far."
        except Exception:
            pass

    # --- 5) Closing ---
    closing_templates = [
        "Comfortable owning services end-to-end, from design through production support, and brings that same depth of ownership directly to this position's requirements.",
        "Approaches this opportunity with the same ownership and technical rigor demonstrated throughout that experience.",
    ]
    line_5 = closing_templates[_pick_index(seed + "5", len(closing_templates))]

    paragraph = " ".join(p for p in (line_1, line_2, (line_3 + gap_clause).strip(), line_5) if p)
    return _MARK_OPEN + paragraph + _MARK_CLOSE


def generate_tailored_resume(
    resume_info: dict, job_description: str, target_role: Optional[str] = None
) -> tuple[dict, dict, Optional[dict]]:
    """
    Calls Anthropic API to generate a structured JSON resume based on resume_info and job_description.
    `target_role` — the requirement's actual title (e.g. "Sr. Java Backend
    Developer") — is the authoritative exact designation/title for the
    Career Objective when supplied, taking priority over whatever the AI
    (or the offline fallback) would otherwise guess out of the free-text
    job_description.
    Returns (resume_json, rate_limit_headers, usage_info).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    
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
    real_summary = (
        resume_info.get("summary")
        or resume_info.get("career_objective")
        or resume_info.get("professional_summary")
        or resume_info.get("objective")
        or ""
    )
    real_skills = (
        resume_info.get("skills")
        or resume_info.get("tech_stack", {}).get("expert", [])
        or resume_info.get("tech_stack", {}).get("intermediate", [])
        or []
    )
    # BUG FIX ("career objective missing" — reported for a candidate whose
    # DB profile has no stored summary field): when there's no summary to
    # pass through AND the code has reached this fallback (meaning the
    # real AI call — which would otherwise generate a genuinely tailored
    # objective — failed or is unavailable), real_summary used to just
    # stay "" and the Career Objective section vanished entirely (the
    # frontend only renders it when non-empty). Build a plain, factual
    # sentence from data that IS real (years of experience, actual
    # skills) instead of leaving it blank — still never invents a title,
    # employer, or achievement, consistent with this fallback's existing
    # no-fabrication rule.
    #
    # BUG FIX ("every new requirement should generate a new career
    # objective — this one is identical to the base resume, and would
    # stay identical for every other job too"): when resume_info DOES
    # have a stored summary, this used to pass it through completely
    # unchanged regardless of which job requirement generation was
    # running for — so Base Resume and Generated Resume showed the exact
    # same text, and regenerating for a totally different JD produced
    # that same frozen text again. The stored summary is kept as the
    # primary content (it's real, specific, and already states years of
    # experience), but a short JD-relevance line now gets appended
    # naming whichever of the candidate's real skills this specific JD
    # actually asks for — so the objective genuinely varies per
    # requirement instead of being static, without inventing anything
    # the stored summary or profile doesn't already support.
    if not real_summary:
        real_summary = _build_factual_career_objective(resume_info, real_skills, job_description, target_role)
    else:
        # Bold whichever of the candidate's real skills are both in their
        # stored summary AND in this JD, so the summary visually reads as
        # tailored to this requirement (matches the bold-keyword style of
        # a hand-tailored resume) without changing a single word of it.
        jd_lower = (job_description or "").lower()
        overlapping_in_summary = [s for s in real_skills if s and s.lower() in jd_lower]
        real_summary = _bold_terms(real_summary, overlapping_in_summary)
        addendum = _build_jd_relevance_addendum(real_skills, job_description)
        if addendum:
            real_summary = real_summary.rstrip() + " " + addendum
    # TECHNICAL PROFICIENCIES table: prefer resume_info's own categorized
    # list if it has one, otherwise build one from the full tech_stack
    # (expert + exposure/intermediate + familiar tiers merged) so the
    # table isn't limited to just the "expert" tier used for `skills`
    # above. Most stored profiles (like this one) only have a flat
    # tech_stack, not pre-categorized technical_proficiencies.
    tech_stack = resume_info.get("tech_stack") or {}
    all_tech_skills = [
        *tech_stack.get("expert", []),
        *tech_stack.get("exposure", []),
        *tech_stack.get("intermediate", []),
        *tech_stack.get("familiar", []),
    ]
    real_tech_proficiencies = (
        resume_info.get("technical_proficiencies")
        or (categorize_skills(all_tech_skills) if all_tech_skills else None)
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
    if real_tech_proficiencies:
        already_listed = set()
        for cat in real_tech_proficiencies:
            cat_skills = cat.get("skills")
            cat_list = cat_skills if isinstance(cat_skills, list) else str(cat_skills or "").split(",")
            already_listed.update(s.strip().lower() for s in cat_list if s and s.strip())

        jd_exact, jd_related = _match_skills_against_jd(real_skills, job_description)
        candidate_pool = list(dict.fromkeys([*real_skills, *all_tech_skills, *jd_exact, *jd_related]))
        missing = [s for s in candidate_pool if s and s.strip().lower() not in already_listed]
        if missing:
            real_tech_proficiencies = [
                *real_tech_proficiencies,
                {"category": "Additional Skills", "skills": missing},
            ]
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
        "generation_notes": "AI generation was unavailable — this is the candidate's stored profile data as-is, not an AI-tailored resume."
    }

    if not api_key or api_key.startswith("your_"):
        logger.warning("ANTHROPIC_API_KEY not found or is a placeholder, returning real profile data (untailored) for testing.")
        return _normalize_resume_data(mock_fallback, resume_info), {}, None

    try:
        client, working_key = get_working_anthropic_client()
        if client is None:
            logger.warning("No working Anthropic API key found (primary + backups all failed).")
            return _normalize_resume_data(mock_fallback, resume_info), {}, None
        
        target_role_line = (
            f"\nTARGET ROLE (authoritative — use this exact title verbatim in the Career Objective, per Step 1):\n{target_role}\n"
            if target_role else ""
        )
        user_prompt = f"""
CANDIDATE PROFILE (JSON):
{json.dumps(resume_info, indent=2)}
{target_role_line}
TARGET JOB DESCRIPTION:
{job_description}

Generate the tailored resume JSON now.
"""
        response = client.messages.with_raw_response.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )
        
        # Extract rate limit headers
        headers = response.headers
        rate_limits = {
            "tokens-limit": headers.get("anthropic-ratelimit-tokens-limit"),
            "tokens-remaining": headers.get("anthropic-ratelimit-tokens-remaining"),
            "tokens-reset": headers.get("anthropic-ratelimit-tokens-reset"),
            "requests-limit": headers.get("anthropic-ratelimit-requests-limit"),
            "requests-remaining": headers.get("anthropic-ratelimit-requests-remaining"),
            "requests-reset": headers.get("anthropic-ratelimit-requests-reset")
        }
        
        # Parse content
        parsed_response = response.parse()
        content = parsed_response.content[0].text
        
        # Capture real token usage for cost tracking
        usage_info = {
            "input_tokens": parsed_response.usage.input_tokens,
            "output_tokens": parsed_response.usage.output_tokens,
        }
        # Sometimes Claude returns wrapped in markdown JSON block
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
            
        result_json = json.loads(content.strip())
        return _normalize_resume_data(result_json, resume_info), rate_limits, usage_info
    except Exception as e:
        logger.error(f"Error calling Anthropic API: {e}")
        logger.error(traceback.format_exc())
        return _normalize_resume_data(mock_fallback, resume_info), {}, None


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


PARSE_REQUIREMENT_SYSTEM_PROMPT = """You are a job requirement parsing engine. You will be given the raw subject and body of an email containing a job requirement.
Extract its content using the extract_requirement tool.
If a field is not present or cannot be confidently determined, leave it as null (or an empty list for list fields) — do not guess.
"""

# P0 fix: previously this asked the model to "return only JSON" and then
# manually stripped ```json fences with string slicing before json.loads().
# That silently broke (falling all the way back to the regex-only parser in
# parser.py) any time the model added so much as a stray leading word or
# used a different fence style. Forcing a tool call with an explicit
# input_schema makes the API itself guarantee a parseable, schema-shaped
# object — the tool_use block's `.input` is already a dict, no string
# parsing at all.
PARSE_REQUIREMENT_TOOL = {
    "name": "extract_requirement",
    "description": "Record the structured fields extracted from a job requirement email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "role": {"type": ["string", "null"], "description": "The job title."},
            "client": {"type": ["string", "null"], "description": "The end client or company, if explicitly mentioned."},
            "location": {"type": ["string", "null"], "description": "City, state, or Remote/Hybrid/Onsite."},
            "rate": {"type": ["string", "null"], "description": "The pay/bill rate or compensation."},
            "duration": {"type": ["string", "null"], "description": "e.g. '6 months', 'long term'."},
            "work_mode": {
                "type": ["string", "null"],
                "enum": ["REMOTE", "HYBRID", "ONSITE", "UNKNOWN", None],
                "description": "REMOTE, HYBRID, ONSITE, or UNKNOWN."
            },
            "employment_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["C2C", "W2", "1099", "FULLTIME", "CONTRACT", "UNKNOWN"]},
                "description": "One or more of C2C, W2, 1099, FULLTIME, CONTRACT, or UNKNOWN."
            },
            "experience": {"type": ["string", "null"], "description": "e.g. '8+ years'."},
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key skills and technologies requested."
            },
        },
        "required": ["role", "client", "location", "rate", "duration", "work_mode", "employment_types", "experience", "skills"],
    },
}

def parse_requirement_text(subject: str, body: str) -> Optional[dict]:
    """
    Calls Anthropic API to parse the raw text of a job requirement email
    into a structured JSON. Returns None if parsing fails.
    """
    import hashlib
    try:
        from disk_cache import PersistentDiskCache
        _REQUIREMENT_CACHE = PersistentDiskCache("requirement_cache.json")
    except ImportError:
        _REQUIREMENT_CACHE = None

    if _REQUIREMENT_CACHE:
        content_hash = hashlib.md5(f"{subject}\\n{body}".encode("utf-8")).hexdigest()
        cached = _REQUIREMENT_CACHE.get(content_hash)
        if cached is not None:
            return cached

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key.startswith("your_"):
        logger.warning("ANTHROPIC_API_KEY not found, returning None for parse_requirement_text.")
        return None
    if _claude_circuit_is_open():
        return None

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key, timeout=15.0)
        
        user_prompt = f"SUBJECT:\\n{subject}\\n\\nBODY:\\n{body}\\n\\nExtract the requirement now."
        
        response = client.messages.with_raw_response.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=PARSE_REQUIREMENT_SYSTEM_PROMPT,
            tools=[PARSE_REQUIREMENT_TOOL],
            tool_choice={"type": "tool", "name": "extract_requirement"},
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )

        parsed_response = response.parse()
        tool_use_block = next(
            (b for b in parsed_response.content if b.type == "tool_use"), None
        )
        if tool_use_block is None:
            logger.warning("Claude API returned no tool_use block for requirement parsing.")
            return None
        result_json = tool_use_block.input

        # Ensure correct schema (also guards against a model omitting a
        # field despite it being "required" in the tool schema above)
        final_dict = {
            'role': result_json.get('role') or 'UNKNOWN',
            'client': result_json.get('client'),
            'location': result_json.get('location'),
            'rate': result_json.get('rate'),
            'duration': result_json.get('duration'),
            'work_mode': result_json.get('work_mode') or 'UNKNOWN',
            'employment_types': result_json.get('employment_types') or ['UNKNOWN'],
            'experience': result_json.get('experience'),
            'skills': result_json.get('skills') or [],
            'parsing_model': "Claude 3.5 Sonnet"
        }
        if _REQUIREMENT_CACHE:
            _REQUIREMENT_CACHE.set(content_hash, final_dict)
        return final_dict
    except Exception as e:
        if _is_hard_claude_failure(e):
            _trip_claude_circuit(f"requirement parsing: {e}")
        else:
            logger.warning(f"Error calling Claude API for requirement parsing: {e}")
        return None


ROLE_MATCH_SYSTEM_PROMPT = """You are an expert technical recruiter evaluating role match.
Given a Requirement Role and a Consultant's Role History (a list of roles they've held or preferred), 
evaluate how well the consultant's specialization matches the requirement.
Consider domains and specializations (e.g., 'DevOps Engineer' matches 'Site Reliability Engineer', but 'Java Developer' does not match 'Python Developer' just because they both have 'Developer').
Ignore seniority differences (e.g. 'Senior' or 'Lead').

Return ONLY a valid JSON object with a single field 'score', which is an integer between 0 and 100 representing the match percentage.
Example: {"score": 85}
"""

def evaluate_role_match_with_ai(requirement_role: str, consultant_roles: list[str]) -> Optional[float]:
    """
    Calls Anthropic API to evaluate role match. Returns score 0.0-100.0, or None if it fails.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key.startswith("your_"):
        return None
    if _claude_circuit_is_open():
        return None

    if not consultant_roles:
        return 50.0

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key, timeout=10.0)
        
        user_prompt = f"Requirement Role: {requirement_role}\\nConsultant Roles: {', '.join(consultant_roles)}\\nEvaluate the match."
        
        response = client.messages.with_raw_response.create(
            # BUG FIX ("Error calling Claude API for role matching: 404 -
            # model: claude-haiku-3-5"): this model ID was never valid —
            # not a deprecated/renamed model, a plain typo/placeholder
            # that 404'd on every single call, every time this ran (which
            # is every profile save's background re-matching — see
            # phase3.py's update_own_profile). The current Haiku model ID
            # requires its full date suffix (a bare "claude-haiku-4-5"
            # also 404s) — this restores the original "faster, cheaper
            # model" intent rather than falling back to Sonnet.
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=ROLE_MATCH_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )
        
        parsed_response = response.parse()
        content = parsed_response.content[0].text
        
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]

        # BUG FIX ("Error calling Claude API for role matching: Extra
        # data: line 2 column 1"): json.loads() requires the ENTIRE
        # string to be nothing but the JSON object — it fails the moment
        # anything else follows it, even a trailing newline plus a short
        # aside. Despite the system prompt saying "Return ONLY a valid
        # JSON object", claude-haiku-4-5-20251001 sometimes appends a
        # short line after the JSON anyway. JSONDecoder.raw_decode()
        # parses just the first valid JSON value starting at position 0
        # and ignores whatever comes after it, so a well-formed
        # {"score": N} followed by extra text still parses successfully
        # instead of failing outright.
        result_json, _ = json.JSONDecoder().raw_decode(content.strip())
        score = float(result_json.get("score", 50.0))
        return score
    except Exception as e:
        if _is_hard_claude_failure(e):
            _trip_claude_circuit(f"role matching: {e}")
        else:
            logger.warning(f"Error calling Claude API for role matching: {e}")
        return None