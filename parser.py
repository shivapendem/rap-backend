# =============================================================
# Phase 2 - Task 2 & 3: Requirement Parser + Employment Types
# Extracts structured fields from raw email text
# =============================================================

import re
from typing import Optional, List, Dict, Any, Tuple, Union

# ---------------------------------------------------------------------------
# Constants - Stop Words and Patterns
# ---------------------------------------------------------------------------

# Labels that indicate field boundaries (stop extraction at these)
FIELD_BOUNDARIES = [
    # Common field labels
    'Client', 'Location', 'Duration', 'Rate', 'Skills', 'Experience',
    'Employment', 'Remote', 'Hybrid', 'Onsite', 'On-site', 'Contract',
    'Need', 'Looking for', 'Position', 'Opening', 'Role', 'Job Title',
    'Job Description', 'Responsibilities', 'Required Skills', 'Preferred Skills',
    'Qualifications', 'Benefits', 'About Company', 'Equal Opportunity',
    'Disclaimer', 'Vendor', 'Recruiter', 'Contact', 'Phone', 'Email',
    # Email signatures
    'Regards', 'Thanks', 'Best Regards', 'Best,', 'Warm Regards',
    'Sincerely', 'Yours', 'Thank You', 'Cheers',
    # Other common sections
    'Job Summary', 'Key Responsibilities', 'Requirements', 'Minimum Requirements',
    'Preferred Qualifications', 'Education', 'Certifications', 'Schedule',
    'Work Schedule', 'Shift', 'Hours', 'Benefits', 'Perks'
]

# Patterns to stop extraction (case insensitive)
STOP_PATTERNS = [rf'\b{re.escape(boundary)}\b' for boundary in FIELD_BOUNDARIES]

# Combined stop pattern
STOP_PATTERN = re.compile('|'.join(STOP_PATTERNS), re.IGNORECASE)

# Employment type keywords
EMPLOYMENT_KEYWORDS = {
    'C2C': ['c2c', 'corp to corp', 'corp-to-corp', 'corp2corp'],
    'W2': ['w2'],
    '1099': ['1099'],
    'FULLTIME': ['full time', 'full-time', 'fulltime', 'permanent', 'fte'],
    'CONTRACT': ['contract', 'contractual', 'contract-to-hire']
}

# Work mode patterns
WORK_MODE_PATTERNS = {
    'REMOTE': [
        r'\b100%\s*remote\b',
        r'\bremote\s+opportunity\b',
        r'\bremote\b',
        r'\bwork\s+from\s+home\b',
        r'\bwfh\b'
    ],
    'HYBRID': [
        r'\bhybrid\s+schedule\b',
        r'\bhybrid\b'
    ],
    'ONSITE': [
        r'\bon\s*-?\s*site\b',
        r'\bin\s*-?\s*person\b',
        r'\bon\s+location\b'
    ]
}

# Regex patterns for field extraction
ROLE_PATTERNS = [
    r'(?i)\bjob\s*title\s*[:\-]\s*(.+)',
    r'(?i)\bjob\s*role\s*[:\-]\s*(.+)',
    r'(?i)\bposition\s*[:\-]\s*(.+)',
    r'(?i)\brole\s*[:\-]\s*(.+)',
    r'(?i)\bopening\s*[:\-]\s*(.+)',
    r'(?i)\brequirement\s*[:\-]\s*(.+)',
]

CLIENT_PATTERNS = [
    r'(?i)\bend\s*client\s*[:\-]\s*(.+)',
    r'(?i)\bclient\s*[:\-]\s*(.+)',
    r'(?i)\bcustomer\s*[:\-]\s*(.+)',
]

LOCATION_PATTERNS = [
    r'(?i)\bwork\s*location\s*[:\-]\s*(.+)',
    r'(?i)\bplace\s*of\s*work\s*[:\-]\s*(.+)',
    r'(?i)\blocation\s*[:\-]\s*(.+)',
]

RATE_PATTERNS = [
    r'(?i)\bpay\s*rate\s*[:\-]\s*(.+)',
    r'(?i)\bbill\s*rate\s*[:\-]\s*(.+)',
    r'(?i)\bcompensation\s*[:\-]\s*(.+)',
    r'(?i)\brate\s*[:\-]\s*(.+)',
]

