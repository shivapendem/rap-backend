# =============================================================
# Phase 2 - Task 2 & 3: Requirement Parser + Employment Types
# Extracts structured fields from raw email text
# Merged: other-dev base + v3 fixes for role/location/client
# =============================================================

import re
from typing import Optional, List, Dict, Any, Tuple, Union

# ---------------------------------------------------------------------------
# Constants - Stop Words and Patterns
# ---------------------------------------------------------------------------

FIELD_BOUNDARIES = [
    'Client', 'Location', 'Duration', 'Rate', 'Skills', 'Experience',
    'Employment', 'Remote', 'Hybrid', 'Onsite', 'On-site', 'Contract',
    'Need', 'Looking for', 'Position', 'Opening', 'Role', 'Job Title',
    'Job Description', 'Responsibilities', 'Required Skills', 'Preferred Skills',
    'Qualifications', 'Benefits', 'About Company', 'Equal Opportunity',
    'Disclaimer', 'Vendor', 'Recruiter', 'Contact', 'Phone', 'Email',
    'Regards', 'Thanks', 'Best Regards', 'Best,', 'Warm Regards',
    'Sincerely', 'Yours', 'Thank You', 'Cheers',
    'Job Summary', 'Key Responsibilities', 'Requirements', 'Minimum Requirements',
    'Preferred Qualifications', 'Education', 'Certifications', 'Schedule',
    'Work Schedule', 'Shift', 'Hours', 'Benefits', 'Perks'
]

STOP_PATTERNS = [rf'\b{re.escape(boundary)}\b' for boundary in FIELD_BOUNDARIES]
STOP_PATTERN = re.compile('|'.join(STOP_PATTERNS), re.IGNORECASE)

EMPLOYMENT_KEYWORDS = {
    'C2C': ['c2c', 'corp to corp', 'corp-to-corp', 'corp2corp'],
    'W2': ['w2'],
    '1099': ['1099'],
    'FULLTIME': ['full time', 'full-time', 'fulltime', 'permanent', 'fte'],
    'CONTRACT': ['contract', 'contractual', 'contract-to-hire']
}

WORK_MODE_PATTERNS = {
    'REMOTE': [
        r'\b100%\s*remote\b', r'\bremote\s+opportunity\b', r'\bremote\b',
        r'\bwork\s+from\s+home\b', r'\bwfh\b'
    ],
    'HYBRID': [r'\bhybrid\s+schedule\b', r'\bhybrid\b'],
    'ONSITE': [r'\bon\s*-?\s*site\b', r'\bin\s*-?\s*person\b', r'\bon\s+location\b']
}

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

PHONE_PATTERN = re.compile(
    r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'
)

BARE_RATE_PATTERN = re.compile(
    r'\$\s*\d+(?:,\d{3})?(?:\s*[-\u2013]\s*\$?\s*\d+(?:,\d{3})?)?\s*/\s*'
    r'(?:hr|hour|day|month|year|yr)',
    re.IGNORECASE
)
# Context that means a nearby bare-rate match is portal/subscription
# boilerplate, not a real client rate — see the rate-fallback fix below.
_RATE_FALSE_POSITIVE_CONTEXT = re.compile(
    r'(?i)(hire\s+(?:our|a)\s+.{0,20}?recruiter|sign[\s\-]?up|subscri|'
    r'broadcast|recruiting\s+portal|prohires|powerhouse)'
)

# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------

US_STATE_CODES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
    'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
    'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
    'VA','WA','WV','WI','WY','DC',
}

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

# Matches "City, TX" or "City, Texas" — resolved through resolve_state_code()
BARE_LOCATION_PATTERN = re.compile(
    r'\b([A-Z][a-zA-Z]+(?:[ \-][A-Z][a-zA-Z]+){0,2}),\s*'
    r'([A-Z]{2}\b|[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)'
)

# Street-level prefixes that precede a real city in addresses like
# "Drive, Plano, Texas" or "1 West Street, Mineola NY"
_STREET_SUFFIXES = re.compile(
    r'(?i)(?:^\d+\s+)?[A-Za-z]+\s+'
    r'(?:Drive|Dr|Street|St|Ave|Avenue|Blvd|Boulevard|Rd|Road|'
    r'Lane|Ln|Way|Pkwy|Parkway|Suite|Ste|Court|Ct|Place|Pl)\s*,\s*'
)

_SIGNOFF_WORDS = {
    'regards', 'thanks', 'thank', 'sincerely', 'best',
    'cheers', 'warm', 'yours', 'respectfully'
}


