# =============================================================
# Phase 2 - Task 2 & 3: Requirement Parser + Employment Types
# Extracts structured fields from raw email text
# Merged: other-dev base + v3 fixes for role/location/client
# =============================================================

import re
import html
from typing import Optional, List, Dict, Any, Tuple, Union


# ─── Claude requirement-parsing (merged in from claude_parsing_service.py) ──
# This was previously a separate file, split out from claude_service.py so
# resume-generation edits couldn't accidentally break parsing. Per request,
# merged directly into parser.py instead — this is the only file that calls
# it, so there's no real benefit to it living elsewhere. The shared circuit
# breaker still lives in claude_service.py (imported below) so one bad/
# expired/out-of-credit API key still trips a single breaker across
# parsing, resume generation, AND role matching — not an independent one
# just for this file.

PARSE_REQUIREMENT_SYSTEM_PROMPT = """You are a job requirement parsing engine. You will be given the raw subject and body of an email containing a job requirement.
Extract its content using the extract_requirement tool.
If a field is not present or cannot be confidently determined, leave it as null (or an empty list for list fields) — do not guess.
"""

# P0 fix: previously this asked the model to "return only JSON" and then
# manually stripped ```json fences with string slicing before json.loads().
# That silently broke (falling all the way back to the regex-only parser
# below) any time the model added so much as a stray leading word or used
# a different fence style. Forcing a tool call with an explicit
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
    import os
    import hashlib
    import logging
    logger = logging.getLogger(__name__)

    try:
        from disk_cache import PersistentDiskCache
        _REQUIREMENT_CACHE = PersistentDiskCache("requirement_cache.json")
    except ImportError:
        _REQUIREMENT_CACHE = None

    if _REQUIREMENT_CACHE:
        content_hash = hashlib.md5(f"{subject}\n{body}".encode("utf-8")).hexdigest()
        cached = _REQUIREMENT_CACHE.get(content_hash)
        if cached is not None:
            return cached

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key.startswith("your_"):
        logger.warning("ANTHROPIC_API_KEY not found, returning None for parse_requirement_text.")
        return None

    from claude_service import _claude_circuit_is_open, _trip_claude_circuit, _is_hard_claude_failure
    if _claude_circuit_is_open():
        return None

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key, timeout=15.0)

        user_prompt = f"SUBJECT:\n{subject}\n\nBODY:\n{body}\n\nExtract the requirement now."

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
# ─── end merged-in Claude parsing ────────────────────────────────────────

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

# Field boundaries should only stop extraction when they're genuinely acting
# as a label (start of a line, optionally followed by a colon) — not when
# they appear naturally mid-sentence, e.g. "5+ years of experience with SQL"
# was being incorrectly cut at "experience" even though it wasn't a real
# "Experience:" section label.
STOP_PATTERNS = [rf'(?:^|\n)\s*{re.escape(boundary)}\s*[:\-]' for boundary in FIELD_BOUNDARIES]
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
    # "Client is Zensar" -- no colon at all, just prose. Tightly bounded to
    # 1-4 capitalized words (typical company-name shape) so it stops
    # naturally at the client name instead of running into the rest of the
    # sentence like ".+" would (there's no colon-based field boundary to
    # crop at here).
    r'(?i)\bclient\s+is\s+([A-Z][a-zA-Z0-9&.\-]*(?:\s+[A-Z][a-zA-Z0-9&.\-]*){0,3})',
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
    r'(?i)primary\s*skills?\b\s*[:\-]?\s*\n?\s*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    r'(?i)required\s*skills?\b\s*[:\-]?\s*\n?\s*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    r'(?i)technical\s*skills?\b\s*[:\-]?\s*\n?\s*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    r'(?i)key\s*skills?\b\s*[:\-]?\s*\n?\s*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    r'(?i)skill\s*set\s*[:\-]?\s*\n?\s*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    r'(?i)tech(?:nology|nical)?\s*stack\s*[:\-]?\s*\n?\s*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    # Bare "skills:" kept LAST and colon-REQUIRED (not optional) — this one is
    # generic enough that making it colon-optional would risk matching the
    # word "skills" inside unrelated sentences ("strong problem-solving and
    # debugging skills.") anywhere in the body.
    r'(?i)skills?\s*[:\-]\s*\n?\s*[•\-\*\u2022]?\s*(.+)',
]