DURATION_PATTERNS = [
    r'(?i)\bcontract\s*length\s*[:\-]\s*(.+)',
    r'(?i)\bcontract\s*duration\s*[:\-]\s*(.+)',
    r'(?i)\bduration\s*[:\-]\s*(.+)',
]

SKILLS_PATTERNS = [
    r'(?i)primary\s*skills?\s*[:\-]\s*(.+)',
    r'(?i)required\s*skills?\s*[:\-]\s*(.+)',
    r'(?i)technical\s*skills?\s*[:\-]\s*(.+)',
    r'(?i)key\s*skills?\s*[:\-]\s*(.+)',
    r'(?i)skills?\s*[:\-]\s*(.+)',
    r'(?i)skill\s*set\s*[:\-]\s*(.+)',
    r'(?i)tech(?:nology|nical)?\s*stack\s*[:\-]\s*(.+)',
]

EXPERIENCE_PATTERNS = [
    r'(?i)(\d+\+?\s*(?:-\s*\d+\s*)?years?\s*(?:of\s*)?experience)',
    r'(?i)experience\s*[:\-]\s*(\d+\+?\s*(?:-\s*\d+\s*)?years?)',
    r'(?i)(\d+\+?\s*yrs?\.?\s*(?:of\s*)?exp(?:erience)?)',
    r'(?i)minimum\s*(?:of\s*)?(\d+\+?\s*years?)',
    r'(?i)(\d+\s*-\s*\d+\s*years?)'
]

PHONE_PATTERN = re.compile(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')

# Fallback: bare rate pattern like "$55/hr" or "$55-65/hr" with no "Rate:" label
BARE_RATE_PATTERN = re.compile(r'\$\s*\d+(?:,\d{3})?(?:\s*[-–]\s*\$?\s*\d+(?:,\d{3})?)?\s*/\s*(?:hr|hour|day|month|year|yr)', re.IGNORECASE)

# Fallback: bare "City, ST" location pattern
US_STATE_CODES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
    'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
    'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
    'VA','WA','WV','WI','WY','DC',
}

# Full names -> codes. Real recruiter emails write "Plano, Texas" as often as
# "Plano, TX" — the bare-location fallback needs to catch both.
US_STATE_NAMES = {
    'alabama':'AL','alaska':'AK','arizona':'AZ','arkansas':'AR','california':'CA',
    'colorado':'CO','connecticut':'CT','delaware':'DE','florida':'FL','georgia':'GA',
    'hawaii':'HI','idaho':'ID','illinois':'IL','indiana':'IN','iowa':'IA',
    'kansas':'KS','kentucky':'KY','louisiana':'LA','maine':'ME','maryland':'MD',
    'massachusetts':'MA','michigan':'MI','minnesota':'MN','mississippi':'MS',
    'missouri':'MO','montana':'MT','nebraska':'NE','nevada':'NV',
    'new hampshire':'NH','new jersey':'NJ','new mexico':'NM','new york':'NY',
    'north carolina':'NC','north dakota':'ND','ohio':'OH','oklahoma':'OK',
    'oregon':'OR','pennsylvania':'PA','rhode island':'RI','south carolina':'SC',
    'south dakota':'SD','tennessee':'TN','texas':'TX','utah':'UT','vermont':'VT',
    'virginia':'VA','washington':'WA','west virginia':'WV','wisconsin':'WI',
    'wyoming':'WY','district of columbia':'DC',
}

# Stricter bare "City, ST" or "City, State Name" — 1-3 capitalized words then
# a real state code OR a full state name (both resolved through
# resolve_state_code() below).
BARE_LOCATION_PATTERN = re.compile(
    r'\b([A-Z][a-zA-Z]+(?:[ \-][A-Z][a-zA-Z]+){0,2}),\s*'
    r'([A-Z]{2}\b|[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)'
)

def resolve_state_code(token):
    """Return a 2-letter state code for either 'TX' or 'Texas', else None."""
    if not token:
        return None
    token = token.strip()
    if token.upper() in US_STATE_CODES:
        return token.upper()
    return US_STATE_NAMES.get(token.lower())