def resolve_state_code(token: str) -> Optional[str]:
    """Return a 2-letter state code for 'TX' or 'Texas', else None."""
    if not token:
        return None
    token = token.strip()
    if token.upper() in US_STATE_CODES:
        return token.upper()
    return US_STATE_NAMES.get(token.lower())


def find_city_state(text: str, reject_first_words=None) -> Optional[str]:
    """
    Find the first valid "City, ST" / "City, State Name" pair.
    Uses a sliding-window search so that a failed match (e.g. "Drive, Plano")
    doesn't consume "Plano" before it can be tried as a city candidate.
    Rejects pairs whose city starts with a sign-off word (e.g. "Regards, VA").
    Also strips leading street-level address tokens before searching.
    """
    if not text:
        return None
    reject_first_words = reject_first_words or set()

    # Strip leading street prefix so "Drive, Plano, Texas" → "Plano, Texas"
    text = _STREET_SUFFIXES.sub('', text, count=1).strip()

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


# ---------------------------------------------------------------------------
# Text normalisation helpers
# ---------------------------------------------------------------------------

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

# Un-glue field labels that HTML-collapse has fused onto a preceding word,
# e.g. "TrintechLocation:" or "ArchitectDuration:". Uses longest-label-first
# ordering so multi-word labels ("work location") beat single-word prefixes.
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


def normalize_text(text: str) -> str:
    """Fold fancy punctuation to ASCII and un-glue HTML-collapsed field labels."""
    if not text:
        return ''
    for bad, good in _PUNCT_MAP.items():
        text = text.replace(bad, good)
    # Un-glue labels AFTER punct-fold so en-dash variants are already '-'
    text = _GLUED_LABEL_PATTERN.sub(' ', text)
    return text


def crop_at_next_field(value: str) -> str:
    """
    Trim a captured field value at the first next-field label or sign-off.
    Also trims at a closing paren immediately followed by a new sentence
    (common in single-line HTML-collapsed emails).
    """
    if not value:
        return value
    cut = len(value)
    m = NEXT_FIELD_PATTERN.search(value)
    if m:
        cut = min(cut, m.start())
    m = SIGNATURE_PATTERN.search(value)
    if m:
        cut = min(cut, m.start())
    # Stop at ")(CapitalWord" boundary — parenthetical ends, new sentence starts
    m = re.search(r'\)\s*(?=[A-Z][a-z])', value)
    if m:
        cut = min(cut, m.start() + 1)
    return value[:cut].strip()


