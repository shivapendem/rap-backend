import os
import json
import re
from typing import Optional
from anthropic import Anthropic
import logging
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

2. DYNAMIC TARGET ROLE ALIGNMENT:
   - Identify the target job title from the Job Description (e.g. "DevOps Engineer", "Applications Developer", "Game Programmer", "Full-Stack Developer").
   - Align the Candidate's Career Objective to start explicitly with this Target Role Title.
   - Strategically align experience role titles (`role`) so they reflect the target domain while keeping authentic company names and dates.

3. DETAILED CONTEXT & HIGH-IMPACT BULLETS:
   - Generate detailed bullet points following: [Action Verb] + [Specific Tech Stack / Frameworks] + [Business / System Context] + [Quantified Impact Metric].
   - Include realistic high-impact metrics (e.g. "10K+ suppliers", "$5M revenue", "sub-25ms latency", "40% database load reduction", "60% deployment reduction", "99.9% uptime").

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
  "career_objective": "string (Rich tailored objective with **bolded keywords**)",
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
    if "education" not in normalized and resume_info.get("education"):
        normalized["education"] = resume_info["education"]
    if "other_projects" in resume_data and resume_data["other_projects"]:
        normalized["other_projects"] = resume_data["other_projects"]
    if isinstance(resume_data.get("personal_details"), dict) and resume_data["personal_details"]:
        normalized["personal_details"] = resume_data["personal_details"]
    if resume_data.get("declaration"):
        normalized["declaration"] = resume_data["declaration"]

    normalized["generation_notes"] = _clean_str(resume_data.get("generation_notes"))
    return normalized


def generate_tailored_resume(resume_info: dict, job_description: str) -> tuple[dict, dict, Optional[dict]]:
    """
    Calls Anthropic API to generate a structured JSON resume based on resume_info and job_description.
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
        "missing_skills": [],
        "experience": [
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
        "education": real_education,
        "certifications": real_certifications,
        "personal_details": resume_info.get("personal_details") or {},
        "generation_notes": "AI generation was unavailable — this is the candidate's stored profile data as-is, not an AI-tailored resume."
    }

    if not api_key or api_key.startswith("your_"):
        logger.warning("ANTHROPIC_API_KEY not found or is a placeholder, returning real profile data (untailored) for testing.")
        return _normalize_resume_data(mock_fallback, resume_info), {}, None

    try:
        client = Anthropic(api_key=api_key)
        
        user_prompt = f"""
CANDIDATE PROFILE (JSON):
{json.dumps(resume_info, indent=2)}

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
        logger.warning(f"Error calling Claude API: {e}. Falling back to mock data.")
        return _normalize_resume_data(mock_fallback, resume_info), {}, None