def find_city_state(text, reject_first_words=None):
    """
    Find the first "City, ST"/"City, State Name" pair with a REAL state,
    trying every possible starting point — not just non-overlapping matches.
    BUG this fixes: re.finditer() is non-overlapping, so on input like
    "Corridor, Charlotte, North Carolina", it tries "Corridor, Charlotte"
    first (not a real state -> rejected), then resumes scanning AFTER
    "Charlotte" — meaning "Charlotte, North Carolina" (the actual valid
    pair) never gets a chance to match, because "Charlotte" was already
    consumed by the rejected attempt. Sliding the search start forward by
    just 1 character (instead of past the whole failed match) lets
    "Charlotte" be retried as its own candidate.
    """
    if not text:
        return None
    reject_first_words = reject_first_words or set()
    pos = 0
    while pos < len(text):
        m = BARE_LOCATION_PATTERN.search(text, pos)
        if not m:
            return None
        code = resolve_state_code(m.group(2))
        first_word = m.group(1).split()[0].lower()
        if code and first_word not in reject_first_words:
            return f"{m.group(1)}, {code}"
        pos = m.start() + 1
    return None

_SIGNOFF_WORDS = {'regards', 'thanks', 'thank', 'sincerely', 'best',
                  'cheers', 'warm', 'yours', 'respectfully'}

# Fold "fancy" punctuation to ASCII so [:\-] label patterns match en/em dashes,
# full-width colons, NBSP, smart quotes, and CRLF newlines.
_PUNCT_MAP = {
    '\u2013': '-', '\u2014': '-', '\u2012': '-', '\u2212': '-',
    '\uFF1A': ':', '\u00A0': ' ',
    '\u201C': '"', '\u201D': '"', '\u2018': "'", '\u2019': "'",
    '\r\n': '\n', '\r': '\n',
}

NEXT_FIELD_LABELS = [
    'job title', 'job role', 'position', 'role', 'opening', 'requirement',
    'end client', 'client', 'customer', 'work location', 'place of work',
    'location', 'pay rate', 'bill rate', 'compensation', 'rate',
    'contract length', 'contract duration', 'duration', 'primary skills',
    'required skills', 'technical skills', 'key skills', 'skill set', 'skills',
    'experience', 'employment type', 'employment', 'work mode', 'vendor',
    'recruiter', 'contact', 'phone', 'email', 'responsibilities',
    'qualifications', 'job description', 'benefits',
]
NEXT_FIELD_PATTERN = re.compile(
    r'\b(?:' + '|'.join(re.escape(w) for w in NEXT_FIELD_LABELS) + r')\s*[:\-]',
    re.IGNORECASE,
)

# Recruiter emails converted from HTML sometimes lose the whitespace between
# adjacent fields entirely — e.g. "...Governance Lead" + "Location :" comes
# through as "...Governance LeadLocation :". crop_at_next_field()'s \b can't
# find "Location" glued onto "Lead" (no word boundary between two word
# chars), so extraction never stops there and the role/location value runs
# straight into the next field's text. Re-insert a space wherever a known
# field label is directly glued onto the preceding character with zero
# separation, before any extraction pattern ever sees the text.
_FIELD_LABEL_ALTERNATION = '|'.join(
    re.escape(w) for w in sorted(NEXT_FIELD_LABELS, key=len, reverse=True)
)
_GLUED_LABEL_PATTERN = re.compile(
    r'(?<=[A-Za-z0-9])(?=(?:' + _FIELD_LABEL_ALTERNATION + r')\s*[:\-])',
    re.IGNORECASE,
)

SIGNATURE_PATTERN = re.compile(
    r'\b(?:regards|thanks|thank you|best regards|warm regards|sincerely|'
    r'cheers|best,)\b',
    re.IGNORECASE,
)

def normalize_text(text):
    if not text:
        return ''
    for bad, good in _PUNCT_MAP.items():
        text = text.replace(bad, good)
    text = _GLUED_LABEL_PATTERN.sub(' ', text)
    return text

def crop_at_next_field(value):
    """Cut a captured value at the next field label or sign-off on the line."""
    if not value:
        return value
    cut = len(value)
    m = NEXT_FIELD_PATTERN.search(value)
    if m:
        cut = min(cut, m.start())
    m = SIGNATURE_PATTERN.search(value)
    if m:
        cut = min(cut, m.start())
    return value[:cut].strip()