def role_from_subject(subject: str) -> Optional[str]:
    """
    Best-effort job title extracted from a subject line (fallback only).
    Strips recruiter noise: reply prefixes, parentheticals, pipe/slash
    separators, city suffixes, rate tokens, and marketing keywords.
    """
    if not subject:
        return None
    s = normalize_text(subject)
    # Strip reply/forward prefixes
    s = re.sub(r'(?i)^\s*(re|fw|fwd)\s*:\s*', '', s).strip()
    # If an explicit label is present, use its value
    m = re.search(r'(?i)\b(?:job\s*title|job\s*role|position|role|opening)\s*[:\-]\s*(.+)', s)
    if m:
        s = m.group(1)
    s = crop_at_next_field(s)
    # Drop ALL parentheticals: "(Local to VA)", "(USC AND H4 Only)", "(Onsite)"
    s = re.sub(r'\s*\([^)]*\)', '', s).strip()
    # Split on pipe || or double-slash // bulk separators
    s = re.split(r'\s*(?:\|\|+|//+)\s*', s)[0]
    # Drop city/state suffix after a bare dash: "Architect -Chicago, IL / Remote"
    s = re.sub(r'\s*-\s*[A-Z][a-zA-Z].*$', '', s).strip()
    # Drop location prepositions
    s = re.split(r'(?i)\s+(?:in|near|@)\s+', s)[0]
    # Drop rate tokens
    s = re.sub(r'\$\s*\d.*$', '', s)
    # Drop trailing slash-separated noise: "//Local to X"
    s = re.sub(r'(?i)[/\\]+\s*\w.*$', '', s).strip()
    # Strip leading "Requirement for / Opening for" prefix
    s = re.sub(r'(?i)^\s*(?:requirement|req|opening|posting)\s+for\s+', '', s).strip()
    # Drop marketing keywords only at START — a mid-string match like
    # "Hiring!! Financial Data Analyst" would wipe the whole role with .*$
    s = re.sub(
        r'(?i)^\s*(?:needed|required|urgent|immediate|hiring(?:\s+now)?|hot|hire|'
        r'opportunity|apply|local)\b[\s:\-!.]*', '', s
    ).strip()
    # Drop leading punctuation left behind after stripping
    s = re.sub(r'^[\s!?.,:;\-]+', '', s)
    s = sanitize_text(s)
    if not s:
        return None
    return s if len(s) <= 80 else s[:77] + '...'


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def is_email_body(text: str) -> bool:
    """Return True if text looks like a full email body rather than a field value."""
    if not text:
        return False
    sentences = re.split(r'[.!?]\s+', text)
    if len(sentences) > 2 and len(text) > 100:
        return True
    email_patterns = [
        r'job\s+description', r'responsibilities', r'qualifications',
        r'benefits', r'about\s+company', r'thank\s+you', r'best\s+regards'
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
    """Return True when enough job-requirement indicators are found."""
    if not text:
        return False
    indicators = [
        r'\bjob\s+title\b', r'\bposition\b', r'\bopening\b', r'\brequirement\b',
        r'\bclient\b', r'\blocation\b', r'\brate\b', r'\bduration\b',
        r'\bcontract\b', r'\bskills\b', r'\bexperience\b',
        r'\$\d+', r'\bC2C\b', r'\bW2\b', r'\b1099\b',
        r'\bremote\b', r'\bonsite\b', r'\bon-site\b', r'\bhybrid\b', r'\byears?\b'
    ]
    indicators_found = sum(
        1 for i in indicators if re.search(i, text, re.IGNORECASE)
    )
    return indicators_found >= 2


def safe_extract_value(text: str, max_length: int = 200) -> Optional[str]:
    """Safely extract a field value, stopping at boundaries."""
    if not text:
        return None
    if is_email_body(text):
        return None
    match = STOP_PATTERN.search(text)
    if match:
        text = text[:match.start()]
    if '\n' in text:
        parts = text.split('\n')
        if len(parts[0]) < 80 and len(parts) > 1:
            text = parts[0]
    text = text.strip()
    text = ' '.join(text.split())
    if len(text) > max_length:
        text = text[:max_length].rsplit(' ', 1)[0] + '...'
    return text if text else None


def extract_field_value(text: str, patterns: List[str]) -> Optional[str]:
    """Extract field using patterns with safe stopping."""
    if not text:
        return None
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1).strip()
            lines = value.split('\n')
            if lines:
                first_line = lines[0].strip()
                if first_line and not is_email_body(first_line):
                    cleaned = safe_extract_value(first_line)
                    if cleaned:
                        return cleaned
                    if len(first_line) < 100:
                        return first_line
    return None


def parse_field_with_fallback(
    text: str,
    patterns: List[str],
    fallback_patterns: Optional[List[str]] = None,
    default: Optional[str] = None
) -> Optional[str]:
    """Parse a field with multiple pattern attempts and fallbacks."""
    if not text:
        return default
    result = extract_field_value(text, patterns)
    if result:
        return result
    if fallback_patterns:
        result = extract_field_value(text, fallback_patterns)
        if result:
            return result
    return default


# ---------------------------------------------------------------------------
# Main Extraction Functions
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


# Negation words immediately before a keyword mean it is being excluded —
# e.g. "No C2C", "Not accepting W2", "Non-C2C".
_NEGATION_BEFORE = re.compile(
    r'\b(?:no|not|without|excluding|except|non)\b[\s\-]*$', re.IGNORECASE
)


