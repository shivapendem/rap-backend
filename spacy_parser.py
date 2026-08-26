import logging
from typing import Optional
import subprocess
import sys

logger = logging.getLogger(__name__)

# Lazy loaded spacy resources
_nlp = None
_matcher = None

# A basic list of IT skills to build the PhraseMatcher
SKILLS = [
    "python", "java", "javascript", "typescript", "c#", "go", "golang", 
    "react", "angular", "vue", "next.js", "node.js", "fastapi", "django", 
    "flask", "spring boot", "postgresql", "mysql", "oracle", "mongodb", 
    "redis", "elasticsearch", "aws", "azure", "gcp", "docker", "kubernetes", 
    "terraform", "ci/cd", "rest api", "graphql", "microservices", 
    "machine learning", "sql", "kafka", "spark", "airflow", "tailwind", 
    "redux", "sap", "salesforce", "servicenow", "linux", "ansible", "jenkins"
]

def _ensure_spacy_model():
    global _nlp, _matcher
    if _nlp is not None:
        return _nlp, _matcher
        
    try:
        import spacy
        from spacy.matcher import PhraseMatcher
    except ImportError:
        logger.warning("spacy is not installed. SpaCy NLP parser will fail.")
        raise
        
    try:
        _nlp = spacy.load("en_core_web_sm")
    except OSError:
        logger.info("Downloading spacy en_core_web_sm model...")
        subprocess.check_call([sys.executable, "-m", "spacy", "download", "en_core_web_sm"])
        _nlp = spacy.load("en_core_web_sm")
        
    _matcher = PhraseMatcher(_nlp.vocab, attr="LOWER")
    patterns = [_nlp.make_doc(text) for text in SKILLS]
    _matcher.add("SKILLS", patterns)
    
    return _nlp, _matcher

def parse_requirement_spacy(subject: str, body: str) -> Optional[dict]:
    """
    Uses SpaCy Named Entity Recognition (NER) to extract entities as a fallback.
    Returns a partially populated dictionary containing ORG (client), GPE (location),
    and extracted skills.
    """
    try:
        nlp, matcher = _ensure_spacy_model()
    except Exception as e:
        logger.warning(f"Failed to load SpaCy model: {e}")
        return None

    full_text = f"{subject}\n\n{body}"
    
    # Process text through NLP pipeline
    doc = nlp(full_text)
    
    # Extract Location (GPE: Geo-Political Entity)
    locations = [ent.text for ent in doc.ents if ent.label_ == "GPE"]
    location = locations[0] if locations else None
    
    # Extract Client (ORG: Organization)
    orgs = [ent.text for ent in doc.ents if ent.label_ == "ORG"]
    # Filter out common false positives
    orgs = [org for org in orgs if not org.lower().startswith("equal opportunity")]
    client = orgs[0] if orgs else None
    
    # Extract Skills using PhraseMatcher
    matches = matcher(doc)
    extracted_skills = set()
    for match_id, start, end in matches:
        span = doc[start:end]
        extracted_skills.add(span.text.lower())
        
    # Validation gate: If it couldn't find ANY location, client, or skills, it's not a confident parse
    if not location and not client and not extracted_skills:
        logger.info("SpaCy parser failed confidence gate (no entities or skills found).")
        return None
        
    return {
        "role": None, # Let the regex fallback handle the role if it's missing
        "client": client,
        "location": location,
        "rate": None,
        "duration": None,
        "work_mode": "UNKNOWN",
        "employment_types": ["UNKNOWN"],
        "experience": None,
        "skills": list(extracted_skills),
        "parsing_model": "SpaCy NLP"
    }