def role_from_subject(subject):
    """Best-effort job title from a subject line (fallback only)."""
    if not subject:
        return None
    s = normalize_text(subject)
    s = re.sub(r'(?i)^\s*(re|fw|fwd)\s*:\s*', '', s).strip()
    m = re.search(r'(?i)\b(?:job\s*title|job\s*role|position|role|opening)\s*[:\-]\s*(.+)', s)
    if m:
        s = m.group(1)
    s = crop_at_next_field(s)
    s = re.split(r'(?i)\s+(?:in|at|for|near|@)\s+', s)[0]
    s = re.split(r'\s+-\s+', s)[0]
    s = re.sub(r'\$\s*\d.*$', '', s)
    s = re.sub(r'(?i)\b(needed|required|urgent|immediate|hiring|opportunity)\b', '', s)
    # Recruiter subjects routinely wrap noise in parens: "(Local to VA)",
    # "(USC AND H4 Only)", "(W2 Only)" — none of that belongs in a role title,
    # so drop every parenthetical group rather than just the label match above.
    s = re.sub(r'\([^)]*\)', '', s)
    # Leading junk punctuation left behind once "Hiring" etc. is stripped out
    # of the middle of the string (e.g. "Hiring!! X" -> "!! X").
    s = re.sub(r'^[\s!?.,:;\-]+', '', s)
    s = sanitize_text(s)
    if not s:
        return None
    return s if len(s) <= 80 else s[:77] + '...' 

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def is_email_body(text: str) -> bool:
    """
    Check if text looks like email content (long paragraphs).
    Returns True if text appears to be email body content.
    """
    if not text:
        return False
    
    # If text has multiple sentences/paragraphs, it's likely email body
    sentences = re.split(r'[.!?]\s+', text)
    if len(sentences) > 2 and len(text) > 100:
        return True
    
    # Check for common email patterns
    email_patterns = [
        r'job\s+description',
        r'responsibilities',
        r'qualifications',
        r'benefits',
        r'about\s+company',
        r'thank\s+you',
        r'best\s+regards'
    ]
    
    text_lower = text.lower()
    for pattern in email_patterns:
        if re.search(pattern, text_lower):
            return True
    
    return False


def sanitize_text(text: Optional[str]) -> Optional[str]:
    """Remove pipes, tabs, multiple spaces, and normalize."""
    if not text:
        return None
    
    text = text.replace('|', ' ')
    text = text.replace('\t', ' ')
    text = ' '.join(text.split())
    return text.strip()


def clean_whitespace(text: Optional[str]) -> Optional[str]:
    """Clean whitespace from text."""
    if not text:
        return None
    return ' '.join(text.split())


def is_job_requirement_email(text: str) -> bool:
    if not text:
        return False
    
    indicators = [
        r'\bjob\s+title\b', r'\bposition\b', r'\bopening\b', r'\brequirement\b',
        r'\bclient\b', r'\blocation\b', r'\brate\b', r'\bduration\b',
        r'\bcontract\b', r'\bskills\b', r'\bexperience\b',
        # New: catches shorthand/terse postings like "$55/hr", "C2C", "Onsite"
        r'\$\d+', r'\bC2C\b', r'\bW2\b', r'\b1099\b',
        r'\bremote\b', r'\bonsite\b', r'\bon-site\b', r'\bhybrid\b',
        r'\byears?\b'
    ]
    
    indicators_found = 0
    for indicator in indicators:
        if re.search(indicator, text, re.IGNORECASE):
            indicators_found += 1
    
    return indicators_found >= 2


def safe_extract_value(text: str, max_length: int = 200) -> Optional[str]:
    """
    Safely extract a field value by stopping at boundaries.
    Returns None if text is invalid or looks like email body.
    """
    if not text:
        return None
    
    # Check if this is email body content
    if is_email_body(text):
        return None
    
    # Stop at first boundary or newline
    match = STOP_PATTERN.search(text)
    if match:
        text = text[:match.start()]
    
    # Stop at newline if it's after a short value
    if '\n' in text:
        parts = text.split('\n')
        # If first line is short and there are more lines, use first line
        if len(parts[0]) < 80 and len(parts) > 1:
            text = parts[0]
    
    # Clean up
    text = text.strip()
    text = ' '.join(text.split())  # Remove extra spaces
    
    # If text is too long, truncate but preserve complete words
    if len(text) > max_length:
        text = text[:max_length].rsplit(' ', 1)[0] + '...'
    
    return text if text else None