def extract_employment_types(text: str) -> List[str]:
    """
    Extract employment types with negation awareness.
    'No C2C' / 'Non-C2C' / 'Not W2' are correctly excluded.
    Uses word boundaries to avoid false matches inside longer tokens.
    """
    if not text:
        return ["UNKNOWN"]
    text_lower = normalize_text(text).lower()
    found_types = []
    for emp_type, keywords in EMPLOYMENT_KEYWORDS.items():
        matched = False
        for keyword in keywords:
            for m in re.finditer(rf'\b{re.escape(keyword)}\b', text_lower):
                window = text_lower[max(0, m.start() - 20):m.start()]
                if _NEGATION_BEFORE.search(window):
                    continue        # negated — skip this occurrence
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
            year_match = re.search(r'\d+\+?\s*(?:-\s*\d+\s*)?years?', exp, re.IGNORECASE)
            if year_match:
                value = year_match.group(0)
                value = re.sub(r'(?i)\byrs?\.?\b', 'years', value)
                return value
            return exp
    number_match = re.search(
        r'(\d+\+?)\s*[-\u2013]?\s*(?:\d+\+?\s*)?years?', text, re.IGNORECASE
    )
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
            stop_match = STOP_PATTERN.search(skills_text)
            if stop_match:
                skills_text = skills_text[:stop_match.start()].strip()
            break
    if not skills_text:
        return []
    parts = re.split(r',|;|\||/|\n|\band\b', skills_text)
    skills = []
    for skill in parts:
        skill = skill.strip()
        if not skill:
            continue
        skill = re.sub(r'(?i)\b(with|experience|knowledge|required|preferred)\b.*', '', skill)
        skill = re.sub(r'\s+', ' ', skill).strip()
        if 2 < len(skill) < 40:
            skill = skill.rstrip('.')
            skills.append(skill)
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
    if not vendor_name or not vendor_email:
        from_header = headers.get('from', '') if headers else ''
        if from_header:
            if not vendor_email:
                email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+', from_header)
                if email_match:
                    vendor_email = email_match.group(0).lower()
            if not vendor_name:
                name_match = re.match(r'^([^<]+)<', from_header)
                if name_match:
                    vendor_name = name_match.group(1).strip().strip('"\'')
                    if len(vendor_name) > 30:
                        vendor_name = vendor_name.split(',')[0].strip()
                elif '@' not in from_header:
                    vendor_name = from_header.strip().strip('"\'').split(',')[0].strip()
    phone = None
    if body:
        phone_match = PHONE_PATTERN.search(body)
        if phone_match:
            phone = phone_match.group(0).strip()
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
    if parsed.get('role') and parsed['role'] != 'UNKNOWN':
        if valid_fields >= 1:
            return min(round(valid_fields / len(important_fields), 2), 1.0)
    return 0.0


# ---------------------------------------------------------------------------
# Cleaning Functions
# ---------------------------------------------------------------------------

def clean_role(role: Optional[str]) -> Optional[str]:
    """
    Clean role title.
    Strips: trailing parentheticals, leading marketing junk, trailing punctuation.
    """
    if not role:
        return None
    role = sanitize_text(normalize_text(role))
    if not role:
        return None
    role = crop_at_next_field(role)
    # Drop trailing parenthetical asides: "(Onsite Role)", "(USC & H4 Only)"
    for _ in range(3):
        stripped = re.sub(r'\s*\([^)]*\)\s*$', '', role).strip()
        if stripped == role:
            break
        role = stripped
    # Drop leading marketing words: "Hiring!!", "Urgent -", "!!"
    role = re.sub(
        r'(?i)^\s*(?:hiring(?:\s*now)?|urgent|immediate|hot|new|open(?:ing)?|apply)'
        r'\b[\s:\-!.]*', '', role
    )
    role = re.sub(r'^[^0-9A-Za-z]+', '', role).strip()
    role = re.sub(r'[\-\u2013,:;]+\s*$', '', role).strip()
    if len(role) > 60:
        role = role[:57] + '...'
    return role or None


def clean_client(client: Optional[str]) -> Optional[str]:
    """
    Clean client name.
    Strips only LEADING filler words so "Center for Medicare Services" is kept intact.
    """
    if not client:
        return None
    client = sanitize_text(normalize_text(client))
    if not client:
        return None
    client = crop_at_next_field(client)
    client = re.sub(r'(?i)^\s*(?:is|the|our|a|for|at|with)\s+', '', client).strip()
    if len(client) > 50:
        client = client[:47] + '...'
    return client or None


_LOCATION_PREFERENCE_PATTERN = re.compile(
    r'(?i)\b(?:preference|preferred|ideally|nice\s+to\s+have|'
    r'in\s+or\s+near|candidates?\s+(?:in|near|located|based))\b'
)


