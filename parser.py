# =============================================================
# Phase 2 - Task 2 & 3: Requirement Parser + Employment Types
# Extracts structured fields from raw email text
# =============================================================

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Role patterns
# ---------------------------------------------------------------------------
ROLE_PATTERNS = [
    r'(?i)job\s*title\s*[:\-]\s*(.+)',
    r'(?i)job\s*role\s*[:\-]\s*(.+)',
    r'(?i)position\s*[:\-]\s*(.+)',
    r'(?i)role\s*[:\-]\s*(.+)',
    r'(?i)opening\s*[:\-]\s*(.+)',
    r'(?i)requirement\s*[:\-]\s*(.+)',
]

# ---------------------------------------------------------------------------
# Client patterns
# ---------------------------------------------------------------------------
CLIENT_PATTERNS = [
    r'(?i)end\s*client\s*[:\-]\s*(.+)',
    r'(?i)client\s*[:\-]\s*(.+)',
    r'(?i)customer\s*[:\-]\s*(.+)',
]

# ---------------------------------------------------------------------------
# Location patterns
# ---------------------------------------------------------------------------
LOCATION_PATTERNS = [
    r'(?i)location\s*[:\-]\s*(.+)',
    r'(?i)work\s*location\s*[:\-]\s*(.+)',
    r'(?i)place\s*of\s*work\s*[:\-]\s*(.+)',
]

# ---------------------------------------------------------------------------
# Rate patterns
# ---------------------------------------------------------------------------
RATE_PATTERNS = [
    r'(?i)rate\s*[:\-]\s*(.+)',
    r'(?i)pay\s*rate\s*[:\-]\s*(.+)',
    r'(?i)bill\s*rate\s*[:\-]\s*(.+)',
    r'(?i)compensation\s*[:\-]\s*(.+)',
]

# ---------------------------------------------------------------------------
# Duration patterns
# ---------------------------------------------------------------------------
DURATION_PATTERNS = [
    r'(?i)duration\s*[:\-]\s*(.+)',
    r'(?i)contract\s*length\s*[:\-]\s*(.+)',
    r'(?i)contract\s*duration\s*[:\-]\s*(.+)',
]

# ---------------------------------------------------------------------------
# Experience patterns
# ---------------------------------------------------------------------------
EXPERIENCE_PATTERNS = [
    r'(?i)experience\s*[:\-]\s*(.+)',
    r'(?i)exp\s*[:\-]\s*(.+)',
    r'(?i)(\d+\+?\s*(?:to|-)?\s*\d*\+?\s*(?:years?|yrs?)\s*(?:of)?\s*experience)',
]

# ---------------------------------------------------------------------------
# Skills patterns
# ---------------------------------------------------------------------------
SKILL_LABEL_PATTERNS = [
    r'(?i)(?:primary|required|mandatory|technical|key)?\s*skills?\s*[:\-]\s*(.+)',
    r'(?i)tech\s*stack\s*[:\-]\s*(.+)',
    r'(?i)technologies\s*[:\-]\s*(.+)',
]

# ---------------------------------------------------------------------------
# Work mode patterns
# ---------------------------------------------------------------------------
WORK_MODE_PATTERNS = {
    "REMOTE": [r'(?i)\bremote\b', r'(?i)work\s*from\s*home', r'(?i)\bwfh\b'],
    "ONSITE": [r'(?i)\bon.?site\b', r'(?i)\bin.?person\b', r'(?i)\bon\s*location\b'],
    "HYBRID": [r'(?i)\bhybrid\b'],
}


def first_match(patterns: list, text: str) -> Optional[str]:
    """Return first regex match from list of patterns."""
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            value = m.group(1).strip()
            # Remove trailing noise
            value = re.split(r'[\n\r|]', value)[0].strip()
            return value if value else None
    return None


def extract_work_mode(text: str) -> str:
    """Extract work mode from text."""
    for mode, patterns in WORK_MODE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text):
                return mode
    return "UNKNOWN"


def extract_employment_types(text: str) -> list:
    """
    Task 3: Extract employment types from text.
    Returns list of: C2C, W2, FULLTIME, UNKNOWN
    """
    text_lower = (text or "").lower()
    result = []

    if any(x in text_lower for x in ["c2c", "corp to corp", "corp-to-corp", "corp2corp"]):
        result.append("C2C")

    if "w2" in text_lower:
        result.append("W2")

    if any(x in text_lower for x in ["full time", "full-time", "fulltime", "permanent", "fte"]):
        result.append("FULLTIME")

    # Remove duplicates while preserving order
    result = list(dict.fromkeys(result))

    return result if result else ["UNKNOWN"]


def extract_experience(text: str) -> Optional[str]:
    """Extract required experience."""
    for pattern in EXPERIENCE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            value = m.group(1).strip()
            value = re.split(r'[\n\r|]', value)[0].strip()
            return value
    return None


def extract_skills(text: str) -> Optional[str]:
    """Extract required skills."""
    for pattern in SKILL_LABEL_PATTERNS:
        m = re.search(pattern, text)
        if m:
            value = m.group(1).strip()
            value = re.split(r'[\n\r]', value)[0].strip()
            return value

    return None

def extract_vendor_email(headers: dict, body: str) -> Optional[str]:
    """Extract vendor email from Reply-To header or body."""
    # Prefer Reply-To header
    reply_to = headers.get("reply_to", "")
    if reply_to:
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+', reply_to)
        if email_match:
            return email_match.group(0).lower()

    # Fall back to From header
    from_header = headers.get("from", "")
    if from_header:
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+', from_header)
        if email_match:
            return email_match.group(0).lower()

    return None


def calculate_confidence(parsed: dict) -> float:
    """
    Calculate parser confidence score (0.0 to 1.0)
    based on how many fields were extracted.
    """
    important_fields = ["role", "client", "location", "rate", "employment_types"]
    extracted = sum(1 for f in important_fields if parsed.get(f) and parsed[f] != ["UNKNOWN"])
    return round(extracted / len(important_fields), 2)


def parse_requirement(
    subject: str,
    body: str,
    headers: dict,
) -> dict:
    """
    Task 2: Main parser function.
    Extracts all structured fields from email.
    Returns dict with all parsed fields.
    """
    full_text = f"{subject}\n{body}"

    # Extract role (fallback to subject line)
    role = first_match(ROLE_PATTERNS, full_text) or subject or "UNKNOWN"

    # Extract other fields
    client = first_match(CLIENT_PATTERNS, full_text)
    location = first_match(LOCATION_PATTERNS, full_text)
    rate = first_match(RATE_PATTERNS, full_text)
    duration = first_match(DURATION_PATTERNS, full_text)
    experience = extract_experience(full_text)
    skills = extract_skills(full_text)
    work_mode = extract_work_mode(full_text)
    employment_types = extract_employment_types(full_text)
    vendor_email = extract_vendor_email(headers, body)

    # Extract vendor name from From header
    from_header = headers.get("from", "")
    vendor_name = None
    if from_header:
        name_match = re.match(r'^([^<]+)<', from_header)
        if name_match:
            vendor_name = name_match.group(1).strip().strip('"')

    parsed = {
    "role": role,
    "client": client,
    "location": location,
    "rate": rate,
    "duration": duration,
    "experience": experience,
    "skills": skills,
    "work_mode": work_mode,
    "employment_types": employment_types,
    "vendor_email": vendor_email,
    "vendor": vendor_name,
}
    
    # Calculate confidence score
    confidence = calculate_confidence(parsed)
    parsed["parse_confidence"] = confidence

    return parsed