def extract_field_value(text: str, patterns: List[str]) -> Optional[str]:
    """
    Extract field using patterns with safe stopping.
    Returns only the extracted value, not everything after.
    """
    if not text:
        return None
    
    for pattern in patterns:
        # Use non-greedy matching and stop at boundaries
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1).strip()
            
            # Safely extract just the value
            lines = value.split('\n')
            if lines:
                first_line = lines[0].strip()
                
                # Check if first line is reasonable
                if first_line and not is_email_body(first_line):
                    # Try to extract just the field value using safe extraction
                    cleaned = safe_extract_value(first_line)
                    if cleaned:
                        return cleaned
                    
                    # If safe extraction returned None, return first line if it's short
                    if len(first_line) < 100:
                        return first_line
    
    return None


def parse_field_with_fallback(
    text: str,
    patterns: List[str],
    fallback_patterns: Optional[List[str]] = None,
    default: Optional[str] = None
) -> Optional[str]:
    """
    Parse a field with multiple pattern attempts and fallbacks.
    """
    if not text:
        return default
    
    # Try primary patterns
    result = extract_field_value(text, patterns)
    if result:
        return result
    
    # Try fallback patterns if provided
    if fallback_patterns:
        result = extract_field_value(text, fallback_patterns)
        if result:
            return result
    
    return default


# ---------------------------------------------------------------------------
# Main Extraction Functions (Required for backward compatibility)
# ---------------------------------------------------------------------------

def first_match(patterns: List[str], text: str) -> Optional[str]:
    """
    Returns the first regex capture group found from a list of patterns.
    Works safely on multiline emails and stops at the next field label.
    This function signature must remain unchanged for backend compatibility.
    """
    if not text:
        return None

    text = normalize_text(text)

    if not is_job_requirement_email(text):
        return None

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            # '.' does not cross newlines, so the capture is already one line;
            # take the first line defensively, collapse whitespace, then crop
            # at the next field label / sign-off on that line.
            first_line = match.group(1).split('\n', 1)[0]
            value = sanitize_text(first_line)
            if not value:
                continue
            value = crop_at_next_field(value)
            if not value or is_email_body(value) or len(value) > 200:
                continue
            return value

    return None


def extract_work_mode(text: str) -> str:
    """Extract work mode from text."""
    if not text:
        return "UNKNOWN"
    
    text_lower = text.lower()
    for mode, patterns in WORK_MODE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                return mode
    return "UNKNOWN"


# Negation words that, when found immediately before a keyword match,
# mean the keyword is being EXCLUDED, not offered — e.g. "No C2C",
# "Not accepting W2", "Without 1099", "Non-C2C".
_NEGATION_BEFORE = re.compile(
    r'\b(?:no|not|without|excluding|except|non)\b[\s\-]*$', re.IGNORECASE
)


def extract_employment_types(text: str) -> List[str]:
    """Extract employment types from text."""
    if not text:
        return ["UNKNOWN"]

    text_lower = normalize_text(text).lower()
    found_types = []

    for emp_type, keywords in EMPLOYMENT_KEYWORDS.items():
        matched = False
        for keyword in keywords:
            for m in re.finditer(rf'\b{re.escape(keyword)}\b', text_lower):
                # Look at the text immediately before this occurrence for a
                # negation cue. If negated, keep scanning — a LATER,
                # non-negated mention of the same keyword should still count.
                window = text_lower[max(0, m.start() - 20):m.start()]
                if _NEGATION_BEFORE.search(window):
                    continue
                matched = True
                break
            if matched:
                break
        if matched:
            found_types.append(emp_type)

    return found_types if found_types else ["UNKNOWN"]


def extract_experience(text: str) -> Optional[str]:
    """Extract experience requirement from text."""
    if not text:
        return None
    
    for pattern in EXPERIENCE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            exp = match.group(1).strip()
            # Clean up to just the years part
            year_match = re.search(r'\d+\+?\s*(?:-\s*\d+\s*)?years?', exp, re.IGNORECASE)
            if year_match:
                value = year_match.group(0)
                value = re.sub(r'(?i)\byrs?\.?\b', 'years', value)
                return value
            return exp
    
    # Check for just numbers with years
    number_match = re.search(r'(\d+\+?)\s*[-–]?\s*(?:\d+\+?\s*)?years?', text, re.IGNORECASE)
    if number_match:
        return f"{number_match.group(1)} years"
    
    return None