def clean_location(location: Optional[str]) -> Optional[str]:
    """
    Clean location value.
    Priority: real City/State pair > Remote/Hybrid/Onsite keyword.
    Strips parentheticals before keyword checks so "(Hybrid)" or "(Onsite)"
    inside a labeled location don't shadow the real city.
    Uses find_city_state() for full-state-name support and sliding-window
    matching to handle street prefixes and multi-city strings.
    """
    if not location:
        return None
    location = sanitize_text(normalize_text(location))
    if not location:
        return None
    location = crop_at_next_field(location)
    # Strip parentheticals BEFORE keyword checks:
    # "Philadelphia, PA (Hybrid - Local)" → "Philadelphia, PA"
    # "Atlanta, GA (3 Days Onsite)"       → "Atlanta, GA"
    location_no_paren = re.sub(r'\s*\([^)]*\)', '', location).strip()
    # Try to find a real City/State pair first (includes full state name support)
    city_state = find_city_state(location_no_paren)
    if city_state:
        # Edge case: a genuinely Remote/Hybrid role that only names a city as a
        # soft geographic *preference* — e.g. "Remote (U.S.) — preference for
        # candidates in or near Minneapolis, MN". City-first priority would
        # wrongly promote that preference city to the primary location, making a
        # remote role look onsite. When a Remote/Hybrid keyword is stated BEFORE
        # a preference phrase and the city follows it, keep the work mode as the
        # primary location. Normal strings like "Remote or Dallas, TX" or
        # "Austin, TX (Hybrid)" have no preference phrase, so they're unaffected.
        low = location_no_paren.lower()
        pref_m = _LOCATION_PREFERENCE_PATTERN.search(location_no_paren)
        if pref_m:
            mode = 'Remote' if 'remote' in low else 'Hybrid' if 'hybrid' in low else None
            if mode:
                mode_idx = low.find(mode.lower())
                city_idx = low.find(city_state.split(',')[0].lower())
                if mode_idx != -1 and mode_idx < pref_m.start() and (city_idx == -1 or city_idx >= pref_m.start()):
                    return f"{mode} (pref: {city_state})"
        return city_state
    # Keyword fallbacks — only reached when no city was found
    low = location_no_paren.lower()
    if 'remote' in low:
        return 'Remote'
    if 'hybrid' in low:
        return 'Hybrid'
    if 'onsite' in low or 'on-site' in low or 'on site' in low:
        return 'Onsite'
    if len(location) > 50:
        location = location[:47] + '...'
    return location or None


def clean_rate(rate: Optional[str]) -> Optional[str]:
    """Clean rate — handles range ($55-65/hr), single ($65/hr), annual ($120k)."""
    if not rate:
        return None
    rate = sanitize_text(normalize_text(rate))
    if not rate:
        return None
    rate = crop_at_next_field(rate)
    # Range: $55-65/hr or $55-$65/hr
    m = re.search(
        r'(USD\s*)?\$?\s*(\d+(?:,\d{3})?)\s*[-\u2013]\s*\$?\s*(\d+(?:,\d{3})?)'
        r'\s*/\s*(hr|hour|day|month|year|yr)',
        rate, re.IGNORECASE
    )
    if m:
        cur = m.group(1) or ''
        return f"{cur}${m.group(2)}-${m.group(3)}/{m.group(4)}".strip()
    # Single: $65/hr
    m = re.search(
        r'(USD\s*)?\$?\s*(\d+(?:,\d{3})?)\s*/\s*(hr|hour|day|month|year|yr)',
        rate, re.IGNORECASE
    )
    if m:
        cur = m.group(1) or ''
        return f"{cur}${m.group(2)}/{m.group(3)}".strip()
    # Annual / flat: $120k
    m = re.search(r'(USD\s*)?\$?\s*(\d+(?:,\d{3})?)\s*k?\b', rate, re.IGNORECASE)
    if m:
        cur = m.group(1) or ''
        return f"{cur}${m.group(2)}".strip()
    return re.split(r'\s+', rate)[0] or None