EXPERIENCE_PATTERNS = [
    r'(?i)(\d+\+?\s*(?:-\s*\d+\s*)?years?\s*(?:of\s*)?experience)',
    r'(?i)experience\s*[:\-]\s*(\d+\+?\s*(?:-\s*\d+\s*)?years?)',
    r'(?i)(\d+\+?\s*yrs?\.?\s*(?:of\s*)?exp(?:erience)?)',
    r'(?i)minimum\s*(?:of\s*)?(\d+\+?\s*years?)',
    r'(?i)(\d+\s*-\s*\d+\s*years?)',
    # Bare "Experience: 8+" / "Exp: 5+" -- no explicit "years" unit at all.
    # Common in condensed templates; the label makes the unit unambiguous,
    # so it's safe to infer "years" even though the text doesn't say it.
    r'(?i)\bexp(?:erience)?\s*[:\-]\s*(\d+\+?)\b',
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

# Matches "City, TX" / "City TX" / "City, Texas" — resolved through resolve_state_code()
BARE_LOCATION_PATTERN = re.compile(
    r'\b([A-Z][a-zA-Z]+(?:[ \-][A-Z][a-zA-Z]+){0,2}),?\s*'
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


def _find_city_state_match(text: str, reject_first_words=None):
    """
    Sliding-window search for the first VALIDATED "City, ST" / "City ST"
    pair -- returns the re.Match object itself (so callers can use
    m.start()/m.end() for cut points), or None. Shared by find_city_state()
    and role_from_body_lead() so both use the same state-code-validated
    search instead of a raw, unvalidated regex search that can false-match
    on any two-or-three capitalized words (e.g. "Sr Salesforce Developer"
    was previously mistaken for a city/state pair by a naive raw search).
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
            return m
        pos = m.start() + 1
    return None


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
    # Strip leading street prefix so "Drive, Plano, Texas" → "Plano, Texas"
    text = _STREET_SUFFIXES.sub('', text, count=1).strip()
    m = _find_city_state_match(text, reject_first_words)
    if m:
        code = resolve_state_code(m.group(2))
        return f"{m.group(1)}, {code}"
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
    'qualifications', 'job description', 'benefits', 'visa', 'type',
    'no. of position', 'no. of positions', 'number of position',
    'number of positions',
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
    # Decode HTML entities FIRST -- upstream HTML-to-text conversion doesn't
    # always resolve these (&nbsp;, &ndash;, &rsquo;, &amp;...), and leaving
    # them undecoded corrupts field boundaries and leaks literal entity text
    # into extracted fields.
    text = html.unescape(text)
    for bad, good in _PUNCT_MAP.items():
        text = text.replace(bad, good)
    # Un-glue labels AFTER punct-fold so en-dash variants are already '-'
    text = _GLUED_LABEL_PATTERN.sub(' ', text)
    text = _unglue_leading_city_state(text)
    return text


# Some templates glue a City/State location directly onto the end of the
# preceding word with NO separator at all -- not even the missing-colon
# case _GLUED_LABEL_PATTERN handles, e.g. "Tech leadPhoenix AZ" (role and
# location fused mid-word). Detect this by looking for a lowercase letter
# immediately followed by a Capitalized city phrase immediately followed by
# a token that resolves to a real 2-letter state code, and insert a space
# right before the city. Gated on state-code validation specifically so
# this never fires on ordinary camelCase tech proper nouns (TypeScript,
# GitHub, DevOps, PowerShell...) -- none of those are ever followed by a
# real state abbreviation, so they're untouched.
_GLUED_CITY_STATE_SCAN = re.compile(
    r'(?P<pre>[a-z])(?P<city>[A-Z][a-zA-Z]+(?:[ \-][A-Z][a-zA-Z]+){0,2})'
    r'\s*,?\s*(?P<code>[A-Z]{2})\b'
)


def _unglue_leading_city_state(text: str) -> str:
    if not text:
        return text

    def _sub(m: 're.Match') -> str:
        if resolve_state_code(m.group('code')):
            return m.group('pre') + ' ' + m.group(0)[len(m.group('pre')):]
        return m.group(0)

    return _GLUED_CITY_STATE_SCAN.sub(_sub, text)


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
    # Drop a trailing location/work-mode suffix after a bare dash, but cut
    # precisely AT the location trigger (a validated City/State match, or a
    # Remote/Hybrid/Onsite keyword) instead of blindly at the dash itself.
    # The old blind cut assumed anything capitalized after a dash was a
    # location, so "Senior Technical Leads - PeopleSoft, Remote" lost
    # "PeopleSoft" (real title content, not a location) along with "Remote".
    dash_m = re.search(r'\s-\s([A-Z][a-zA-Z].*)$', s)
    if dash_m:
        tail = dash_m.group(1)
        mode_m = re.search(r'(?i)\b(remote|hybrid|onsite|on-site|on\s+location)\b', tail)
        loc_m = _find_city_state_match(tail)
        candidates = [m.start() for m in (mode_m, loc_m) if m]
        if candidates:
            trigger_pos = min(candidates)
            if trigger_pos == 0:
                s = s[:dash_m.start()].strip()
            else:
                keep = tail[:trigger_pos].strip().strip(',').strip()
                s = (s[:dash_m.start()].strip() + (' - ' + keep if keep else '')).strip()
        # else: nothing in the tail looks like a real location/work-mode
        # trigger -- leave s unchanged rather than guessing.
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
    # Looped: multi-word prefixes like "Urgent hiring for X" need more than
    # one pass -- a single pass only strips "Urgent", leaving "hiring for X"
    # behind, since each pass only consumes one keyword from the group.
    _marketing_prefix_re = re.compile(
        r'(?i)^\s*(?:needed|required|urgent|immediate|hiring(?:\s+now)?|hot|hire|'
        r'opportunity|apply|local)\b\s*(?:for\s+)?[\s:\-!.]*'
    )
    for _ in range(3):
        stripped = _marketing_prefix_re.sub('', s)
        if stripped == s:
            break
        s = stripped
    s = s.strip()
    # Drop leading punctuation left behind after stripping
    s = re.sub(r'^[\s!?.,:;\-]+', '', s)
    s = sanitize_text(s)
    if not s:
        return None
    return s if len(s) <= 80 else s[:77] + '...'


_EMAIL_ADDR_PATTERN = re.compile(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+')


def role_from_body_lead(norm_text: str) -> Optional[str]:
    """
    Last-resort role fallback for templates that never use a Role:/Position:/
    Job Title: label at all -- they just open with the bare title as the
    very first thing in the body (often glued straight into the location,
    e.g. "Sr Salesforce Developer / Tech lead Phoenix AZ (...)"). Takes the
    lead text up to whichever comes first: the next labeled field, or a
    bare City/State location. `norm_text` must already be normalize_text()'d
    so _unglue_leading_city_state() has already separated a glued location.

    Skips past a leading sender/header block first ("From: Name, Company
    email@x.com Reply to: email@x.com") common in broadcast recruiter
    templates -- otherwise the candidate ends up being that whole header
    block instead of the actual role text that follows it. Detected as: the
    text up through the LAST email address found in the first 2000 chars.
    """
    if not norm_text:
        return None
    text = norm_text
    header_slice = text[:2000]
    email_matches = list(_EMAIL_ADDR_PATTERN.finditer(header_slice))
    if email_matches:
        text = text[email_matches[-1].end():]
    text = text.lstrip()
    cut = len(text)
    m = NEXT_FIELD_PATTERN.search(text)
    if m:
        cut = min(cut, m.start())
    loc_m = _find_city_state_match(text)
    if loc_m:
        cut = min(cut, loc_m.start())
    candidate = sanitize_text(text[:cut])
    if not candidate or is_email_body(candidate) or len(candidate) > 80:
        return None
    return candidate


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

def is_reply_email(subject: str) -> bool:
    """Detect if an email is a reply/forward, which should never be treated as a fresh requirement."""
    return bool(re.match(r'^\s*(re|fw|fwd)\s*:', subject or '', re.IGNORECASE))

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


# "Hotlist" emails (recruiters advertising THEIR available bench consultants,
# asking others to send THEM requirements) use nearly all the same staffing
# keywords as a real job requirement (experience, visa, C2C, location,
# years), so is_job_requirement_email()'s keyword-count gate can't tell
# them apart -- it was treating "Please find Updated Hotlist... send me
# requirements" as if it described an actual job opening. This is the
# opposite direction: the recruiter is SUPPLYING candidates, not asking
# for one to be filled. "hotlist" itself is an almost unambiguous signal
# in this domain; the other phrases are checked together for precision.
_HOTLIST_INDICATORS = re.compile(
    r'(?i)\bhotlist\b|'
    r'\bour\s+(?:consultants?|resources?|candidates?)\s+(?:are|is)\b|'
    r'\bconsultants?\s+(?:are\s+)?ready\s+to\s+join\b|'
    r'\bbench\s+(?:consultants?|resources?)\b|'
    r'\bsend\s+(?:me\s+)?(?:the\s+)?requirements?\s+(?:to\s+my\s+email|on\s+(?:a\s+)?daily\s+basis)\b'
)


def is_hotlist_email(text: str) -> bool:
    """True for recruiter 'available consultants' broadcasts -- the
    opposite of a job requirement email. See _HOTLIST_INDICATORS above."""
    if not text:
        return False
    return bool(_HOTLIST_INDICATORS.search(text))


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
    Returns the regex capture group whose match occurs EARLIEST in the
    document, checked across all patterns in the list. Works safely on
    multiline emails and stops at the next field label.
    This function signature must remain unchanged for backend compatibility.

    BUG FIX: previously this returned the first PATTERN (in list order)
    that matched anywhere in the text, not the match that occurs earliest
    in the document. That meant a lower-priority label appearing later in
    the email (e.g. "No. of position - 5", a headcount field matched by
    the 'position' pattern) could win over a higher-priority label
    appearing earlier (e.g. "Role - Java full stack developer", matched by
    the 'role' pattern) purely because 'position' happened to be checked
    before 'role' in the pattern list -- producing garbage like role='5'.
    Now every pattern is checked, and whichever valid match starts
    earliest in the text wins, regardless of list order.
    """
    if not text:
        return None
    text = normalize_text(text)
    if not is_job_requirement_email(text):
        return None
    best_pos = None
    best_value = None
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
            if best_pos is None or match.start() < best_pos:
                best_pos = match.start()
                best_value = value
    return best_value


# Words that commonly follow "hybrid" in a TECHNICAL sense unrelated to
# work arrangement (hybrid-cloud, hybrid identity architecture, etc.) --
# used to reject those matches so bare "hybrid" isn't mistaken for a work
# mode indicator when the email is just discussing infrastructure.
_HYBRID_NON_WORKMODE_CONTEXT = re.compile(
    r'(?i)^\s*[\-]?\s*(?:cloud|identity|architecture|infrastructure|'
    r'deployment|deployments|environment|environments|approach|strategy|'
    r'model|integration|integrations)\b'
)


def extract_work_mode(text: str) -> str:
    """Extract work mode from text."""
    if not text:
        return "UNKNOWN"
    text_lower = text.lower()
    for mode, patterns in WORK_MODE_PATTERNS.items():
        for pattern in patterns:
            for m in re.finditer(pattern, text_lower):
                if mode == 'HYBRID':
                    trailing = text_lower[m.end():m.end() + 25]
                    if _HYBRID_NON_WORKMODE_CONTEXT.search(trailing):
                        continue  # false positive -- try next occurrence
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
            if re.fullmatch(r'\d+\+?', exp):
                return f"{exp} years"
            return exp
    number_match = re.search(
        r'(\d+\+?)\s*[-\u2013]?\s*(?:\d+\+?\s*)?years?', text, re.IGNORECASE
    )
    if number_match:
        return f"{number_match.group(1)} years"
    return None


# ---------------------------------------------------------------------------
# Keyword-based skills fallback (for emails with NO skills label at all)
#
# Some JDs never use a "Skills:"/"Required Skills:"/"Tech Stack:" heading —
# the technologies are just named throughout ordinary prose sentences
# ("Extensive experience building AI applications using Python ... and
# TypeScript ..."). The labeled SKILLS_PATTERNS above have nothing to
# anchor to in that case, so extract_skills() would otherwise return [].
# This scans the body against a known technology keyword list instead.
# Deliberately used ONLY as a last resort when no labeled section exists —
# it never overrides or second-guesses a labeled match.
# ---------------------------------------------------------------------------

TECH_KEYWORDS = [
    # Languages
    'Python', 'TypeScript', 'JavaScript', 'Java', 'C++', 'C#', 'Golang', 'Go',
    'Rust', 'Scala', 'Ruby', 'PHP', 'Kotlin', 'Swift',
    # AI / ML / GenAI
    'LangChain', 'LlamaIndex', 'LangGraph', 'CrewAI', 'AutoGen',
    'Semantic Kernel', 'OpenAI Agents SDK', 'Agentic AI',
    'Retrieval-Augmented Generation', 'RAG', 'Prompt Engineering',
    'LLM', 'GPT', 'TensorFlow', 'PyTorch', 'Hugging Face', 'scikit-learn',
    'NLP',
    # Backend frameworks
    'FastAPI', 'Flask', 'Django', 'Spring Boot', 'Spring MVC', 'Node.js',
    'NestJS', 'Express', 'ASP.NET', '.NET', 'Vert.x',
    # Frontend
    'React.js', 'React', 'Angular', 'Vue.js', 'Vue',
    # Cloud
    'AWS', 'Azure', 'GCP', 'Google Cloud',
    # Databases / vector stores
    'PostgreSQL', 'MySQL', 'MongoDB', 'Redis', 'Oracle', 'SQL Server',
    'SQL', 'PL/SQL',
    'Pinecone', 'FAISS', 'ChromaDB', 'Weaviate', 'pgvector',
    'Vector Database',
    # DevOps
    'Docker', 'Kubernetes', 'Terraform', 'Jenkins', 'CI/CD',
    'GitHub Actions',
    # Messaging / streaming
    'Kafka', 'RabbitMQ',
    # APIs / architecture
    'RESTful', 'REST API', 'GraphQL', 'Microservices', 'gRPC',
    # ERP / Finance systems
    'PeopleSoft', 'SAP', 'Oracle EBS', 'Oracle Financials', 'Workday',
    'NetSuite', 'Dynamics 365', 'JD Edwards', 'Hyperion',
    'General Ledger', 'GAAP', 'Accounts Payable', 'Accounts Receivable',
    'Financial Reporting', 'Procure to Pay', 'Order to Cash',
    # SAP modules -- as compound "SAP <MODULE>" phrases only. Bare module
    # codes (MM, WM, PP, SD, IM...) are too short/ambiguous to match safely
    # on their own (e.g. "PP" or "IM" could be almost anything), but "SAP"
    # immediately preceding one is specific enough to be safe.
    'SAP MM', 'SAP WM', 'SAP PP', 'SAP SD', 'SAP EDI', 'SAP IM',
    'SAP FICO', 'SAP ABAP', 'SAP BASIS', 'SAP QM', 'SAP PM', 'SAP HR',
    'SAP HCM', 'SAP EWM', 'SAP MDG',
    # Oracle EBS supply-chain modules -- spelled-out full names only, same
    # reasoning as SAP modules above: the short codes (INV, BOM, WIP, WMS,
    # OM) are too ambiguous/common to match safely standalone.
    'Bills of Material', 'Work in Process', 'Warehouse Management System',
    'Order Management', 'Oracle E-Business Suite',
    # Project methodology
    'Agile', 'Scrum', 'Kanban', 'Waterfall', 'SAFe', 'Sprint Planning',
    'User Stories', 'UAT', 'SDLC',
]


def _keyword_boundary_pattern(keyword: str) -> re.Pattern:
    """Word-boundary-safe pattern that won't match a keyword as a substring
    of a longer word (e.g. 'Java' inside 'JavaScript'), even though some
    keywords contain punctuation ('.', '+', '#') that \\b doesn't handle.
    Common-English-word keywords ('Go', 'Express', 'Swift', 'R') are matched
    CASE-SENSITIVELY (require the capitalized tech form) to avoid false
    positives like "please go through" matching the Golang keyword."""
    esc = re.escape(keyword)
    flags = re.IGNORECASE
    if keyword in _CASE_SENSITIVE_KEYWORDS:
        flags = 0
    return re.compile(r'(?<![A-Za-z0-9])' + esc + r'(?![A-Za-z0-9])', flags)


_CASE_SENSITIVE_KEYWORDS = {'Go', 'Express', 'Swift', 'R', 'C', 'SAFe'}
_TECH_KEYWORD_PATTERNS = [(kw, _keyword_boundary_pattern(kw)) for kw in TECH_KEYWORDS]


# Keywords that can appear as part of an unrelated proper noun -- checked
# against the ~15 chars immediately before each match. "Express" inside
# "American Express" (the company) is not the Express.js framework. Uses
# finditer + skip-forward (not a single search()) so a genuine Express.js
# mention elsewhere in the same email is still found even when the first
# occurrence is this kind of false positive.
_KEYWORD_EXCLUDE_PRECEDING = {
    'Express': re.compile(r'(?i)american\s*$'),
}


def extract_skills_from_keywords(text: str, max_skills: int = 15) -> List[str]:
    """Fallback: scan body for known technology names when no labeled
    skills section exists. Returns matches in the order they first appear
    in the text. Longer/more-specific keywords (e.g. 'React.js') suppress
    their shorter substrings ('React') so both don't get listed redundantly."""
    if not text:
        return []
    text_scope = text[:6000]

    found = []  # (start, end, keyword)
    for kw, pattern in _TECH_KEYWORD_PATTERNS:
        exclude_re = _KEYWORD_EXCLUDE_PRECEDING.get(kw)
        if exclude_re:
            for m in pattern.finditer(text_scope):
                preceding = text_scope[max(0, m.start() - 15):m.start()]
                if exclude_re.search(preceding):
                    continue  # false positive context -- try the next occurrence
                found.append((m.start(), m.end(), kw))
                break
        else:
            m = pattern.search(text_scope)
            if m:
                found.append((m.start(), m.end(), kw))

    # Drop a keyword only when its OWN matched occurrence is fully contained
    # within another keyword's matched occurrence at the SAME text location
    # (e.g. "React" matching inside "React.js" at the same start position).
    # This does NOT suppress a keyword just because its text happens to be a
    # substring of another keyword found somewhere ELSE in the email -- e.g.
    # "SQL" and "PL/SQL" mentioned as two separate skills in different
    # places are both genuine and both kept.
    kept = []
    for start, end, kw in found:
        contained = any(
            kw.lower() != other_kw.lower()
            and other_start <= start and end <= other_end
            for other_start, other_end, other_kw in found
        )
        if contained:
            continue
        kept.append((start, kw))

    kept.sort(key=lambda x: x[0])
    return [kw for _, kw in kept][:max_skills]


def extract_skills(text: str) -> List[str]:
    """Extract skills from text as a list."""
    if not text:
        return []

    # Strip known boilerplate footers (portal signup ads, etc.) before
    # scanning for skills -- scoped to skills specifically rather than
    # applied globally, since ad copy ("Sign-Up for your account...",
    # "Hire our IT Recruiter at just $499/month") was being matched as
    # if it were real skill content, both by the labeled-section path and
    # the keyword fallback. Other fields (e.g. employment_types) are
    # unaffected by this and still scan the full, unstripped text.
    text = strip_boilerplate_footer(text)

    # Restrict scan window to avoid extracting generic skills from recruiter
    # boilerplate footers (e.g. portal signup ads) that can appear far down
    # in the email. Widened from 1500 -> 6000: longer, well-structured JDs
    # routinely have a Job Summary + Responsibilities section (which can run
    # 1500-2500+ chars) BEFORE the actual "Required Skills" label, so 1500
    # was cutting off the label entirely on exactly the emails where skill
    # extraction should work best. 6000 comfortably covers Summary +
    # Responsibilities + Skills for realistic JDs while still stopping well
    # before most footer/portal-ad boilerplate.
    text_scope = text[:6000]
    
    skills_text = None
    # Capture across the WHOLE skills section, not just one line — a bulleted
    # "Required Skills" list is many lines, and the old single-line capture
    # combined with an outer split('\n', 1)[0] elsewhere silently dropped
    # every bullet after the first. re.DOTALL lets '.' span newlines here.
    for pattern in SKILLS_PATTERNS:
        match = re.search(pattern, text_scope, re.IGNORECASE | re.DOTALL)
        if match:
            skills_text = match.group(1).strip()
            stop_match = STOP_PATTERN.search(skills_text)
            if stop_match:
                skills_text = skills_text[:stop_match.start()].strip()
            break
    if not skills_text:
        return extract_skills_from_keywords(text_scope)

    # Cap how much of the captured block we process — a stray missing stop
    # boundary shouldn't let this run through the rest of the email.
    skills_text = skills_text[:3000]

    # Bulleted prose mode: "•  Strong experience with Java 8+, preferably
    # Java 11/17/21.•  Strong hands-on experience with Spring Boot..."
    # Splitting this on commas (old behavior) shreds sentences into
    # meaningless fragments. Instead: split into one chunk per bullet, then
    # pull the actual skill tokens out of each sentence.
    if re.search(r'[•\u2022]|\n\s*[-\*]\s', skills_text):
        bullet_chunks = re.split(r'[•\u2022]|\n\s*[-\*]\s', skills_text)
        skills = []
        for chunk in bullet_chunks:
            skills.extend(_extract_skill_tokens(chunk))
        return list(dict.fromkeys(skills))[:20]

    # Flat list mode: "Python, Java, AWS, Docker" — original comma/semicolon
    # splitting logic, unchanged.
    parts = re.split(r',|;|\||/|\n|\band\b', skills_text)
    skills = []
    for skill in parts:
        skill = skill.strip()
        skill = re.sub(r'^[•\-\*\u2022]+\s*', '', skill)
        if not skill:
            continue
        if re.match(r'(?i)^\d+\+?\s*years?\b', skill):
            continue
        skill = re.sub(r'(?i)^\s*(?:including|such\s+as)\s*[:\-]?\s*', '', skill)
        skill = re.sub(r'(?i)\b(with|experience|knowledge|required|preferred)\b.*', '', skill)
        skill = re.sub(r'\s+', ' ', skill).strip()
        if 2 < len(skill) < 40:
            skill = skill.rstrip('.')
            skills.append(skill)
    return list(dict.fromkeys(skills))[:10]


# Leading filler phrases that precede the actual skill in a bulleted JD
# sentence, e.g. "Strong hands-on experience with React.js" -> "React.js".
# Longest-first so "strong hands-on experience with" matches before the
# shorter "experience with" would truncate it early.
_SKILL_SENTENCE_PREFIXES = [
    r'strong\s+hands[\s\-]?on\s+experience\s+(?:with|in)',
    r'hands[\s\-]?on\s+experience\s+(?:with|in|designing|implementing|building)',
    r'strong\s+(?:working\s+)?knowledge\s+of',
    r'good\s+(?:working\s+)?knowledge\s+of',
    r'good\s+understanding\s+of',
    r'strong\s+understanding\s+of',
    r'strong\s+experience\s+(?:with|in)',
    r'extensive\s+experience\s+(?:with|in|building)',
    r'experience\s+(?:with|in|integrating|building|developing|designing)',
    r'familiarity\s+with',
    r'knowledge\s+of',
    r'understanding\s+of',
    r'proficiency\s+(?:with|in)',
    r'expertise\s+(?:with|in)',
]
_SKILL_SENTENCE_PREFIX_PATTERN = re.compile(
    r'^\s*(?:' + '|'.join(_SKILL_SENTENCE_PREFIXES) + r')\s+', re.IGNORECASE
)
# Trailing filler that sometimes survives after the prefix strip, e.g.
# "...React.js for backend services" -> keep "React.js", drop the rest.
_SKILL_TRAILING_FILLER = re.compile(
    r'\s+(?:such\s+as|for|to\s+(?:build|deliver|join|support)|is\s+a\s+plus)\b.*$',
    re.IGNORECASE
)
_GENERIC_SKILL_WORDS = {
    'a', 'the', 'and', 'or', 'with', 'in', 'of', 'strong', 'good', 'solid',
    'related field', 'similar', 'etc', 'other', 'similar systems',
}


def _extract_skill_tokens(sentence: str) -> List[str]:
    """
    Pull actual skill/technology names out of one JD bullet sentence.
    Strips a leading filler phrase ('Strong experience with ...'), then
    splits the remainder on commas/'and' into individual candidate tokens.
    Returns [] for bullets that aren't skill-shaped at all (soft-skill
    bullets like 'Strong problem-solving and debugging skills' still pass
    through — they're short and specific enough to keep as-is).
    """
    sentence = sentence.strip().rstrip('.').strip()
    if not sentence:
        return []
    if re.match(r'(?i)^\d+\+?\s*years?\b', sentence):
        return []
    core = _SKILL_SENTENCE_PREFIX_PATTERN.sub('', sentence).strip()
    core = _SKILL_TRAILING_FILLER.sub('', core).strip()
    if not core:
        return []
    tokens = re.split(r',\s*|\s+and\s+', core)
    results = []
    for tok in tokens:
        tok = tok.strip().strip('.')
        # Strip stray leading conjunction/hedge words a comma-split can leave
        # behind, e.g. ", and REST APIs" -> "and REST APIs" -> "REST APIs".
        tok = re.sub(r'(?i)^(?:and|preferably|or)\s+', '', tok).strip()
        tok_lower = tok.lower()
        if not tok or tok_lower in _GENERIC_SKILL_WORDS:
            continue
        if 1 < len(tok) < 45:
            results.append(tok)
    return results


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
                
                if not vendor_name and vendor_email:
                    domain_match = re.search(r'@([^.]+)\.', vendor_email)
                    if domain_match:
                        vendor_name = domain_match.group(1).capitalize()
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

    Intentional: if `role` is not found (stays 'UNKNOWN'), confidence is
    forced to 0.0 regardless of how many other fields were extracted. A row
    with no identifiable role is treated as not a usable requirement even if
    location/rate/etc. are present.
    """
    if not parsed:
        return 0.0
    
    important_fields = ['client', 'location', 'rate', 'employment_types', 'role', 'must_have_skills']
    valid_fields = 0
    
    for field in important_fields:
        value = parsed.get(field)
        if field == 'employment_types' or field == 'must_have_skills':
            if value and isinstance(value, list) and value != ['UNKNOWN']:
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
    # ROLE-SPECIFIC PARSING: real job titles are short (typically 2-8 words).
    # If crop_at_next_field() didn't find a clean boundary (e.g. HTML-collapsed
    # single-line emails with no recognizable "Location:"/signature marker
    # nearby), this cuts off at the point runaway sentence text starts,
    # instead of falling through to a blunt 60-char truncation that grabs
    # unrelated trailing words like "AWS Engineer so on more unwanted...".
    words = role.split()
    if len(words) > 8:
        role = ' '.join(words[:8])
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
    # BUG FIX: HTML tables (e.g. bulk hotlist/broadcast emails) render
    # header cells like <td>Client:</td><td>Requirements & Resumes From:</td>
    # with only a space between them (td/th deliberately aren't treated as
    # line breaks in html_to_text — see cleaner.py's HTMLToTextParser docstring
    # for the identical "TrintechLocation:" fusion problem). That flattens
    # to "Client: Requirements & Resumes From: ..." in plain text, and
    # CLIENT_PATTERNS then grabs "Requirements & Resumes From" as if it were
    # a real client name -- it's actually the NEXT column's header, not a
    # value at all. A real client/company name never starts with generic
    # staffing nouns like this, so reject on sight rather than accept
    # boilerplate as if it were a company name.
    _GENERIC_NON_CLIENT_LEAD_WORDS = (
        'requirement', 'requirements', 'resume', 'resumes', 'resource',
        'resources', 'candidate', 'candidates', 'consultant', 'consultants',
        'submit', 'submission', 'submissions', 'send', 'share', 'kindly',
        'please',
    )
    first_word = client.split()[0].lower().strip('.,:;') if client.split() else ''
    if first_word in _GENERIC_NON_CLIENT_LEAD_WORDS:
        return None
    # ROLE-SPECIFIC PARSING: real client/company names are short (2-10 words).
    # Same runaway-text problem as role — cap word count before falling
    # back to a blunt character truncation.
    words = client.split()
    if len(words) > 10:
        client = ' '.join(words[:10])
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
    # If it contains a keyword but no city was found, we still want to 
    # return the original location string to retain extra details (e.g. 
    # 'Hybrid - New Jersey') instead of collapsing it to just 'Hybrid'.
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
# Boilerplate-footer stripping
#
# Mailing-list unsubscribe blocks and recruiter-portal ads can contain
# field-shaped noise that gets mistaken for real job data -- a Google Group
# literally named ".../Daily IT Requirements-C2C" was being matched as an
# employment type; a PROHIRES portal ad's "$499/month" was being matched as
# a rate. Rather than patch each field extractor individually, strip
# everything from the earliest known footer-start signature onward BEFORE
# any extraction runs, so no field can pull data from the footer at all.
# ---------------------------------------------------------------------------

_BOILERPLATE_FOOTER_PATTERN = re.compile(
    r'(?i)you received this message because you are subscribed to the google groups|'
    r'to unsubscribe from this group|'
    r'sign-up for your account with prohires|'
    r'hire our it recruiter at just \$'
)


def strip_boilerplate_footer(text: str) -> str:
    """Truncate `text` at the earliest known boilerplate-footer signature,
    if any. Leaves text unchanged when no footer is detected."""
    if not text:
        return text
    m = _BOILERPLATE_FOOTER_PATTERN.search(text)
    if m:
        return text[:m.start()]
    return text


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

    if not is_job_requirement_email(full_text) or is_hotlist_email(full_text):
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

    # Attempt AI parsing first. We will try OpenAI -> Claude -> SpaCy -> Regex fallback
    # We will track the reasons for fallback in parsing_log for debugging
    ai_parsed = None
    parsing_log = []
    try:
        from openai_parser import parse_requirement_openai
        ai_parsed = parse_requirement_openai(safe_subject, safe_body)
        if ai_parsed:
            parsing_log.append("OpenAI (gpt-4o-mini): Success")
        else:
            parsing_log.append("OpenAI (gpt-4o-mini): Failed or returned None.")
    except Exception as e:
        parsing_log.append(f"OpenAI (gpt-4o-mini): Exception - {e}")
        ai_parsed = None

    if not ai_parsed:
        try:
            # parse_requirement_text now defined locally in this file (merged
            # in from claude_parsing_service.py — see top of file)
            ai_parsed = parse_requirement_text(safe_subject, safe_body)
            if ai_parsed:
                parsing_log.append("Claude 3.5 Sonnet: Success")
            else:
                parsing_log.append("Claude 3.5 Sonnet: Failed or returned None.")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"AI requirement parsing failed: {e}. Falling back to regex.")
            parsing_log.append(f"Claude 3.5 Sonnet: Exception (API Key / Subscription / Error) - {e}")
            ai_parsed = None

    # OPTIMIZATION: track which stage actually produced ai_parsed. SpaCy's
    # NER is the weakest signal in this chain for client/location (it's
    # guessing from unlabeled entities, not reading a labeled field the
    # way OpenAI/Claude or a "Client:"/"Location:" regex match do) — this
    # flag lets the client/location blocks below give a labeled regex
    # match priority over a SpaCy guess specifically, instead of treating
    # every ai_parsed source as equally trustworthy.
    ai_source_is_spacy = False

    if not ai_parsed:
        try:
            from spacy_parser import parse_requirement_spacy
            ai_parsed = parse_requirement_spacy(safe_subject, safe_body)
            if ai_parsed:
                parsing_log.append("SpaCy NLP: Success (Partial Extractor)")
                ai_source_is_spacy = True
            else:
                parsing_log.append("SpaCy NLP: Failed or returned None.")
        except Exception as e:
            parsing_log.append(f"SpaCy NLP: Exception - {e}")
            ai_parsed = None
            
    if not ai_parsed:
        parsing_log.append("Regex Fallback: Executing due to AI model failures.")

    ai_parsed = ai_parsed or {}

    def _ai_field(key: str, unknown_value=None):
        """Return the AI's value for `key`, or None if it's missing/blank
        or equal to the AI's own "didn't find it" sentinel (e.g. 'UNKNOWN'
        or ['UNKNOWN']) — so that sentinel doesn't block the regex fallback
        for this field from running."""
        value = ai_parsed.get(key)
        if value is None:
            return None
        if unknown_value is not None and value == unknown_value:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ── Role ──────────────────────────────────────────────────────────────
    # AI first (run through the same cleaner regex output gets — P0 fix #2),
    # then body-first regex to prevent subject-line poisoning, then the
    # subject-line and bare-body-lead fallbacks.
    role = clean_role(_ai_field('role', unknown_value='UNKNOWN'))
    if not role:
        raw_role = first_match(ROLE_PATTERNS, norm_body) or first_match(ROLE_PATTERNS, full_text)
        role = (
            clean_role(raw_role)
            or clean_role(role_from_subject(safe_subject))
            or clean_role(role_from_body_lead(norm_body))
        )
    if not role or is_email_body(role):
        role = 'UNKNOWN'

    # ── Client ────────────────────────────────────────────────────────────
    # OPTIMIZATION: when SpaCy produced ai_parsed, check for a labeled
    # regex match FIRST — a "Client:" label is a much stronger signal
    # than an unlabeled NER guess, so it should win even though SpaCy
    # technically ran first in the fallback chain and would otherwise
    # short-circuit this block via _ai_field('client').
    client = None
    if ai_source_is_spacy:
        raw_client_early = first_match(CLIENT_PATTERNS, norm_body) or first_match(CLIENT_PATTERNS, full_text)
        client = clean_client(raw_client_early)
        if client and is_email_body(client):
            client = None
    if not client:
        client = clean_client(_ai_field('client'))
    if client and is_email_body(client):
        client = None
    if not client:
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
                # BUG FIX: this fallback regex has no awareness of what role was
                # already extracted above — it just grabs whatever capitalized
                # phrase follows a dash anywhere in the body. When the role
                # title itself gets restated near a dash elsewhere in the email
                # (subject-line repeat, signature, etc.), this fallback was
                # capturing the ROLE TEXT ITSELF as the "client" — producing
                # rows where Client is character-for-character identical to
                # Role (e.g. "Veeva Technical Architect" as both). Per spec,
                # client should default to N/A/None when a real one can't be
                # confidently found — a duplicate of the role is not a real
                # client and is worse than leaving it blank, since it reads as
                # legitimate data. Reject the candidate outright if it matches
                # (or is a substring/superstring of) the already-extracted role.
                role_lower = (role or '').strip().lower()
                cand_lower = (cand or '').strip().lower()
                is_role_echo = bool(cand_lower) and bool(role_lower) and (
                    cand_lower == role_lower
                    or cand_lower in role_lower
                    or role_lower in cand_lower
                )
                if cand and len(cand) <= 40 and not is_email_body(cand) and not is_role_echo:
                    client = cand

    # ── Location ──────────────────────────────────────────────────────────
    # OPTIMIZATION: same reasoning as Client above — a labeled
    # "Location:"/"City:" regex match beats SpaCy's unlabeled GPE guess.
    location = None
    if ai_source_is_spacy:
        raw_location_early = (
            first_match(LOCATION_PATTERNS, norm_body)
            or first_match(LOCATION_PATTERNS, full_text)
        )
        location = clean_location(raw_location_early)
        if location and is_email_body(location):
            location = None
    if not location:
        location = clean_location(_ai_field('location'))
    if location and is_email_body(location):
        location = None
    if not location:
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
    rate = clean_rate(_ai_field('rate'))
    if rate and is_email_body(rate):
        rate = None
    if not rate:
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
    duration = clean_duration(_ai_field('duration'))
    if duration and is_email_body(duration):
        duration = None
    if not duration:
        raw_duration = (
            first_match(DURATION_PATTERNS, norm_body)
            or first_match(DURATION_PATTERNS, full_text)
        )
        duration = clean_duration(raw_duration)
        if duration and is_email_body(duration):
            duration = None

    # ── Other fields ──────────────────────────────────────────────────────
    # Each falls back independently to its regex/heuristic extractor, which
    # already returns the correct 'UNKNOWN' / ['UNKNOWN'] / None sentinel
    # when nothing is found — so no separate normalization pass is needed.
    work_mode = _ai_field('work_mode', unknown_value='UNKNOWN') or extract_work_mode(full_text)

    ai_employment_types = _ai_field('employment_types', unknown_value=['UNKNOWN'])
    employment_types = ai_employment_types or extract_employment_types(full_text)

    experience = _ai_field('experience') or extract_experience(full_text)

    ai_skills = _ai_field('skills')
    skills = ai_skills if ai_skills else extract_skills(full_text)

    # ── Vendor info ───────────────────────────────────────────────────────
    vendor_name = None
    vendor_email = None
    from_header = safe_headers.get('from', '')
    reply_to_header = safe_headers.get('reply-to', '') or safe_headers.get('reply_to', '')

    target_email_header = reply_to_header if reply_to_header else from_header
    if target_email_header:
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+', target_email_header)
        if email_match:
            vendor_email = email_match.group(0).lower()

    if from_header:
        name_match = re.match(r'^([^<]+)<', from_header)
        if name_match:
            vendor_name = name_match.group(1).strip().strip('"\'')
            if len(vendor_name) > 30:
                vendor_name = vendor_name.split(',')[0].strip()
        elif '@' not in from_header:
            vendor_name = from_header.strip().strip('"\'').split(',')[0].strip()

    # Fallback: if missed from header, extract from email domain (never the body)
    if not vendor_name and vendor_email:
        domain_match = re.search(r'@([^.]+)\.', vendor_email)
        if domain_match:
            vendor_name = domain_match.group(1).capitalize()

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
        'parsing_model': _ai_field('parsing_model') or "Regex Parser",
        'parsing_log': parsing_log,
    }

    parsed['parse_confidence'] = calculate_confidence(parsed)
    parsed['is_likely_requirement'] = parsed['parse_confidence'] >= 0.3

    # Final guard — never return email body content in any field
    for key, value in parsed.items():
        if isinstance(value, str) and value and is_email_body(value):
            parsed[key] = None

    return parsed


# ---------------------------------------------------------------------------
# Multi-requirement support
#
# parse_requirement() above always treats the WHOLE email body as one
# requirement: every field extractor independently scans the entire body
# and keeps only the first match it finds. When an email actually
# contains multiple distinct job postings, every field after the first
# one is silently discarded, and — because Role/Client/Location/etc. each
# search independently — the single row that IS produced can even mix
# fields from different postings.
#
# parse_requirement() itself is intentionally left untouched (its
# docstring already states its signature/behavior must not change, and
# every existing caller/test depends on that). This section adds a
# strictly additive wrapper instead: split the body into candidate
# requirement blocks only when there's strong, unambiguous evidence of
# more than one posting, then run the existing, unmodified
# parse_requirement() on each block independently. Whenever that
# evidence isn't there, it falls straight through to exactly what
# parse_requirement() already returns today — so single-requirement
# emails (the overwhelming majority) are completely unaffected.
# ---------------------------------------------------------------------------

# Minimum character distance between two accepted anchors before a
# same-text repeat is treated as a restatement (e.g. subject echoed right
# after a "Role:" line) rather than a second posting.
_ANCHOR_MIN_GAP = 120


def _find_role_label_anchors(text: str) -> List[tuple]:
    """Find every labeled role occurrence (e.g. "Job Title:", "Role:") in
    `text`, in document order. Reuses ROLE_PATTERNS — the same labels
    first_match() looks for — since those already require an explicit
    trailing ':' or '-', so a bare mention of the word "role" or
    "position" in a sentence never matches. That keeps false-positive
    anchors low without any new pattern list to maintain separately.

    Returns a list of (match_start, line_start, captured_value) tuples.
    """
    anchors = []
    for pattern in ROLE_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            captured = m.group(1).split('\n', 1)[0]
            value = sanitize_text(captured)
            if not value:
                continue
            value = crop_at_next_field(value)
            if not value or is_email_body(value) or len(value) > 200:
                continue
            line_start = text.rfind('\n', 0, m.start()) + 1
            anchors.append((m.start(), line_start, value))
    anchors.sort(key=lambda a: a[0])
    return anchors


def split_into_requirement_segments(body_text: str, max_segments: int = 10) -> List[str]:
    """Best-effort detection of multiple distinct job postings inside one
    email body.

    Deliberately conservative — returns [body_text] (i.e. "don't split,
    treat as a single requirement") unless there are at least two
    clearly distinct role-labeled blocks. A false-positive split (cutting
    one real posting into pieces) is worse than the existing
    under-splitting behavior this exists to fix, so anything ambiguous
    defers to the current single-block behavior untouched.
    """
    if not body_text or not body_text.strip():
        return [body_text]

    raw_anchors = _find_role_label_anchors(body_text)
    if len(raw_anchors) < 2:
        return [body_text]

    accepted: list = []
    for pos, line_start, value in raw_anchors:
        if accepted:
            prev_pos, _prev_line_start, prev_value = accepted[-1]
            if pos - prev_pos < _ANCHOR_MIN_GAP and value.strip().lower() == prev_value.strip().lower():
                # Same role restated close together — not a second posting.
                continue
        accepted.append((pos, line_start, value))

    if len(accepted) < 2:
        return [body_text]

    accepted = accepted[:max_segments]

    segments = []
    for i, (_, line_start, _value) in enumerate(accepted):
        seg_end = accepted[i + 1][1] if i + 1 < len(accepted) else len(body_text)
        segment = body_text[line_start:seg_end].strip()
        if segment:
            segments.append(segment)

    return segments if len(segments) >= 2 else [body_text]


def parse_requirements(
    subject: str,
    body: str,
    headers: Dict[str, Any]
) -> List[tuple]:
    """
    Multi-requirement-aware wrapper around parse_requirement().

    Existing callers that only ever expect a single requirement per email
    should keep calling parse_requirement() directly — unchanged, same
    behavior as always. Callers that want every requirement an email
    actually contains (not just whichever one's fields happened to match
    first) should call this instead.

    Splits `body` into candidate blocks (see
    split_into_requirement_segments) and runs the existing, unmodified
    parse_requirement() on each block independently, so per-field
    extraction quality for each requirement is identical to today's
    single-requirement path — this only changes HOW MANY times that
    logic runs, never what it does. Falls straight through to exactly
    what parse_requirement() itself would return whenever segmentation
    doesn't find strong evidence of more than one posting, or when none
    of the split pieces individually look like a real requirement (e.g.
    a false split inside one JD) — never returns fewer requirements than
    the existing single-call path would have for the same email.

    Returns a list of (parsed_dict, segment_text) tuples. segment_text is
    the slice of `body` that produced parsed_dict — callers should clean
    and hash THAT (not the full original body) when saving each row, so
    job_description/jd_hash reflect that specific posting rather than the
    whole multi-posting email repeated identically on every row. In the
    no-split/fallback cases segment_text is the original `body` itself,
    matching exactly what today's single-call sites already do.
    """
    safe_body = body or ''
    segments = split_into_requirement_segments(safe_body)

    if len(segments) <= 1:
        return [(parse_requirement(subject, safe_body, headers), safe_body)]

    results = []
    for segment in segments:
        parsed = parse_requirement(subject, segment, headers)
        if parsed.get('is_likely_requirement'):
            results.append((parsed, segment))

    if not results:
        return [(parse_requirement(subject, safe_body, headers), safe_body)]

    return results