def extract_skills(text: str) -> List[str]:
    """Extract skills from text as a list."""
    if not text:
        return []
    
    skills_text = None
    for pattern in SKILLS_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            skills_text = match.group(1).strip()
            # Stop at boundaries
            stop_match = STOP_PATTERN.search(skills_text)
            if stop_match:
                skills_text = skills_text[:stop_match.start()].strip()
            break
    
    if not skills_text:
        return []
    
    # Split on common separators
    parts = re.split(r',|;|\||/|\n|\band\b', skills_text)
    skills = []
    
    for skill in parts:
        skill = skill.strip()
        if not skill:
            continue
        
        # Remove descriptions
        skill = re.sub(r'(?i)\b(with|experience|knowledge|required|preferred)\b.*', '', skill)
        skill = re.sub(r'\s+', ' ', skill).strip()
        
        # Limit skill length
        if 2 < len(skill) < 40:
            # Remove periods and extra spaces
            skill = skill.rstrip('.')
            skills.append(skill)
    
    # Deduplicate and limit
    return list(dict.fromkeys(skills))[:10]


def extract_vendor_contact(
    headers: Dict[str, str],
    body: str,
    vendor_name: Optional[str] = None,
    vendor_email: Optional[str] = None
) -> Optional[str]:
    """
    Extract vendor contact string from headers and body.
    This function signature must remain unchanged for backend compatibility.
    """
    if not headers and not body:
        return None
    
    # Extract from headers if not provided
    if not vendor_name or not vendor_email:
        from_header = headers.get('from', '') if headers else ''
        
        if from_header:
            # Extract email
            if not vendor_email:
                email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+', from_header)
                if email_match:
                    vendor_email = email_match.group(0).lower()
            
            # Extract name
            if not vendor_name:
                name_match = re.match(r'^([^<]+)<', from_header)
                if name_match:
                    vendor_name = name_match.group(1).strip().strip('"\'')
                    # Take first part if too long
                    if len(vendor_name) > 30:
                        vendor_name = vendor_name.split(',')[0].strip()
                elif '@' not in from_header:
                    vendor_name = from_header.strip().strip('"\'').split(',')[0].strip()
    
    # Extract phone from body
    phone = None
    if body:
        phone_match = PHONE_PATTERN.search(body)
        if phone_match:
            phone = phone_match.group(0).strip()
    
    # Build vendor contact string
    contact_parts = []
    if vendor_name:
        contact_parts.append(vendor_name)
    if vendor_email:
        contact_parts.append(vendor_email)
    if phone:
        contact_parts.append(phone)
    
    return ' | '.join(contact_parts) if contact_parts else None


def calculate_confidence(parsed: Dict[str, Any]) -> float:
    """
    Calculate confidence based on extracted fields.
    This function signature must remain unchanged for backend compatibility.
    """
    if not parsed:
        return 0.0
    
    important_fields = ['client', 'location', 'rate', 'employment_types', 'role']
    valid_fields = 0
    
    for field in important_fields:
        value = parsed.get(field)
        if field == 'employment_types':
            if value and value != ['UNKNOWN']:
                valid_fields += 1
        else:
            if value and value != 'UNKNOWN' and not is_email_body(str(value)):
                valid_fields += 1
    
    # Require at least role and one other field
    if parsed.get('role') and parsed['role'] != 'UNKNOWN':
        if valid_fields >= 1:
            return min(round(valid_fields / len(important_fields), 2), 1.0)
    
    return 0.0


# ---------------------------------------------------------------------------
# Cleaning Functions
# ---------------------------------------------------------------------------

def clean_role(role: Optional[str]) -> Optional[str]:
    """Clean role title."""
    if not role:
        return None
    role = sanitize_text(normalize_text(role))
    if not role:
        return None
    role = crop_at_next_field(role)
    role = re.sub(r'[\-\u2013,:;]+\s*$', '', role).strip()
    if len(role) > 60:
        role = role[:57] + '...'
    return role or None