def clean_duration(duration: Optional[str]) -> Optional[str]:
    """Clean duration — extracts the numeric/keyword portion only."""
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
    Main parser function — extracts structured data from job requirement emails.
    This function signature must remain unchanged for backend compatibility.
    """
    safe_subject = subject or ''
    safe_body = body or ''
    safe_headers = headers if isinstance(headers, dict) else {}

    # Normalize BEFORE the is_job_requirement_email gate so that HTML-collapsed
    # labels (e.g. "LeadLocation:") get un-glued and register as indicators.
    full_text = normalize_text(f"{safe_subject}\n{safe_body}")
    norm_body = normalize_text(safe_body)

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

    # ── Role ──────────────────────────────────────────────────────────────
    # Body-first to prevent subject-line poisoning.
    raw_role = first_match(ROLE_PATTERNS, norm_body) or first_match(ROLE_PATTERNS, full_text)
    role = clean_role(raw_role) or clean_role(role_from_subject(safe_subject))
    if not role or is_email_body(role):
        role = 'UNKNOWN'

    # ── Client ────────────────────────────────────────────────────────────
    raw_client = first_match(CLIENT_PATTERNS, norm_body) or first_match(CLIENT_PATTERNS, full_text)
    client = clean_client(raw_client)
    if client and is_email_body(client):
        client = None
    # Infer client from "Role Title – ClientName" dash pattern when no label found
    if not client:
        dash_m = re.search(
            r'(?i)(?:role|position|opening|title)\s*[:\-]\s*[^\n]+?'
            r'[\-\u2013]\s*([A-Z][A-Za-z0-9&\s]{2,30}?)(?=\s*(?:Location|Client|Rate|\n|$))',
            norm_body
        )
        if not dash_m:
            dash_m = re.search(
                r'[A-Za-z ]{4,}\s+[\-\u2013]\s+([A-Z][A-Za-z0-9&\s]{2,30}?)'
                r'(?=\s*(?:Location|Client|Rate|\n|$))',
                norm_body
            )
        if dash_m:
            cand = clean_client(dash_m.group(1))
            if cand and len(cand) <= 40 and not is_email_body(cand):
                client = cand

    # ── Location ──────────────────────────────────────────────────────────
    raw_location = (
        first_match(LOCATION_PATTERNS, norm_body)
        or first_match(LOCATION_PATTERNS, full_text)
    )
    location = clean_location(raw_location)
    if location and is_email_body(location):
        location = None
    if not location:
        # Bare City/State fallback — reject sign-off lines like "Regards, VA"
        location = find_city_state(norm_body, reject_first_words=_SIGNOFF_WORDS)

    # ── Rate ──────────────────────────────────────────────────────────────
    raw_rate = first_match(RATE_PATTERNS, norm_body) or first_match(RATE_PATTERNS, full_text)
    rate = clean_rate(raw_rate)
    if rate and is_email_body(rate):
        rate = None
    if not rate:
        # BUG FIX: BARE_RATE_PATTERN used to grab the FIRST bare $NNN/period
        # string anywhere in the email with zero context check. Recruiter
        # broadcast templates (ProHires and similar) often end with a
        # subscription ad like "Hire our IT Recruiter at just $499/month" —
        # that ad was being picked up as the requirement's rate on every
        # single email from that template, since real rates are frequently
        # unlabeled in these bodies and this fallback ran unconditionally.
        # Now: walk every bare-rate match in order and skip any whose
        # surrounding text looks like portal/subscription boilerplate
        # rather than an actual client rate.
        for bare_match in BARE_RATE_PATTERN.finditer(full_text):
            window_start = max(0, bare_match.start() - 60)
            window_end = min(len(full_text), bare_match.end() + 60)
            context_window = full_text[window_start:window_end]
            if _RATE_FALSE_POSITIVE_CONTEXT.search(context_window):
                continue
            rate = bare_match.group(0).strip()
            break

    # ── Duration ──────────────────────────────────────────────────────────
    raw_duration = (
        first_match(DURATION_PATTERNS, norm_body)
        or first_match(DURATION_PATTERNS, full_text)
    )
    duration = clean_duration(raw_duration)
    if duration and is_email_body(duration):
        duration = None

    # ── Other fields ──────────────────────────────────────────────────────
    work_mode = extract_work_mode(full_text)
    employment_types = extract_employment_types(full_text)
    experience = extract_experience(full_text)
    skills = extract_skills(full_text)

    # ── Vendor info ───────────────────────────────────────────────────────
    vendor_name = None
    vendor_email = None
    from_header = safe_headers.get('from', '')
    if from_header:
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+', from_header)
        if email_match:
            vendor_email = email_match.group(0).lower()
        name_match = re.match(r'^([^<]+)<', from_header)
        if name_match:
            vendor_name = name_match.group(1).strip().strip('"\'')
            if len(vendor_name) > 30:
                vendor_name = vendor_name.split(',')[0].strip()
        elif '@' not in from_header:
            vendor_name = from_header.strip().strip('"\'').split(',')[0].strip()

    vendor_contact = extract_vendor_contact(
        safe_headers, safe_body, vendor_name, vendor_email
    )

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

    parsed['parse_confidence'] = calculate_confidence(parsed)
    parsed['is_likely_requirement'] = parsed['parse_confidence'] >= 0.3

    # Final guard — never return email body content in any field
    for key, value in parsed.items():
        if isinstance(value, str) and value and is_email_body(value):
            parsed[key] = None

    return parsed