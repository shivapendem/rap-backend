import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy loaded pipeline
_qa_pipeline = None

# Reusing the alias dictionary from phase4 for exact skill matching
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
    """Word-boundary-aware check for whether `alias` genuinely appears in `text`."""
    pattern = r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])'
    return bool(re.search(pattern, text))

def extract_skills_from_text(text: str) -> list[str]:
    text_lower = text.lower()
    found_skills = set()
    for canonical, aliases in SKILL_ALIASES.items():
        for alias in aliases:
            if _alias_matches(alias, text_lower):
                found_skills.add(canonical)
                break
    return list(found_skills)

def _get_qa_pipeline():
    global _qa_pipeline
    if _qa_pipeline is None:
        try:
            from transformers import pipeline
            logger.info("Initializing Hugging Face QA pipeline for local job parsing...")
            _qa_pipeline = pipeline("question-answering", model="distilbert-base-cased-distilled-squad", device=-1)
        except ImportError:
            logger.error("transformers not installed. Local CPU parsing will fail.")
            raise
    return _qa_pipeline

def parse_requirement_local(subject: str, body: str) -> Optional[dict]:
    """
    Attempts to parse the requirement text using a local QA model and rule-based extraction.
    Returns the structured dict if highly confident, else None (triggering a fallback).
    """
    try:
        qa = _get_qa_pipeline()
    except Exception as e:
        logger.warning(f"Failed to load QA pipeline: {e}")
        return None

    context = f"Subject: {subject}\n\nBody: {body}"
    # Truncate context if it's absurdly long to prevent memory blowups (e.g. 5000 chars max)
    context = context[:5000]

    def ask(question: str, threshold: float = 0.3) -> Optional[str]:
        try:
            ans = qa(question=question, context=context)
            if ans["score"] >= threshold:
                return ans["answer"].strip()
        except Exception as e:
            logger.debug(f"QA ask failed for question '{question}': {e}")
        return None

    title = ask("What is the job title?", threshold=0.1)
    location = ask("What is the job location?", threshold=0.1)
    
    # Try to determine employment type
    emp_type = ask("Is this a contract, C2C, W2, or Full-Time job?", threshold=0.05)
    
    # Try to determine work mode
    work_mode = ask("Is this remote, hybrid, or onsite?", threshold=0.05)
    
    # Try to extract experience
    exp = ask("How many years of experience are required?", threshold=0.05)
    # Extract just digits if possible
    exp_years = None
    if exp:
        digits = re.findall(r'\d+', exp)
        if digits:
            exp_years = int(digits[0])

    # Rule-based skill extraction (highly accurate for ATS matching)
    skills = extract_skills_from_text(context)

    # Validation Gate: We MUST have a title and at least one skill to confidently bypass Claude.
    if not title or len(skills) == 0:
        logger.info("Local CPU parser failed confidence gate (missing title or skills). Falling back to Claude.")
        return None

    logger.info(f"Local CPU parser succeeded! Title: {title}, Skills: {len(skills)}")

    return {
        "role": title,
        "client": None,
        "location": location,
        "rate": None,
        "duration": None,
        "work_mode": work_mode.upper() if work_mode else None,
        "employment_types": [emp_type.upper()] if emp_type else [],
        "experience_years_required": exp_years,
        "must_have_skills": skills,
        "good_to_have_skills": []
    }