def clean_client(client: Optional[str]) -> Optional[str]:
    """Clean client name."""
    if not client:
        return None
    client = sanitize_text(normalize_text(client))
    if not client:
        return None
    client = crop_at_next_field(client)
    # Strip only LEADING filler words; never delete the rest of the value.
    client = re.sub(r'(?i)^\s*(?:is|the|our|a|for|at|with)\s+', '', client).strip()
    if len(client) > 50:
        client = client[:47] + '...'
    return client or None


def clean_location(location: Optional[str]) -> Optional[str]:
    """Clean location."""
    if not location:
        return None
    location = sanitize_text(normalize_text(location))
    if not location:
        return None
    location = crop_at_next_field(location)
    low = location.lower()

    # Prefer a concrete city/state over a bare mode word — e.g.
    # "Charlotte, North Carolina (Hybrid)" should resolve to "Charlotte, NC",
    # not just "Hybrid" (which loses the city). Work mode is captured
    # separately by extract_work_mode(), so this field can stay geographic.
    city_state = find_city_state(location)
    if city_state:
        return city_state

    if 'remote' in low:
        return 'Remote'
    if 'hybrid' in low:
        return 'Hybrid'

    m = re.search(r'([A-Za-z][A-Za-z\s]+,\s*[A-Za-z]{2,})', location)
    if m:
        return m.group(1).strip()
    if 'onsite' in low or 'on-site' in low or 'on site' in low:
        return 'Onsite'
    if len(location) > 50:
        location = location[:47] + '...'
    return location or None


def clean_rate(rate: Optional[str]) -> Optional[str]:
    """Clean rate."""
    if not rate:
        return None
    rate = sanitize_text(normalize_text(rate))
    if not rate:
        return None
    rate = crop_at_next_field(rate)

    # Range: $55-65/hr
    m = re.search(r'(USD\s*)?\$?\s*(\d+(?:,\d{3})?)\s*[-\u2013]\s*\$?\s*(\d+(?:,\d{3})?)\s*/\s*(hr|hour|day|month|year|yr)', rate, re.IGNORECASE)
    if m:
        cur = m.group(1) or ''
        return f"{cur}${m.group(2)}-${m.group(3)}/{m.group(4)}".strip()
    # Single: $65/hr
    m = re.search(r'(USD\s*)?\$?\s*(\d+(?:,\d{3})?)\s*/\s*(hr|hour|day|month|year|yr)', rate, re.IGNORECASE)
    if m:
        cur = m.group(1) or ''
        return f"{cur}${m.group(2)}/{m.group(3)}".strip()
    # Annual-ish: $120k or 120000
    m = re.search(r'(USD\s*)?\$?\s*(\d+(?:,\d{3})?)\s*k?\b', rate, re.IGNORECASE)
    if m:
        cur = m.group(1) or ''
        return f"{cur}${m.group(2)}".strip()

    rate = re.split(r'\s+', rate)[0]
    return rate or None


def clean_duration(duration: Optional[str]) -> Optional[str]:
    """Clean duration."""
    if not duration:
        return None
    duration = sanitize_text(normalize_text(duration))
    if not duration:
        return None
    duration = crop_at_next_field(duration)

    duration_patterns = [
        r'(\d+)\s*[-\u2013]\s*(\d+)\s*(months?|weeks?)',
        r'(\d+)\s*(months?|weeks?|days?)',
        r'(long\s*term)',
        r'(contract\s*to\s*hire|contract)',
        r'(full\s*time|permanent)',
    ]
    for pattern in duration_patterns:
        m = re.search(pattern, duration, re.IGNORECASE)
        if m:
            return m.group(0).strip()

    if len(duration) > 30:
        duration = duration[:27] + '...'
    return duration or None


# ---------------------------------------------------------------------------
# Main Parser Function
# ---------------------------------------------------------------------------

def parse_requirement(
    subject: str,
    body: str,
    headers: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Main parser function - extracts structured data from job requirement emails.
    This function signature must remain unchanged for backend compatibility.
    """
    safe_subject = subject or ''
    safe_body = body or ''
    safe_headers = headers if isinstance(headers, dict) else {}

    # Normalize BEFORE the is_job_requirement_email gate — glued field
    # labels (e.g. "LeadLocation", "USDuration") don't contain a \b word
    # boundary for indicators like \blocation\b/\bduration\b to match, so
    # a legitimate requirement email with this HTML-collapse artifact was
    # scoring too few indicators and getting discarded entirely, before
    # extraction ever ran. Normalizing here (which un-glues labels) fixes
    # that at the source; extraction below reuses this same normalized
    # text instead of re-deriving it, so nothing downstream regresses.
    full_text = normalize_text(f"{safe_subject}\n{safe_body}")
    norm_body = normalize_text(safe_body)

    # Check if this is a job requirement
    if not is_job_requirement_email(full_text):
        return {
            'role': 'UNKNOWN',
            'client': None,
            'location': None,
            'rate': None,
            'duration': None,
            'work_mode': 'UNKNOWN',
            'employment_types': ['UNKNOWN'],
            'vendor_email': None,
            'vendor': None,
            'vendor_contact': None,
            'experience': None,
            'skills': [],
            'parse_confidence': 0.0,
            'is_likely_requirement': False
        }
    
    # Extract fields.
    # Prefer a labeled value in the BODY; only fall back to the subject when the
    # body has no labeled value. This kills subject-line poisoning, where a value
    # in the subject (e.g. "Location: Remote") overrides the real body value.
    raw_role = first_match(ROLE_PATTERNS, norm_body) or first_match(ROLE_PATTERNS, full_text)
    role = clean_role(raw_role) or clean_role(role_from_subject(safe_subject))
    if not role or is_email_body(role):
        role = 'UNKNOWN'

    raw_client = first_match(CLIENT_PATTERNS, norm_body) or first_match(CLIENT_PATTERNS, full_text)
    client = clean_client(raw_client)
    if client and is_email_body(client):
        client = None

    raw_location = first_match(LOCATION_PATTERNS, norm_body) or first_match(LOCATION_PATTERNS, full_text)
    location = clean_location(raw_location)
    if location and is_email_body(location):
        location = None
    if not location:
        # Bare "City, ST" / "City, State Name" fallback — require a REAL
        # state and reject sign-off lines like "Best Regards, VA ...".
        location = find_city_state(norm_body, reject_first_words=_SIGNOFF_WORDS)

    raw_rate = first_match(RATE_PATTERNS, norm_body) or first_match(RATE_PATTERNS, full_text)
    rate = clean_rate(raw_rate)
    if rate and is_email_body(rate):
        rate = None
    if not rate:
        bare_match = BARE_RATE_PATTERN.search(full_text)
        if bare_match:
            rate = bare_match.group(0).strip()

    raw_duration = first_match(DURATION_PATTERNS, norm_body) or first_match(DURATION_PATTERNS, full_text)
    duration = clean_duration(raw_duration)
    if duration and is_email_body(duration):
        duration = None
    
    work_mode = extract_work_mode(full_text)
    employment_types = extract_employment_types(full_text)
    experience = extract_experience(full_text)
    skills = extract_skills(full_text)
    
    # Extract vendor info
    vendor_name = None
    vendor_email = None
    
    from_header = safe_headers.get('from', '')
    if from_header:
        # Extract email
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+', from_header)
        if email_match:
            vendor_email = email_match.group(0).lower()
        
        # Extract name
        name_match = re.match(r'^([^<]+)<', from_header)
        if name_match:
            vendor_name = name_match.group(1).strip().strip('"\'')
            # Take first part if too long
            if len(vendor_name) > 30:
                vendor_name = vendor_name.split(',')[0].strip()
        elif '@' not in from_header:
            vendor_name = from_header.strip().strip('"\'').split(',')[0].strip()
    
    vendor_contact = extract_vendor_contact(safe_headers, safe_body, vendor_name, vendor_email)
    
    # Build parsed result
    parsed = {
        'role': role,
        'client': client,
        'location': location,
        'rate': rate,
        'duration': duration,
        'work_mode': work_mode,
        'employment_types': employment_types,
        'vendor_email': vendor_email,
        'vendor': vendor_name,
        'vendor_contact': vendor_contact,
        'experience': experience,
        'skills': skills,
    }
    
    # Calculate confidence
    parsed['parse_confidence'] = calculate_confidence(parsed)
    parsed['is_likely_requirement'] = parsed['parse_confidence'] >= 0.3
    
    # Ensure we never return email body content
    for key, value in parsed.items():
        if isinstance(value, str) and value and is_email_body(value):
            parsed[key] = None
    
    return parsed