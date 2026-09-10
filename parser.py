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
    'Client', 'Client Name', 'Location', 'Duration', 'Rate', 'Skills', 'Experience',
    'Employment', 'Remote', 'Hybrid', 'Onsite', 'On-site', 'Contract',
    'Need', 'Looking for', 'Position', 'Opening', 'Role', 'Job Title',
    'Job Description', 'Responsibilities', 'Required Skills', 'Preferred Skills',
    'Qualifications', 'Benefits', 'About Company', 'Equal Opportunity',
    'Disclaimer', 'Vendor', 'Recruiter', 'Contact', 'Phone', 'Email',
    'Regards', 'Thanks', 'Best Regards', 'Best,', 'Warm Regards',
    'Sincerely', 'Yours', 'Thank You', 'Cheers',
    'Job Summary', 'Key Responsibilities', 'Requirements', 'Minimum Requirements',
    'Preferred Qualifications', 'Preferred Experience', 'Nice to Have',
    'Education', 'Certifications', 'Schedule',
    'Work Schedule', 'Shift', 'Hours', 'Benefits', 'Perks'
]

# Field boundaries should only stop extraction when they're genuinely acting
# as a label (start of a line, optionally followed by a colon) — not when
# they appear naturally mid-sentence, e.g. "5+ years of experience with SQL"
# was being incorrectly cut at "experience" even though it wasn't a real
# "Experience:" section label.
# BUG FIX: this required a colon/hyphen right after the boundary word,
# so bare headings with no colon at all (common from real <li>/<h2>-
# derived HTML, e.g. "Qualifications" or "Preferred Experience" sitting
# alone on their own line) were never recognized as a stop point. That
# let a skills-section capture run straight through Qualifications, the
# sender's own signature, and the unsubscribe footer before finally
# hitting the 3000-char cap -- all of which then got bullet-split as if
# it were "skills". (?=\n|$) accepts a bare heading (line-start-anchored,
# so this stays safe against matching mid-sentence).
STOP_PATTERNS = [
    rf'(?:^|\n)\s*{re.escape(boundary)}\s*(?:[:\-]|(?=\n|$))'
    for boundary in FIELD_BOUNDARIES
]
STOP_PATTERN = re.compile('|'.join(STOP_PATTERNS), re.IGNORECASE)

EMPLOYMENT_KEYWORDS = {
    # BUG FIX: "(C-C) : (3) AI Engineer/Banking..." / "Contract Status :
    # C-C preferred or W2+ Referral" -- "C-C" (bare single-letter-dash-
    # single-letter) is a common recruiter shorthand for Corp-to-Corp,
    # distinct from "C2C"/"corp-to-corp"/"corp2corp" already covered
    # below, and was missing entirely.
    'C2C': ['c2c', 'corp to corp', 'corp-to-corp', 'corp2corp', 'c-c'],
    'W2': ['w2'],
    '1099': ['1099'],
    'FULLTIME': ['full time', 'full-time', 'fulltime', 'permanent', 'fte'],
    'CONTRACT': ['contract', 'contractual', 'contract-to-hire']
}

# Flat set of every employment-type keyword string above, used by
# _looks_like_bare_job_title() to reject a bare employment-type value
# ("Contract") sitting alone on its own line from being mistaken for a
# job title — see that function's own comment for the confirmed
# real-world case this fixes.
_EMPLOYMENT_TYPE_BARE_WORDS = {
    kw for keywords in EMPLOYMENT_KEYWORDS.values() for kw in keywords
}

WORK_MODE_PATTERNS = {
    'REMOTE': [
        r'\b100%\s*remote\b', r'\bremote\s+opportunity\b', r'\bremote\b',
        r'\bwork\s+from\s+home\b', r'\bwfh\b'
    ],
    'HYBRID': [r'\bhybrid\s+schedule\b', r'\bhybrid\b'],
    'ONSITE': [r'\bon\s*-?\s*site\b', r'\bin\s*-?\s*person\b', r'\bon\s+location\b']
}

# BUG FIX ("role: 'Openings'" from "Immediate Openings || Automation
# Engineer with Lifescience and Delta V system Experience || contract ||
# West Point, PA"): role_from_subject()'s pipe-split blindly took segment
# [0], assuming the subject convention is always "<Role> || <Location> ||
# <Duration>" — but "<generic prefix> || <Role> || <Type> || <Location>"
# is just as common, with segment [0] being pure marketing filler
# ("Immediate Openings", "Urgent Requirement") rather than any part of
# the real title. This pattern recognizes that filler so the pipe-split
# logic below can skip it and try the next segment instead.
_GENERIC_SUBJECT_ROLE_PATTERN = re.compile(
    r'(?i)^(?:new|weekly|daily|urgent|immediate|today\'?s?)?\s*'
    r'(?:job\s+openings?|job\s+opportunit(?:y|ies)|new\s+opportunit(?:y|ies)|'
    r'urgent\s+requirements?|new\s+requirements?|job\s+alerts?|hiring\s+alerts?|'
    r'immediate\s+openings?|urgent\s+openings?|urgent\s+hiring|now\s+hiring|'
    r'we\s+are\s+hiring|open\s+positions?|new\s+positions?|job\s+postings?|'
    # BUG FIX ("role: 'JD'" from "JD || Senior Cloud Full Stack Developer ||
    # MI ( Locals )"): recruiter subjects commonly lead with a bare "JD"
    # segment (shorthand for "Job Description") before the real title —
    # structurally identical to "Immediate Openings || <real title>" above,
    # just a different filler word. Added as its own alternative since it's
    # a standalone abbreviation, not a pluralizable noun phrase like the
    # others in this list.
    r'new\s+postings?|requirements?|openings?|positions?\s+available|jd|'
    # BUG FIX ("role: None" from "Hiring || Cloud Architect ||
    # Holtsville, NY"): "now hiring"/"we are hiring"/"urgent hiring" were
    # already covered above, but a bare "Hiring" alone as its own
    # leading segment wasn't.
    r'hiring|'
    # BUG FIX ("role: 'JOB'" from "JOB || ServiceNow Developer || Chicago,
    # IL || Day-1- Onsitte" -- confirmed real case): a bare "JOB" leading
    # segment (no "opening"/"posting"/"alert" attached) is just as common
    # a convention as "JD" above, and was completely unrecognized -- it
    # survived whole as if it were the real title.
    r'job|'
    # BUG FIX ("role: 'ONISTE ROLE'" from "ONISTE ROLE || Lead ServiceNow
    # Admin @ Location: ... onsite role" -- confirmed real case, note the
    # common recruiter typo "ONISTE" for "ONSITE", an adjacent-letter
    # transposition): a bare workmode word immediately followed by "role"
    # ("Onsite Role", "Remote Role") is a distinct leading-filler shape
    # from both the bare-workmode check (_bare_workmode_segment, which
    # requires the workmode word ALONE) and this pattern's existing
    # entries. Lists the two common real-world misspellings explicitly
    # alongside the correct spelling.
    r'(?:on\s*-?\s*(?:site|iste|stie|siet)|remote|hybrid)\s+role)\s*$'
)

# BUG FIX ("role: 'Only Dallas/Fort Worth, TX will be considered'" and
# "role: 'W2/ C2c'" / "role: 'Hiring W2/ C2c'" — both confirmed on real
# requirement rows): _GENERIC_SUBJECT_ROLE_PATTERN above only recognizes
# generic MARKETING filler segments ("Immediate Openings", "JD") — it has
# no concept of an ELIGIBILITY/RESTRICTION segment ("Only local
# candidates will be considered", "No relocation") or a bare
# employment-type segment ("W2/ C2c", "C2C only"), both extremely common
# as the FIRST pipe-separated segment in a multi-segment subject line,
# ahead of the real title. Recognizes both shapes so the segment-picking
# loop in role_from_subject() below can skip them and try the next
# segment instead, the same way it already skips a marketing-filler
# segment.
_RESTRICTION_SUBJECT_SEGMENT_PATTERN = re.compile(
    r'(?i)^(?:'
    r'(?:hiring|need|urgent(?:ly)?|only)?\s*'
    r'(?:w2|c2c|corp\s*-?\s*to\s*-?\s*corp|1099|full\s*-?\s*time|fte)'
    r'(?:\s*(?:[/,]|or|and)\s*(?:w2|c2c|corp\s*-?\s*to\s*-?\s*corp|1099|full\s*-?\s*time|fte))*'
    r'|'
    r'only\b.*\bwill\s+(?:not\s+)?be\s+considered\b.*'
    r'|'
    r'.*\bwill\s+not\s+be\s+considered\b.*'
    r'|'
    r'no\s+relocation.*'
    r'|'
    r'local(?:s)?\s+only|only\s+local(?:s)?(?:\s+candidates?)?|need\s+only\s+local.*'
    r'|'
    # BUG FIX ("role: 'to Mt. Laurel, NJ only'" from "Local to Mt.
    # Laurel, NJ only // Data Modeler" — confirmed on a real requirement
    # row): a "Local to <place> only" eligibility-restriction segment
    # wasn't covered by the bare "local(s) only" alternative above (that
    # one only matches "local only"/"only local", not "local TO <a
    # place> only" with a place name in between).
    r'local\s+to\s+.*\bonly\b.*'
    r'|'
    # BUG FIX ("role: 'Contract Role'" from "Contract Role // Pega
    # Developer + Certified LSA // ..." — confirmed on a real requirement
    # row): a bare "<employment-type> Role" segment (no real title, just
    # announcing the engagement type) commonly sits as its own segment
    # ahead of the real title in a "//"-or-"|"-separated subject.
    r'(?:contract|full\s*-?\s*time|part\s*-?\s*time|c2c|w2|1099|direct\s*hire)\s+role'
    r'|'
    # BUG FIX ("role: 'Direct'" from "Direct Client ::Software Quality
    # Assurance Engineer III :::Lake Forest, IL" — confirmed on a real
    # requirement row): "Direct Client" (announcing the sourcing/vendor
    # relationship) is exactly the same kind of non-title filler segment
    # as the employment-type ones above, just a different common phrase.
    r'direct\s+client'
    r')\s*$'
)

ROLE_PATTERNS = [
    # BUG FIX ("role: 'At least 6-8 years of overall IT experience,
    # including at least 4...'" from a multi-posting email — confirmed on
    # a real VLink requirement row, and independently corrupting the
    # multi-posting segmenter too): every label pattern below used a bare
    # trailing \s* right after the ":"/"-" separator. \s* matches ANY
    # whitespace including newlines, so when a label sits at the END of
    # its own line with nothing after it on that line (e.g. "...to be
    # successful in this role:" followed by a BLANK line then a bullet
    # list), \s* silently skipped straight across the blank line and
    # landed on the next bullet's text as if it were the label's value —
    # capturing a random qualifications bullet as the "role". Restricted
    # every pattern's trailing separator to same-line whitespace plus AT
    # MOST one newline ([ \t]*\n?[ \t]*) — this still supports the common
    # "Label:\n<value on the very next line>" template (single newline,
    # no gap) but can no longer cross an actual blank line to reach
    # unrelated content further down. Same fix applied uniformly to every
    # entry in this list since all of them shared the identical bug.
    r'(?i)\bjob\s*title\s*[:\-][ \t]*\n?[ \t]*(.+)',
    # BUG FIX ("role: 'Client Location'" / role missing entirely — from a
    # two-column recruiter HTML table converted to plain text, e.g. "Job
    # Title" / "Business Analyst with Capital Markets & IBOR" as separate
    # table cells): cleaner.py's HTML-to-text conversion joins same-row
    # table cells with a plain SPACE, not a colon — so a label cell right
    # next to its value cell produces a line like "Job Title Business
    # Analyst with Capital Markets & IBOR" with NO colon/dash separator
    # at all. Every "job title"/"title" pattern above and below requires
    # an explicit [:\-], so this extremely common table-template shape
    # never matched any of them and role extraction fell through to a
    # worse fallback. Anchored to the START of a line (so it can never
    # fire on an incidental mid-sentence "job title" mention) and
    # requires the very next thing to be a capitalized word — the same
    # shape a real title always has, and prose never does right after
    # this exact two-word phrase with no punctuation at all.
    r'(?i)(?:^|\n)[ \t]*job\s*title\b[ \t]+(?=[A-Z])(.+)',
    r'(?i)\bjob\s*role\s*[:\-][ \t]*\n?[ \t]*(.+)',
    r'(?i)\bposition\s*[:\-][ \t]*\n?[ \t]*(.+)',
    # BUG FIX ("role parsed as an ordinary sentence, e.g. 'if so, we can
    # connect and speak further'"): the dash form used to accept ANY
    # text after "role -", including plain sentence punctuation ("...
    # interested in this role - if so, we can connect...") — completely
    # unrelated to a real "Role: <title>" label, but first_match() picks
    # whichever match starts earliest in the document across ALL
    # patterns, so this false match at "role -" in ordinary prose won
    # over the real "Role: <title>" label appearing later in the same
    # email. The colon form is a much stronger, more deliberate label
    # signal (recruiters don't habitually write "role:" as a sentence
    # connector) and stays fully permissive; the dash form now requires
    # the captured value start with a capital letter — the same guard
    # CLIENT_PATTERNS already uses for this exact class of problem — a
    # real job title ("Java Developer") is capitalized, an ordinary
    # sentence continuation ("if so, we can connect") is not.
    r'(?i)\brole\s*:[ \t]*\n?[ \t]*(.+)',
    # BUG FIX ("*Role*- *Databrick Architect*" — the label-side asterisk
    # is already handled by _LABEL_ASTERISK_RE in normalize_text(), but
    # the VALUE can independently be wrapped in its own emphasis too,
    # e.g. "Role- *Databrick Architect*". The (?-i:[A-Z]) capital-letter
    # guard existed specifically to reject ordinary sentence continuations
    # (see this pattern's own BUG FIX above) — but it looked at the
    # literal first character, and a leading "*" there isn't a capital
    # letter, so a perfectly good, capitalized real title got rejected
    # for the wrong reason. Tolerates one optional leading "*" before the
    # capital-letter check without capturing it, so sanitize_text()'s own
    # existing trailing/leading-asterisk stripper (see its own comment)
    # still cleans up the matching close/trailing "*" as before.
    # BUG FIX ("role: 'Based Access Control'" from an ordinary bullet
    # "o Role-Based Access Control" describing a security concept, not a
    # job-title label — confirmed on a real requirement row): \s* before
    # AND after the dash allows ZERO whitespace on both sides, so any
    # hyphenated compound word starting with "role" ("Role-Based",
    # "Role-Driven", ...) matched just as well as a genuine "Role -
    # <title>" label. A real label always has at least one whitespace
    # character adjacent to the dash somewhere ("Role - Title", "Role-
    # Title", "Role -Title"); a hyphenated compound adjective has NONE
    # on either side. Requires whitespace before the dash OR after it
    # (not necessarily both), which still matches every real-world label
    # spacing style seen in this codebase's other BUG FIX examples while
    # rejecting the zero-space compound-word case.
    r'(?i)\brole(?:\s+-|-\s)[ \t]*\n?[ \t]*\*?((?-i:[A-Z]).+)',
    r'(?i)\bopening\s*[:\-][ \t]*\n?[ \t]*(.+)',
    # BUG FIX ("Title – SAP BTP DMS Data Archiving Consultant" parsed as
    # UNKNOWN, or fell through entirely to a poor-quality subject-line
    # guess): a bare "Title:"/"Title –" label — no "Job" prefix — was
    # completely missing from this list. It's an extremely common
    # recruiter template label on its own (this exact real email uses
    # it), not just as part of "Job Title:". Allowing a dash too (not
    # colon-only like "requirement" below) since "Title – <role>" is the
    # more common form in practice, and "title" is specific enough a word
    # that it doesn't share "requirement"'s false-positive risk from
    # generic prose.
    r'(?i)\btitle\s*[:\-][ \t]*\n?[ \t]*(.+)',
    # BUG FIX ("role: 'Confirmation that the candidate agrees to the
    # 3-day/week hybrid schedule at 4'" from an "Onsite Requirement:"
    # numbered-checklist item deep in the body — confirmed on a real
    # requirement row): "requirement" is a generic enough word that it
    # shows up as the tail of all sorts of OTHER labels ("Onsite
    # Requirement:", "Interview Requirement:", "System Requirement:")
    # that have nothing to do with announcing the job title — \b before
    # "requirement" only checks it's not glued to a letter, not that
    # it's the START of the label. Anchored to the actual start of a
    # line (only leading horizontal whitespace allowed before it) so it
    # can only match a genuine standalone "Requirement:" label, never
    # one of these qualified variants sitting mid-checklist.
    # subject+body combined (full_text) when the body alone has no
    # labeled title, so this pattern matching that incidental subject-
    # line dash won every time — extracting whatever followed the dash
    # (a location, a client, anything) as if it were the job title.
    # Restricting to a colon only keeps the genuine "Requirement:
    # <title>" label case working while no longer firing on ordinary
    # prose that just happens to contain "requirement -".
    r'(?i)(?:^|\n)[ \t]*requirement\s*:[ \t]*\n?[ \t]*(.+)',
    # BUG FIX ("Hiring: Salesforce FSC (Financial Services Cloud)Developer"
    # fell through to a poor/UNKNOWN result — the real title was sitting
    # right there behind an unrecognized label): "Hiring:" is a common
    # recruiter-template label announcing the role, distinct from generic
    # "we are hiring" marketing prose. Colon-only, same caution as
    # "requirement" above — "hiring" is common enough in ordinary dash-
    # separated marketing phrasing ("Now Hiring - Apply Today!") that
    # allowing a dash here would risk the same false-positive class that
    # fix exists to prevent; a colon is a much stronger, more deliberate
    # label signal.
    r'(?i)\bhiring\s*:[ \t]*\n?[ \t]*(.+)',
    # BUG FIX ("Role Name: Gemini Enterprise SME/Lead" / "Role Name:
    # Guidewire PolicyCenter BSA Lead" fell through entirely, letting a
    # multi-posting email's own "Position -      1." SEQUENCE NUMBER
    # label win instead — see first_match()'s bare-number guard for that
    # half of the fix): "Role Name:" is a distinct, common template label
    # (not caught by the bare "role" pattern above, which requires "role"
    # immediately followed by the colon/dash — "Name" sitting in between
    # doesn't match \s*). Allowing a dash too, matching "title"/"role"
    # above — "role name" is specific/unambiguous enough that it doesn't
    # share "requirement"/"hiring"'s generic-prose false-positive risk.
    r'(?i)\brole\s*name\s*[:\-][ \t]*\n?[ \t]*(.+)',
]

# BUG FIX: some JD templates render section headers ("Good to Have",
# "Work & Interview Requirements", "Key Responsibilities", "Position
# Overview", etc.) as bold/prominent standalone lines structurally
# similar to a real "Role:"/"Job Title:" label -- sometimes even as
# bullet-list items visually indistinguishable from real content once
# flattened to plain text. The AI extractor has repeatedly been observed
# picking one of these (or a sentence fragment sitting under one)
# instead of the real, correctly-labeled title elsewhere in the same
# email -- confirmed on multiple real emails where first_match(
# ROLE_PATTERNS, ...) reliably found the correct labeled title, but the
# AI's own answer was one of these header phrases instead. Reject a role
# value that's just one of these known generic JD section headers, or
# that's structurally NOT title-shaped at all (starts with a digit, or
# with a common sentence-lead word a real job title essentially never
# starts with, e.g. "2 days onsite every week in Raleigh, NC" or "Work &
# Interview Requirements"), so the regex fallback chain below gets a
# chance to find the real title instead.
_GENERIC_ROLE_SECTION_PATTERN = re.compile(
    r'(?i)^(?:'
    r'good to have|nice to have|key responsibilities|roles?\s*(?:and|&)\s*responsibilities|'
    r'position overview|role overview|about (?:the|this) role|'
    # BUG FIX ("role: 'Required Skills & Experience'" from a Snowflake
    # Cortex Developer JD): only the "& Expertise" wording was recognized --
    # "& Experience" is at least as common a heading for this exact section
    # and didn't match at all, so the anchored pattern failed on the whole
    # line and let the header through as if it were a real title.
    r'required skills(?:\s*(?:and|&)\s*(?:experience|expertise))?|required qualifications|preferred qualifications|'
    r'core skills|key skills|must[\s\-]have skills|soft skills|technical skills|'
    r'work\s*(?:and|&)\s*interview requirements|'
    r'experience\s*(?:and|&)\s*qualifications|'
    r'key success metrics|program governance(?:\s*(?:and|&)\s*execution)?|'
    r'technical leadership(?:\s*(?:and|&)\s*innovation)?|'
    r'architecture(?:\s*(?:and|&)\s*consulting)?\s*skills|customer[\s\-]facing skills|'
    r'security assessment(?:\s*(?:and|&)\s*maturity evaluation)?|'
    r'job description|job summary|position summary|position description|'
    r'responsibilities|requirements|qualifications|overview|summary'
    r')\s*$'
)
# Words a real job title essentially never starts with -- used as a
# structural (not phrase-exact) backstop for the pattern above, since new
# JD templates keep introducing new header phrasing we can't fully
# enumerate in advance.
_ROLE_SENTENCE_LEAD_WORDS = {
    'work', 'must', 'the', 'this', 'our', 'we', 'note', 'please',
    'candidate', 'candidates', 'good', 'nice', 'day', 'days',
    # BUG FIX ("role: 'Hi Sir/Madam'" / "role: 'Hi Bench Team'" / "role:
    # 'Hi'" — confirmed on real requirement rows): an email's opening
    # salutation line is sometimes mistaken for the job title when it's
    # the first bold/lead line in the body. A real job title is never a
    # greeting.
    'hi', 'hello', 'hey', 'dear', 'greetings',
    # BUG FIX ("role: \"don't share me DevOps Profile\"" — confirmed on a
    # real requirement row, from a standalone bold disclaimer line at the
    # very top of the body): an instruction/disclaimer sentence, never a
    # real title.
    "don't", 'dont',
}

# BUG FIX ("role: 'Developer'" from a "Role name:      Developer" template
# line, real title "Oracle EBS technical solutions R12 with emphasis on
# O2C" sitting in the subject/body instead): some recruiter templates
# reuse a role label to mean job LEVEL/CATEGORY rather than the actual
# title -- a bare single generic category noun with no other qualifier is
# a placeholder, not a specific title, even though it's technically a
# real job-title word (unlike the section-header phrases above). A
# genuinely specific title essentially always has at least one qualifier
# in front ("Senior Developer", "Full Stack Developer") -- the BARE noun
# alone, and only when it's the entire value, is the signal this is a
# category placeholder rather than a real answer.
_GENERIC_BARE_ROLE_WORDS = {
    'developer', 'engineer', 'architect', 'administrator', 'analyst',
    'consultant', 'manager', 'lead', 'programmer', 'specialist',
    'designer', 'tester', 'scientist', 'scrum master',
}


def _looks_like_generic_role_header(role_value: Optional[str]) -> bool:
    """True when `role_value` is a known generic JD section-header phrase,
    is just a bare job-category placeholder word, or is structurally not
    title-shaped (see BUG FIX comments above), rather than a real job
    title."""
    if not role_value:
        return False
    candidate = role_value.strip().rstrip('.').strip()
    if not candidate:
        return False
    if _GENERIC_ROLE_SECTION_PATTERN.match(candidate):
        return True
    if candidate.lower() in _GENERIC_BARE_ROLE_WORDS:
        return True
    first_word = candidate.split()[0].lower().strip(',.:;')
    if first_word.isdigit():
        return True
    if first_word in _ROLE_SENTENCE_LEAD_WORDS:
        return True
    return False


# BUG FIX ("role: 'Interview Mode: Video'" / "role: 'Visas: H1B, H4,
# USC, TN & L2'" — both confirmed on real requirement rows, one from a
# Steneral ServiceNow email with a stack of bold "Label: Value" lines
# including a real "Role:" line, the other from a Fiona Solutions email
# with just three clean labeled lines "Role:"/"Visas:"/"Location:" in
# that exact order): in both cases the correct, unambiguous "Role:"
# label was sitting right there in the email and the regex fallback
# chain finds it correctly on its own -- these two specific failures
# only reproduce through the AI extraction stage, which sits in front of
# the regex fallback and can't be directly patched with a regex change.
# This is a generic defensive backstop instead: whatever produced the
# role value (AI or regex), reject it if that EXACT text also appears as
# the value of one of these other, well-known non-role labels elsewhere
# in the same email -- a real job title is essentially never character-
# for-character identical to a visa list, an interview mode, a work-auth
# statement, or a duration/rate, so this can't false-reject a genuine
# title while still catching exactly this failure class regardless of
# which stage produced it.
_NON_ROLE_LABEL_VALUE_PATTERN = re.compile(
    r'(?i)(?:^|\n)[ \t]*(?:visas?|interview\s*mode|work\s*auth(?:orization)?|'
    r'duration|contract(?:\s*length)?|pay\s*rate|bill\s*rate|rate|'
    # BUG FIX ("role: 'Client Name: Virtusa/JPMC'" -- confirmed real
    # case): this watch-list already caught a role value that was really
    # the value of Visas:/Interview Mode:/Work Auth:/etc, but "client" and
    # "client name" -- an extremely common adjacent label in the exact
    # same stacked "Job Title:"/"Client Name:"/"Location:" template shape
    # -- were never included, so the AI extractor grabbing that line
    # whole (label included) instead of the real "Job Title:" line above
    # it went completely uncaught by this defensive backstop.
    r'employment\s*type|client\s*location|end\s*client|client\s*name|client)\s*[:\-][ \t]*([^\n]+)'
)


def _role_echoes_non_role_label(role_value: Optional[str], text: str) -> bool:
    """True when `role_value` is actually the value of some OTHER labeled
    field (Visas:, Interview Mode:, Work Authorization:, Duration:, ...)
    elsewhere in the same email, rather than a real job title -- see the
    BUG FIX comment on _NON_ROLE_LABEL_VALUE_PATTERN above."""
    if not role_value or not text:
        return False
    role_key = role_value.strip().lower().rstrip('.')
    if not role_key:
        return False
    for m in _NON_ROLE_LABEL_VALUE_PATTERN.finditer(text):
        candidate = sanitize_text(m.group(1))
        if candidate and candidate.strip().lower().rstrip('.') == role_key:
            return True
        # BUG FIX ("role: 'Client Name: Virtusa/JPMC'" -- confirmed real
        # case): the check above only ever compares role_value against
        # the bare VALUE half of the other field's "Label: Value" line
        # (e.g. just "Virtusa/JPMC") -- but the actual observed failure
        # had the AI extractor grabbing the WHOLE line, label included,
        # as the role. Also compares against the full matched line (with
        # normal whitespace collapsed) so this label-included form is
        # caught too, not just the bare-value form.
        full_line = sanitize_text(m.group(0))
        if full_line and full_line.strip().lower().rstrip('.') == role_key:
            return True
    return False

CLIENT_PATTERNS = [
    # BUG FIX ("client: ':Software Quality Assurance Engineer III :::Lake
    # Forest, IL...'" — the label matched on the FIRST colon of a "::"
    # subject-segment delimiter (e.g. "Direct Client ::Software Quality
    # Assurance..."), then captured everything from the SECOND colon
    # onward as if it were the client name — confirmed on a real
    # requirement row): every pattern below now requires the colon/dash
    # separator NOT be immediately followed by another colon. A genuine
    # "Client:" label is never itself followed by a second colon; a "::"
    # segment delimiter always is.
    r'(?i)\bend\s*client\s*[:\-](?!:)[ \t]*\n?[ \t]*(.+)',
    # BUG FIX ("client: None" -- fell through entirely, letting a worse
    # fallback further down in the extraction chain win instead --
    # confirmed on a real requirement row, "Client Name: Virtusa/
    # Confidential"): "Client Name:" is a common template label distinct
    # from the bare "Client:" pattern below (which requires "client"
    # immediately followed by the colon -- "Name" sitting in between
    # doesn't match \s*).
    r'(?i)\bclient\s*name\s*[:\-](?!:)[ \t]*\n?[ \t]*(.+)',
    r'(?i)\bclient\s*[:\-](?!:)[ \t]*\n?[ \t]*(.+)',
    r'(?i)\bcustomer\s*[:\-](?!:)[ \t]*\n?[ \t]*(.+)',
    r'(?i)\bimplementation\s*(?:partner)?\s*[:\-](?!:)[ \t]*\n?[ \t]*(.+)',
    # "Client is Zensar" -- no colon at all, just prose. Tightly bounded to
    # 1-4 capitalized words (typical company-name shape) so it stops
    # naturally at the client name instead of running into the rest of the
    # sentence like ".+" would (there's no colon-based field boundary to
    # crop at here).
    #
    # BUG FIX: the [A-Z] here was meant to require the captured text
    # START with a capital letter -- a guard against matching ordinary
    # prose like "Client is seeking an experienced Senior Product
    # Manager..." as if "seeking" were a company name. But this whole
    # pattern (and first_match()'s own re.search call) apply
    # re.IGNORECASE, which makes [A-Z] match lowercase letters too --
    # silently defeating the one thing the pattern was designed to check.
    # (?-i:[A-Z]) scopes case-sensitivity back on for just that one
    # character, regardless of the IGNORECASE flag applied around it.
    #
    # BUG FIX ("client: 'Position'" from "...with one of our client\n\n
    # Position: Senior Java developer with AI\n\nLocation: ..."): neither
    # of these two prose patterns had any bound on the \s+ gap between the
    # trigger phrase and the captured value, so a genuinely client-less
    # sentence followed by a blank line and a totally unrelated later
    # paragraph (here, a "Position:" field label) read as if that next
    # capitalized word were the client name named right after the trigger
    # phrase. A real "Client is Zensar" / "Implementation Partner is TCS"
    # always continues within the same sentence/paragraph -- add a
    # negative lookahead rejecting the match outright if a blank line
    # (paragraph break) sits between the trigger phrase and its value.
    r'(?i)\bclient\s+is(?!\s*\n\s*\n)\s+((?-i:[A-Z])[a-zA-Z0-9&.\-]*(?:\s+(?-i:[A-Z])[a-zA-Z0-9&.\-]*){0,3})',
    # "Implementation Partner is TCS" -- same prose shape/guard as above.
    r'(?i)\bimplementation\s*(?:partner)?\s+is(?!\s*\n\s*\n)\s+((?-i:[A-Z])[a-zA-Z0-9&.\-]*(?:\s+(?-i:[A-Z])[a-zA-Z0-9&.\-]*){0,3})',
]

# BUG FIX: used by the client-echo guards below to distinguish "client
# value was grounded in an actual explicit label somewhere in the email"
# from "client value was inferred/guessed with no real client mention at
# all". Without this distinction, the echo guards below would incorrectly
# null out perfectly legitimate clients whose name happens to also be a
# common tech vendor/skill (e.g. a genuine "Client: Oracle" next to
# "Skills: Oracle, PL/SQL", or "Client: SAP" next to a role like "SAP
# FICO Consultant") -- the value isn't an echo in that case, it's just a
# client whose name is also a widely-used technology name. This only
# needs to detect that SOME client-ish label exists somewhere in the
# text, not extract its value (CLIENT_PATTERNS already does that).
_CLIENT_LABEL_PRESENT_RE = re.compile(
    # BUG FIX ("client: 'Business Architect'" role-echo not caught): the
    # colon/hyphen branch had no check on what follows the separator, so
    # an ordinary compound word like "customer-centric" or "client-facing"
    # (hyphen glued directly onto the next word, no real field-label
    # intent at all) satisfied it just as well as a genuine "Client: Acme"
    # label. That false "a label IS present" reading disabled the
    # role-echo guard this regex gates, letting the role text survive as
    # the client value with nothing to reject it. A real label's
    # separator is never immediately followed by a lowercase letter
    # glued onto another word -- only by whitespace, a capital letter, or
    # end of text -- so require that here too. (?-i:[a-z]) scopes
    # case-sensitivity back on for the lookahead specifically -- the
    # surrounding (?i) would otherwise make [a-z] match uppercase too,
    # silently defeating the check (same class of bug already fixed once
    # in CLIENT_PATTERNS below via the identical (?-i:...) technique).
    r'(?i)\b(?:end\s*client|client|customer|implementation\s*(?:partner)?)\s*[:\-](?!(?-i:[a-z]))'
    r'|\bclient\s+is\b'
    r'|\bimplementation\s*(?:partner)?\s+is\b'
)

# BUG FIX ("client: 'Requisition List Client Name Req'" from an HTML
# table's own flattened column-header row -- confirmed real case): role
# already has _looks_like_generic_role_header() to reject a value that's
# just a section/column header rather than real content -- client had no
# equivalent, so when the AI extractor was handed a flattened HTML table
# (cells joined by spaces, losing their row/column structure) it could
# echo the table's own header text back as if it were an actual client
# name. Checked against a fixed set of known ATS/recruiter-template
# column-header phrases; a real client name is essentially never
# character-for-character identical to one of these.
_GENERIC_CLIENT_HEADER_PHRASES = {
    'client name', 'end client', 'requisition list', 'req #', 'req',
    'job title', 'location', 'rate', 'c2c rate', 'bill rate', 'pay rate',
    'notes', 'on your w2', 'on your w2?', '# of positions',
    'number of positions', 'client', 'customer',
}


def _looks_like_generic_client_header(client_value: Optional[str]) -> bool:
    """True when `client_value` is just an ATS/recruiter-template column
    header (or a run of several concatenated ones) rather than a real
    client/company name -- see _GENERIC_CLIENT_HEADER_PHRASES' comment."""
    if not client_value:
        return False
    candidate = client_value.strip().rstrip('.').strip().lower()
    if not candidate:
        return False
    if candidate in _GENERIC_CLIENT_HEADER_PHRASES:
        return True
    # A run of 2+ known header phrases concatenated with nothing but
    # whitespace between them ("Requisition List Client Name Req") is
    # just as clearly not a real client name as any single one alone.
    words = candidate.split()
    if len(words) >= 3:
        matched_phrase_words = 0
        remaining = candidate
        for phrase in sorted(_GENERIC_CLIENT_HEADER_PHRASES, key=len, reverse=True):
            if phrase in remaining:
                matched_phrase_words += len(phrase.split())
                remaining = remaining.replace(phrase, ' ', 1)
        if matched_phrase_words >= max(2, len(words) - 1):
            return True
    return False

LOCATION_PATTERNS = [
    # BUG FIX ("location: 'Linkdein :'" from a "Your visa :\nYour location
    # :\nLinkdein :" fill-in-the-blank template -- confirmed real case):
    # every pattern here used a bare \s* after the label's colon/dash,
    # which matches ANY whitespace including newlines -- so a genuinely
    # BLANK labeled line (the template asking the candidate to fill in
    # their own location, nothing typed in after the colon) let the match
    # slide straight across the blank spot and down onto the NEXT line's
    # label, capturing that whole label as if it were the location value.
    # ROLE_PATTERNS hit this identical failure mode and was already fixed
    # by restricting the separator to same-line whitespace plus at most
    # one newline ([ \t]*\n?[ \t]*) -- this was never applied here.
    # Beyond that, a negative lookahead rejects the match outright when
    # the captured line itself is just a short "Word(s) :" shape with
    # nothing else on it -- a real location value never ends in a bare
    # trailing colon like that, only another field's empty label does, so
    # this still catches the blank-template case even when crossing the
    # single allowed newline.
    r'(?i)\bwork\s*locations?\s*[:\-][ \t]*\n?[ \t]*'
    r'(?!(?:[A-Za-z][a-zA-Z]*\s*){1,3}[:\-][ \t]*(?:\n|$))(.+)',
    r'(?i)\bplace\s*of\s*work\s*[:\-][ \t]*\n?[ \t]*'
    r'(?!(?:[A-Za-z][a-zA-Z]*\s*){1,3}[:\-][ \t]*(?:\n|$))(.+)',
    # BUG FIX: plural "Locations -Remote" was missed entirely (returned
    # null) because the pattern only accepted the singular form.
    r'(?i)\blocations?\s*[:\-][ \t]*\n?[ \t]*'
    r'(?!(?:[A-Za-z][a-zA-Z]*\s*){1,3}[:\-][ \t]*(?:\n|$))(.+)',
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
    # BUG FIX: the whitespace before "[:\-]" used to be \s* (matches
    # newlines too), which let the pattern skip across several BLANK
    # LINES to reach an unrelated "--" email-signature delimiter far
    # below the last mention of "skills" and treat that as "the hyphen
    # after skills" -- silently capturing the entire sender signature
    # block (name, company, address) as if it were the skills list.
    # [ \t]* only matches same-line whitespace, so the colon/hyphen must
    # actually appear on (or right after) the same line as the label.
    # BUG FIX ("skills: ['through self-study', 'training', 'exploring new
    # frameworks', 'tools', 'Contribute innovative ideas', 'user
    # experience']" from a VBeyond iOS Developer email — confirmed on a
    # real requirement row): every "<adjective> skills" label pattern in
    # this list had an OPTIONAL colon/dash ([:\-]?), so it matched ANY
    # incidental mid-sentence mention of the phrase, not just a real
    # section header. The email's own "Continuous Learning and
    # Innovation" bullet said "...improve your technical skills through
    # self-study, training, and exploring new frameworks and tools." —
    # nowhere near a real skills list — and this pattern happily matched
    # right there, well before the actual "Requirements:" section further
    # down, so THAT sentence fragment won as "the skills section" instead.
    # Anchored every one of these patterns to the START of a line (same
    # (?:^|\n) convention already used by STOP_PATTERN elsewhere in this
    # file) and required the label be followed by an actual colon/dash OR
    # sit alone at the end of its line (a bare header with the list
    # starting on the next line) — never free-floating mid-sentence. A
    # real "Technical Skills:" / "Required Skills" header is always its
    # own line; ordinary prose mentioning "technical skills" never starts
    # a line with those exact words.
    r'(?i)(?:^|\n)[ \t]*primary\s*skills?\b[ \t]*(?:[:\-]\s*|(?=\n|$))[ \t]*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    r'(?i)(?:^|\n)[ \t]*required\s*skills?\b[ \t]*(?:[:\-]\s*|(?=\n|$))[ \t]*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    # BUG FIX ("skills undercounted — only a short 'Mandatory Skills'
    # one-liner used, a much richer 'Skill Requirements' bulleted section
    # further down in the same email completely ignored"): "Skill
    # Requirements" (noun-noun, skill THEN requirements) is the reverse
    # word order from every other pattern in this list ("required
    # skills", "technical skills", "key skills" — all adjective/label
    # THEN "skills"), so it matched none of them and was never even
    # tried as a candidate section.
    r'(?i)(?:^|\n)[ \t]*skill\s*requirements?\b[ \t]*(?:[:\-]\s*|(?=\n|$))[ \t]*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    r'(?i)(?:^|\n)[ \t]*technical\s*skills?\b[ \t]*(?:[:\-]\s*|(?=\n|$))[ \t]*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    r'(?i)(?:^|\n)[ \t]*key\s*skills?\b[ \t]*(?:[:\-]\s*|(?=\n|$))[ \t]*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    # BUG FIX: no trailing \b/plural meant this matched only the first 8
    # chars of "skillsets:", leaving a stray unconsumed "s" before the
    # real colon -- since the colon check here is optional, the whole
    # capture then silently ran from right after that stray "s" instead
    # of after the real colon, swallowing the entire rest of the JD
    # (Qualifications, Responsibilities, everything) as if it were
    # "skills". sets?\b makes both "skill set:" and "skillsets:" resolve
    # to the real colon correctly.
    r'(?i)(?:^|\n)[ \t]*skill\s*sets?\b[ \t]*(?:[:\-]\s*|(?=\n|$))[ \t]*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    r'(?i)(?:^|\n)[ \t]*tech(?:nology|nical)?\s*stack\b[ \t]*(?:[:\-]\s*|(?=\n|$))[ \t]*[•\-\*\u2022]?\s*(?!\s*(?:&|and\b))(.+)',
    # Bare "skills:" kept LAST and colon-REQUIRED (not optional) — this one is
    # generic enough that making it colon-optional would risk matching the
    # word "skills" inside unrelated sentences ("strong problem-solving and
    # debugging skills.") anywhere in the body.
    r'(?i)skills?[ \t]*[:\-]\s*\n?\s*[•\-\*\u2022]?\s*(.+)',
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

# BUG FIX (used by BARE_LOCATION_PATTERN below): a real full state name is
# always exactly one of these 51 known names -- built directly from
# US_STATE_NAMES itself (title-cased, longest-first so a genuine two-word
# state like "New York" matches whole rather than stopping at "New") so
# the two can never drift out of sync. Used in place of a generic
# "run of Title-Case words" pattern, which had no way to tell a real
# state name apart from any other run of Title-Case recruiter prose that
# happened to follow it (e.g. "...Missouri Need Only Local Candidate for
# Face to Face Interview" -- confirmed real case -- got swallowed whole
# as if all of it were the state name, corrupting resolve_state_code()
# and causing the entire match, and the location cleanup that depends on
# it, to fail).
_FULL_STATE_NAME_ALTERNATION = '|'.join(
    re.escape(_name.title())
    for _name in sorted(US_STATE_NAMES.keys(), key=len, reverse=True)
)

# Matches "City, TX" / "City TX" / "City, Texas" — resolved through resolve_state_code()
BARE_LOCATION_PATTERN = re.compile(
    r'\b([A-Z][a-zA-Z]+(?:[ \-][A-Z][a-zA-Z]+){0,2})\s*,?\s*'
    r'([A-Z]{2}\b|' + _FULL_STATE_NAME_ALTERNATION + r')'
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


# A "City, ST" match can accidentally land inside a work-authorization /
# visa-status enumeration -- e.g. "H1-B, H4-EAD, TN, E3, L2, STEM OPT",
# where "TN" is the TN (NAFTA professional) visa category rather than the
# state of Tennessee, and the preceding token ("EAD") coincidentally looks
# like a city name in a "City, ST" shape. Rejects a match when 2+ distinct
# visa-status tokens appear in a window around it -- a real address is
# extremely unlikely to sit in the middle of a run of visa abbreviations.
_VISA_STATUS_TOKENS = re.compile(
    r'(?i)\bh-?1-?b\b|\bh-?4\b|\bh-?4[\s\-]*ead\b|\bstem\s*opt\b|\bopt\b|'
    r'\bcpt\b|\bgc[\s\-]*ead\b|\bgc\b|\bl-?1\b|\bl-?2\b|\be-?3\b|\btn\b|'
    r'\bu\s*visa\b|\busc\b'
)


def _looks_like_visa_status_context(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - 40):end + 40]
    hits = {m.group(0).lower() for m in _VISA_STATUS_TOKENS.finditer(window)}
    return len(hits) >= 2


# ---------------------------------------------------------------------------
# Work Authorization (ported from rap_python_cron's identical addition)
# ---------------------------------------------------------------------------
# BUG FIX: this project's Requirement model never had a work_authorization
# column at all, and there was no extraction logic anywhere in this file --
# so the field was silently unavailable on every row ever saved, regardless
# of what the email actually said (and plenty of recruiter emails state
# this explicitly, e.g. "USC or GC only", "No H1B"). See models.py's own
# comment for the required column addition this pairs with, and dedup.py's
# save_requirement() for where the extracted value gets persisted.

WORK_AUTH_PATTERNS = [
    r'(?i)\bwork\s*authorization\s*[:\-]\s*(.+)',
    r'(?i)\bwork\s*auth\.?\s*[:\-]\s*(.+)',
    r'(?i)\bvisa\s*status\s*[:\-]\s*(.+)',
    r'(?i)\bvisa\s*requirement\s*[:\-]\s*(.+)',
    r'(?i)\bvisa\s*[:\-]\s*(.+)',
    r'(?i)\bauthoriz(?:ation|ed)\s*to\s*work\s*[:\-]\s*(.+)',
]

# Reuses the same visa-abbreviation vocabulary _looks_like_visa_status_context
# already relies on elsewhere, plus a few common spelled-out phrases that
# vocabulary doesn't cover (it was only ever built to recognize an
# abbreviation *run*, not to be read as English).
_WORK_AUTH_TOKEN_PATTERN = re.compile(
    r'(?i)\bno\s+(?:third\s*party\s+)?sponsorship\b|'
    r'\bwithout\s+sponsorship\b|'
    r'\bunrestricted\s+work\s+authorization\b|\bunrestricted\s+visa\b|'
    r'\bus\s+citizens?\b|\bu\.s\.\s+citizens?\b|\bcitizens?\s+only\b|'
    r'\bgreen\s*card\b|'
    r'\bh-?1-?b\s*(?:transfer)?\b|\bh-?4[\s\-]*ead\b|\bh-?4\b|'
    r'\bstem\s*opt\b|\bopt\b|\bcpt\b|\bgc[\s\-]*ead\b|\bgc\b|'
    r'\bl-?1\b|\bl-?2\b|\be-?3\b|\btn\s*visa\b|\busc\b|\bf-?1\b|'
    r'\bany\s+visa\b|\bno\s+visa\s+sponsorship\b'
)


def clean_work_authorization(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = sanitize_text(normalize_text(value))
    if not value:
        return None
    value = sanitize_text(_ANY_TAG_RE.sub(' ', value))
    if not value:
        return None
    value = crop_at_next_field(value)
    if not value or is_email_body(value):
        return None
    if len(value) > 100:
        value = _cap_real_words(value, 15)
    return value or None


def extract_work_authorization(text: str) -> Optional[str]:
    """Best-effort work authorization / visa status extraction.

    Tries a labeled field first ("Work Authorization:", "Visa Status:",
    etc.), then falls back to collecting every recognized visa/citizenship
    token mentioned anywhere in the text (e.g. a bare "USC or GC only"
    sentence with no field label at all) and returning them joined in the
    order they appear. Returns None when nothing is found rather than
    guessing — an absent statement is not the same as "no restriction".
    """
    if not text:
        return None
    labeled = first_match(WORK_AUTH_PATTERNS, text[:6000])
    cleaned = clean_work_authorization(labeled)
    if cleaned:
        return cleaned
    tokens = []
    seen = set()
    # Negation-aware: "No H1B or OPT candidates please, USC/GC only" must
    # not come back as "H1B, OPT, USC, GC" with no indication H1B/OPT are
    # explicitly EXCLUDED. Checks a short window immediately before each
    # token for a negation cue and prefixes "No " onto that token
    # specifically, so excluded and accepted statuses stay distinguishable.
    _NEGATION_CUE = re.compile(
        r'(?i)\b(?:no|not|except|excluding|won\'?t\s+accept|cannot\s+accept)\b'
        r'(?:\s+[\w/\-]+){0,3}?\s*$'
    )
    for m in _WORK_AUTH_TOKEN_PATTERN.finditer(text[:6000]):
        tok = re.sub(r'\s+', ' ', m.group(0)).strip()
        window_before = text[max(0, m.start() - 30):m.start()]
        negated = bool(_NEGATION_CUE.search(window_before)) and not tok.lower().startswith('no ')
        display = f"No {tok}" if negated else tok
        key = display.lower()
        if key not in seen:
            seen.add(key)
            tokens.append(display)
    if tokens:
        return ', '.join(tokens[:8])
    return None


# BUG FIX ("location: 'NEED ONLY LOC, AL'" / "location: 'NEED ONLY
# LOCAL, OR'" — confirmed on a real SailPoint requirement row):
# BARE_LOCATION_PATTERN's "city" group matches ANY run of 1-3
# capitalized-looking words with no vocabulary check at all — so an
# ordinary recruiter boilerplate phrase like "NEED ONLY LOCAL OR NEARBY
# STATE CANDIDATES..." false-matched as a city/state pair purely because
# "OR" (the common English conjunction) is ALSO a valid 2-letter state
# code (Oregon), with "NEED ONLY LOCAL" swallowed as the "city" in front
# of it. The existing reject_first_words mechanism only ever checked the
# FIRST word of the matched city phrase — useless here since the bogus
# match can start one word later ("ONLY LOCAL" + "OR") just as easily
# once the first candidate is rejected. This set covers common
# recruiter/marketing filler words that are never part of a genuine city
# name; a match is now rejected if ANY word in the captured city phrase
# is one of these, not just the first.
_LOCATION_FILLER_WORDS = {
    'need', 'needed', 'only', 'local', 'locals', 'urgent', 'urgently',
    'immediate', 'immediately', 'hiring', 'must', 'will', 'not', 'no',
    'yes', 'and', 'nor', 'candidates', 'candidate', 'apply', 'submit',
    'send', 'looking', 'seeking', 'required', 'requirement', 'requirements',
    'nearby', 'state', 'states', 'or', 'note', 'please', 'thanks', 'regards',
}


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
        city_words = [w.lower() for w in m.group(1).split()]
        first_word = city_words[0] if city_words else ''
        # Reject recruiter filler anywhere in the candidate city.
        has_filler_word = any(w in _LOCATION_FILLER_WORDS for w in city_words)
        # Also reject a job-title category at the end of a fake city match.
        last_word = city_words[-1] if city_words else ''
        if (
            code
            and first_word not in reject_first_words
            and not has_filler_word
            and last_word not in _GENERIC_BARE_ROLE_WORDS
            and not _looks_like_visa_status_context(text, m.start(), m.end())
        ):
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

# BUG FIX ("DevOps Engineer</span></b></span></span></p>" / other literal
# HTML tags leaking into the extracted role): the real fix for THIS is
# upstream — requirements_sync.py/pipeline.py now detect raw HTML sitting
# in body_text and convert it before parsing ever runs (see their own
# _looks_like_html). But relying solely on catching it once, upstream, is
# fragile — any future/unforeseen ingestion path that skips that check
# would reproduce the exact same leak. This is a second, independent
# layer: strip any literal tag-shaped text directly out of clean_role()'s
# output, unconditionally, regardless of which extraction path produced
# it or why. Matches ANY tag shape (opening, closing, or self-closing),
# same pattern as the upstream detector, so it stays in sync with what
# that one recognizes as "this looks like a tag".
_ANY_TAG_RE = re.compile(r'</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>')

NEXT_FIELD_LABELS = [
    'job title', 'job role', 'title', 'position', 'role', 'role type',
    'opening', 'requirement', 'end client', 'client', 'customer',
    # BUG FIX ("Yardi Consultant-Multiple Locations- Dallas..." role
    # captured as "Yardi Consultant-Multiple" — cut off mid-phrase):
    # "locations" alone as a boundary correctly stopped the crop right
    # before "Locations-", but "Multiple" (part of the very common
    # recruiter boilerplate phrase "Multiple Locations", not a real
    # field value) sat one word earlier with nothing telling the crop to
    # stop there instead, so it stayed glued onto the role. Registering
    # the whole two-word phrase as its own boundary — same pattern
    # "work location"/"place of work" already use for compound labels —
    # moves the stop point one word earlier, to right before "Multiple".
    'multiple locations', 'multiple location',
    'work location', 'work locations', 'place of work', 'location',
    'locations', 'pay rate', 'bill rate',
    'compensation', 'rate', 'contract length', 'contract duration',
    'duration', 'primary skills', 'required skills', 'technical skills',
    'key skills', 'skill set', 'skills',
    'experience', 'employment type', 'employment', 'work mode',
    # BUG FIX ("Role: 'Work Model'" — confirmed on a real requirement
    # row): "Work Model" is a real, distinct label variant some vendor
    # templates use as a synonym for "Work Mode" — it wasn't recognized
    # at all, so a bare "Work Model" label line (value on the next line,
    # no colon on this one) passed every check in
    # _looks_like_bare_job_title() and got treated as if it were itself
    # the job title.
    'work model', 'work models',
    # BUG FIX ("location: '...(IP will be tracked through assessment)Work
    # Arrangement: Hybrid...Interview: Onsite...'" / "location: 'Fully
    # Onsite Interview Process: 1 Round...Possibility for Extension:
    # Yes'" -- both confirmed real cases, from HTML-flattened recruiter
    # templates with zero whitespace between adjacent label:value
    # segments): "Work Arrangement", "Interview"/"Interview Process", and
    # "Possibility for Extension" are all common recruiter-template field
    # labels that sit immediately after Location/Work Location in these
    # templates, but none of them were recognized as field boundaries --
    # so a captured Location value ran straight through all of them
    # instead of stopping at the first one, as this list already does for
    # "Duration:"/"Rate:"/etc.
    'work arrangement', 'interview', 'interview process',
    'possibility for extension',
    'work type', 'vendor',
    'recruiter', 'contact', 'phone', 'email', 'responsibilities',
    'qualifications', 'job description', 'role description',
    'role descriptions', 'position description', 'position descriptions',
    'benefits', 'visa', 'type',
    'no. of position', 'no. of positions', 'number of position',
    'number of positions',
    # BUG FIX ("Product Leader Digital Catering SolutionsStart Date:
    # 09/21/2026# of..." — role ran straight through into the next two
    # fields, glued with no space at all): confirmed on a real
    # requirement row. Neither "Start Date" nor the "# of Positions"
    # shorthand (as opposed to the already-recognized "No. of
    # Positions"/"Number of Positions" spelled-out forms) was in this
    # list at all, so crop_at_next_field() had no boundary to stop at,
    # and the glued-label ungluer had nothing to insert a space before
    # either — both problems at once, same as any other missing label
    # here.
    'start date', '# of position', '# of positions',
    # BUG FIX ("Job Title: Forward Deployed Engineer (FDE) - AI/ML & API
    # Integration Domain: Banking / Financial Services" — role kept
    # running straight through into the Domain field instead of stopping
    # before it): "Domain:" is a common recruiter-template label (BFSI/
    # Healthcare/Insurance-domain postings frequently use it right after
    # the title) that was simply missing from this list entirely.
    'domain',
    # BUG FIX ("Location: RemoteNote: Migrate 600GB of data from Oracle
    # to Db2..." — the entire freeform note, plus everything after it
    # including the sender's email address, got swallowed into
    # location): "Note:" is a common freeform recruiter-template field
    # that was missing from this list entirely, so nothing ever stopped
    # a preceding field's capture at it. Registering it here fixes both
    # halves at once — this list also feeds _GLUED_LABEL_PATTERN, so a
    # zero-whitespace glue like "RemoteNote:" gets un-glued to
    # "Remote Note:" too, not just cropped.
    'note',
]

def _label_dash_terminator(label: str) -> str:
    """Regex fragment matching `label` followed by the colon/hyphen that
    marks it as a field boundary. For most labels a bare trailing hyphen
    is safe (recruiters often write "Location- Chicago"), but "domain" is
    also the first half of ordinary compound words like "Domain-Driven
    Design" -- so for that one label specifically, a hyphen only counts
    as the boundary when it's NOT immediately followed by another word
    character (i.e. an actual separator, not part of a compound word).
    """
    esc = re.escape(label)
    if label == 'domain':
        return esc + r'\s*(?::|-(?!\w))'
    return esc + r'\s*[:\-]'


NEXT_FIELD_PATTERN = re.compile(
    r'\b(?:' + '|'.join(_label_dash_terminator(w) for w in NEXT_FIELD_LABELS) + r')',
    re.IGNORECASE,
)

# Un-glue field labels that HTML-collapse has fused onto a preceding word,
# e.g. "TrintechLocation:" or "ArchitectDuration:". Uses longest-label-first
# ordering so multi-word labels ("work location") beat single-word prefixes.
_FIELD_LABEL_ALTERNATION = '|'.join(
    _label_dash_terminator(w) for w in sorted(NEXT_FIELD_LABELS, key=len, reverse=True)
)
_GLUED_LABEL_PATTERN = re.compile(
    r'(?<=[A-Za-z0-9])(?=(?:' + _FIELD_LABEL_ALTERNATION + r'))',
    re.IGNORECASE,
)

SIGNATURE_PATTERN = re.compile(
    r'\b(?:regards|thanks|thank you|best regards|warm regards|sincerely|'
    r'cheers|best,)\b',
    re.IGNORECASE,
)

# BUG FIX ("Cloud Policy as Code EngineerThis is 12+ months in Phoenix, AZ
# (Hybrid)" — title ran straight into the duration/location sentence that
# followed it, then got mid-sentence-truncated by the 12-word cap instead
# of stopping at the real title boundary): a very common recruiter
# template puts the title first, then an UNLABELED sentence like "This is
# <duration> in <location> (<work mode>)" — no "Duration:"/"Location:"
# label at all until much later (if ever), so NEXT_FIELD_PATTERN has
# nothing to crop at. HTML-collapse also frequently glues this sentence
# directly onto the title with zero whitespace ("EngineerThis is..."), so
# this intentionally does NOT require a word boundary before "This" --
# the literal substring match alone is enough to find the seam.
RUNAWAY_SENTENCE_PATTERN = re.compile(
    r'(?i)This\s+(?:is|will\s+be)\b|\bThis\s+(?:position|role|opportunity)\b'
)


# BUG FIX: see the "*Role*-" comment inside normalize_text() below for
# the full story. Fixed, known label vocabulary only -- covers every
# label word ROLE_PATTERNS/SKILLS_PATTERNS/LOCATION_PATTERNS/CLIENT_
# PATTERNS/etc. already recognize, so a stray glued "*" can no longer
# hide a real label from any of them. Two alternatives: a LEADING "*"
# (with an optional trailing one consumed in the same match, e.g. the
# common "*Label*" case) or a lone TRAILING "*" with no leading one
# ("Label*"). Word-boundary anchored so it only strips asterisks
# touching the label word itself, never a same-named word inside other
# content.
_ASTERISK_LABEL_WORDS = (
    r'job\s*title|job\s*role|position|role\s*name|role|title|'
    r'requirement|hiring|opening|location|client|customer|'
    r'required\s*skills?|primary\s*skills?|key\s*skills?|technical\s*skills?|'
    r'skill\s*requirements?|skill\s*sets?|skills?|mandatory\s*skills?|'
    r'duration|rate|pay\s*rate|bill\s*rate|experience|'
    r'work\s*model|work\s*mode|employment\s*type|visas?|interview\s*mode|'
    r'work\s*auth(?:orization)?'
)
_LABEL_ASTERISK_RE = re.compile(
    r'(?i)\*(' + _ASTERISK_LABEL_WORDS + r')\*?|(' + _ASTERISK_LABEL_WORDS + r')\*'
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
    # BUG FIX ("skills: []" / role fell through to a worse fallback — from
    # emails formatting field labels as markdown emphasis, e.g. "*Role*-
    # *Databrick Architect*" or "*Required Skills*" on its own line —
    # confirmed on real requirement rows via batch analysis against
    # production data): every label pattern across this file (ROLE_
    # PATTERNS, SKILLS_PATTERNS, LOCATION_PATTERNS, ...) matches the bare
    # label word with \b word boundaries, but a literal "*" glued directly
    # onto the word (no space) sits BETWEEN the label and its separator/
    # line-boundary, breaking every one of those patterns at once even
    # though a human reads "*Role*-" as obviously meaning "Role:". Strips
    # a SINGLE asterisk immediately touching one of these known label
    # words (on either side) before any label pattern ever runs, so every
    # extractor benefits without needing its own asterisk-tolerant
    # variant. Deliberately restricted to a fixed, known label vocabulary
    # (not "any asterisk-wrapped word") so a genuinely emphasized normal
    # word elsewhere in the JD (e.g. "*Kinaxis RapidResponse*" naming a
    # real skill) is left completely untouched.
    text = _LABEL_ASTERISK_RE.sub(lambda m: m.group(1) or m.group(2), text)
    # Un-glue labels AFTER punct-fold so en-dash variants are already '-'
    text = _GLUED_LABEL_PATTERN.sub(' ', text)
    # BUG FIX ("Endpoint EngineerJob DescriptionWe are looking...", role
    # still came out UNKNOWN even after un-gluing with a plain space):
    # _GLUED_LABEL_PATTERN above only un-glues a label followed by a
    # colon/dash field-boundary marker -- here there's no punctuation at
    # all on either side, title, label, and body just run straight
    # together (a common ATS-template HTML-collapse artifact). A single
    # inserted space fixed readability but left title+label+JD-body all
    # on one giant run-on line with no blank-line boundary anywhere near
    # the front -- exactly the boundary role_from_body_lead()'s "bare
    # title line, then blank line, then prose" heuristic depends on to
    # know where the title ends. "Job/Role/Position Description" is
    # specific and common enough as a fixed template phrase, and
    # functionally IS always a section break (never real title content),
    # to safely insert a full blank-line break around it on sight.
    text = re.sub(
        r'(?<=[a-zA-Z])((?:Job|Role|Position)\s+Descriptions?)(?=[A-Za-z])',
        r'\n\n\1\n\n', text
    )
    # BUG FIX (same email, same root cause, different symptom -- "skills"
    # and "experience" indicators never fired for is_job_requirement_email
    # either): this ATS template glues EVERY bullet item directly onto the
    # next with zero separator throughout the whole JD ("Must-Have
    # SkillsStrong Endpoint...", "5+ years experienceHands-on Kandji...").
    # A GENERAL "insert a space before any uppercase letter" rule would be
    # far too broad -- it would just as eagerly shred legitimate compound
    # tech terms this exact JD also contains verbatim ("macOS",
    # "PowerShell") into "mac OS"/"Power Shell". Scoped to only the two
    # specific, unambiguous section-header words this template glues this
    # way; neither is ever legitimately part of a compound tech term when
    # immediately followed by an uppercase letter with no separator.
    text = re.sub(r'\b(Skills|Experience)(?=[A-Z])', r'\1\n\n', text)
    text = _unglue_leading_city_state(text)
    text = _GLUED_WORKMODE_PATTERN.sub(' ', text)
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
    m = RUNAWAY_SENTENCE_PATTERN.search(value)
    if m:
        cut = min(cut, m.start())
    # BUG FIX ("Salesforce FSC (Financial Services Cloud)Developer" lost
    # "Developer" entirely — twice fixed, now removed): this rule was
    # meant to catch a zero-whitespace HTML-collapse paragraph break
    # right after a closing paren (e.g. "...(Confidential)ABC Corp
    # requires..."). It can't be told apart from a recruiter's simple
    # missing-space typo INSIDE the same title (this exact real case),
    # which looks byte-for-byte identical — ")" immediately followed by a
    # capitalized word — and destroys real title content on that guess.
    # It also turned out to be entirely redundant for the one case it
    # could prove itself on: a glued NEXT-FIELD label like
    # "(Onsite)Location:" is already caught by NEXT_FIELD_PATTERN above
    # on its own, since \b matches a word boundary at ")"->"L" with no
    # whitespace required at all — confirmed directly, cut lands at
    # "Location:" either way. With its one provable case redundant and
    # its unique case actively destructive, there's no scenario left
    # where keeping this rule helps.
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
    # BUG FIX ("ON HOLD!!!****: (C-C) : (3) AI Engineer/Banking - Peoples
    # Bank..." parsed with the status banner glued onto the front of the
    # role): recruiters commonly prefix a status-update banner (ON HOLD,
    # FILLED, CLOSED, CANCELLED, REOPENED, UPDATED) ahead of the original
    # "NEW REQ:"-style subject, often wrapped in asterisks/bangs. Nothing
    # else strips this, so it survived straight into the final role.
    s = re.sub(
        r'(?i)^\s*[\*\s]*(?:on\s*hold|put\s*on\s*hold|filled|closed|'
        r'cancell?ed|reopened|re-?opened|updated?|withdrawn|expired)'
        # BUG FIX ("role: 'Please Disregard - Java Developer'" from
        # "FILLED - Please Disregard - Java Developer - Dallas TX"):
        # this only ever consumed a trailing COLON after the status
        # word, not a dash -- an extremely common separator here too.
        r'\s*[\*!]*\s*[:\-]?\s*',
        '', s
    ).strip()
    # BUG FIX (same subject): "Please Disregard" directly follows the
    # status banner in this common recruiter convention and is just as
    # much noise as the banner itself.
    s = re.sub(r'(?i)^\s*please\s+disregard\s*[:\-]?\s*', '', s).strip()
    # BUG FIX ("role: 'New'" from "New Requirement: Senior Java
    # Developer"): crop_at_next_field() (called below) treats
    # "Requirement:" as if it were a real field label -- indistinguishable
    # from "Location:"/"Client:" etc. by that shared, generic mechanism --
    # and truncates everything after it, so by the time the marketing-
    # phrase stripper further down gets a turn, the real title is already
    # gone. Strip this specific broadcast-banner phrase early instead,
    # same as the status-banner strip above.
    s = re.sub(r'(?i)^\s*(?:urgent|new|immediate)\s+requirements?\s*[:\-]?\s*', '', s).strip()
    # BUG FIX (same subject, next segment: "(C-C) : (3) AI Engineer/..."
    # left dangling as "C-C) : (3) AI Engineer..." once the status banner
    # above was stripped): subjects also commonly lead with a bracketed
    # engagement-type ("(C2C)", "(C-C)", "(W2)") and/or headcount ("(3)")
    # annotation ahead of the real title. clean_role's/this function's own
    # trailing-parenthetical stripper only ever handles the END of the
    # string, so these leading ones were never removed.
    for _ in range(3):
        stripped = re.sub(
            r'(?i)^\s*\((?:c-?c|c2c|w-?2|1099|corp\s*-?\s*to\s*-?\s*corp|\d+)\)\s*:?\s*',
            '', s
        )
        if stripped == s:
            break
        s = stripped
    # If an explicit label is present, use its value
    # BUG FIX ("Looking For_____Kafka Administrator" parsed as the role
    # verbatim): recruiter subjects commonly use "Looking for" as the
    # label, and template fill-in-the-blank subjects glue the label
    # straight onto the title with a run of underscores instead of a
    # colon/dash (e.g. a "Looking For: __________" template where the
    # title got typed directly into the blank, collapsing the space).
    # Added "looking for" to the label list and widened the separator
    # to also match one or more underscores.
    m = re.search(r'(?i)\b(?:job\s*title|job\s*role|position|role|opening|looking\s*for)\s*(?:[:\-]|_+)\s*(.+)', s)
    if m:
        s = m.group(1)
    s = crop_at_next_field(s)
    # BUG FIX (same class as crop_at_next_field's "(MFT) Engineer" bug):
    # this stripped a parenthetical ANYWHERE in the string, unlike
    # clean_role()'s trailing-only stripper (`...\)\s*$`) applied to the
    # body-extracted value. A subject line like "Managed File Transfer
    # (MFT) Engineer - Dallas TX" lost "(MFT)" here even though it's part
    # of the actual title, not a recruiter aside — the given examples
    # ("Local to VA", "USC AND H4 Only", "Onsite") are all genuinely
    # TRAILING asides in real subject lines anyway, so restricting to
    # trailing-only (looped, same pattern as clean_role) loses none of
    # the intended cases while no longer eating a mid-title acronym.
    for _ in range(3):
        stripped = re.sub(r'\s*\([^)]*\)\s*$', '', s).strip()
        if stripped == s:
            break
        s = stripped
    # Split on pipe | (single or doubled) or double-slash // bulk separators
    #
    # BUG FIX ("role: 'Only Dallas/Fort Worth, TX will be considered'"
    # from "Only Dallas/Fort Worth, TX will be considered | Senior
    # Network Engineer - Fort Worth, TX" — confirmed on a real
    # requirement row): this used to require TWO OR MORE pipe characters
    # (\|\|+) to trigger a split at all — a single "|" (just as common a
    # separator convention as "||" in real recruiter subjects) never
    # split the subject into segments in the first place, leaving the
    # whole restriction-clause-plus-title string glued together as one
    # candidate. Widened to \|+ (one or more) so a lone "|" splits too.
    #
    # BUG FIX ("role: 'Only Dallas/Fort Worth, TX will be considered'" /
    # "role: 'Hiring W2/ C2c'" — see _RESTRICTION_SUBJECT_SEGMENT_PATTERN's
    # comment above): blindly taking segment [0] assumes the subject
    # convention is always "<Role> || <Location> || <Duration>" — try
    # each segment in order and use the first one that isn't just
    # generic filler instead, falling back to segment [0] if literally
    # every segment looks generic (same as the unconditional behavior
    # before this fix).
    # Recognize common recruiter segment separators: pipes, double slashes,
    # double colons, spaced underscores, and 2+ dashes. A single colon is
    # deliberately excluded because it is normally a Label: Value separator.
    _pipe_segments = re.split(r'\s*(?:\|+|//+|::+)\s*|\s+_+\s+|-{2,}', s)
    s = _pipe_segments[0]
    # BUG FIX: the generic-filler check below only recognizes a fixed
    # list of broadcast marketing phrases -- it has no concept of a
    # segment that's just a bare work-mode word, a location qualifier
    # like "Only LOCAL to X", or a segment that's nothing but a bare
    # validated City/State with no title content at all.
    _bare_workmode_segment = re.compile(r'(?i)^(?:100%\s*)?(?:remote|hybrid|on-?site)(?:\s*only)?$')
    _location_only_segment_prefix = re.compile(r'(?i)^only\s+local(?:s)?(?:\s+to)?\b|^local(?:s)?\s+only(?:\s+to)?\b')

    def _is_bare_employment_type_segment(seg_stripped):
        # BUG FIX ("role: 'C2C'" from "C2C || Senior Azure Infrastructure
        # Automation Engineer || Newark, NJ"): a bare employment-type
        # word/phrase as its own leading segment is just as common a
        # convention as the work-mode/location-only cases already
        # handled -- reuses the same word list extract_employment_types()
        # itself is built on.
        return seg_stripped.strip().lower().rstrip('.') in _EMPLOYMENT_TYPE_BARE_WORDS

    def _is_bare_location_segment(seg_stripped):
        m = _find_city_state_match(seg_stripped)
        if not m:
            return False
        remainder = (seg_stripped[:m.start()] + seg_stripped[m.end():]).strip(' ,.-')
        return not remainder

    for _seg in _pipe_segments:
        _seg_stripped = _seg.strip()
        if (
            _seg_stripped
            and not _GENERIC_SUBJECT_ROLE_PATTERN.match(_seg_stripped)
            and not _RESTRICTION_SUBJECT_SEGMENT_PATTERN.match(_seg_stripped)
            and not _bare_workmode_segment.match(_seg_stripped)
            and not _location_only_segment_prefix.match(_seg_stripped)
            and not _is_bare_employment_type_segment(_seg_stripped)
            and not _is_bare_location_segment(_seg_stripped)
        ):
            s = _seg
            break
    # BUG FIX ("Oracle Exadata DBA, Denver, CO - 100% Day 1 Onsite (Local
    # Candidates Only)" parsed with the city/state AND work-mode still
    # attached): a location introduced by a comma ("<Title>, City, ST ...")
    # rather than a dash was never cropped at all -- only the dash-
    # introduced case below was handled. Crop at the comma immediately
    # before the first validated City/State match anywhere in the string,
    # dropping everything from there to the end (the location and
    # anything trailing it, e.g. work-mode/percentage noise).
    _loc_m = _find_city_state_match(s)
    if _loc_m:
        _prefix = s[:_loc_m.start()].rstrip()
        # BUG FIX ("role: None" from "Hiring - AWS Data Engineer, WA" --
        # the entire real title got swallowed as if it were the CITY
        # name): only apply this crop when the matched location is
        # genuinely INTRODUCED by a comma already sitting in the string
        # -- a real "<Title>, City, ST" shape has TWO commas -- never
        # when the "city" the regex found is actually just the tail end
        # of the title itself with no separating comma before it.
        if _prefix.endswith(','):
            _prefix = _prefix[:-1].rstrip()
            if _prefix:
                s = _prefix

    # Drop a trailing location/work-mode suffix after a bare dash, but cut
    # precisely AT the location trigger (a validated City/State match, or a
    # Remote/Hybrid/Onsite keyword) instead of blindly at the dash itself.
    # The old blind cut assumed anything capitalized after a dash was a
    # location, so "Senior Technical Leads - PeopleSoft, Remote" lost
    # "PeopleSoft" (real title content, not a location) along with "Remote".
    dash_m = re.search(r'\s*-\s([A-Z][a-zA-Z].*)$', s)
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

    # BUG FIX ("role: 'QA Automation with Telecom Billing Experience for
    # Alpharetta, GA (Onsite) - f...'" -- ran the full 80-char fallback
    # truncation below because nothing cropped it): recruiters commonly
    # introduce the location with a bare "for <City>" preposition instead
    # of a dash ("...Experience for Alpharetta, GA (Onsite)") -- the
    # dash-based cropping above has no dash to trigger on at all here, and
    # "for" wasn't in the location-preposition drop list below either
    # (only "in|near|@"). Same validated-trigger-only approach as the dash
    # case above: only crop if what follows "for" actually looks like a
    # real city/state or a remote/hybrid/onsite keyword, never a blind cut
    # (a real title can legitimately contain "for", e.g. "Recruiter for
    # Staffing Agency", so this must never fire on bare "for" alone).
    for_m = re.search(r'\s+for\s+([A-Z].*)$', s)
    if for_m:
        tail = for_m.group(1)
        mode_m = re.search(r'(?i)\b(remote|hybrid|onsite|on-site|on\s+location)\b', tail)
        loc_m = _find_city_state_match(tail)
        candidates = [m.start() for m in (mode_m, loc_m) if m]
        if candidates:
            trigger_pos = min(candidates)
            if trigger_pos == 0:
                s = s[:for_m.start()].strip()
            else:
                keep = tail[:trigger_pos].strip().strip(',').strip()
                s = (s[:for_m.start()].strip() + (' for ' + keep if keep else '')).strip()

    # Drop location prepositions
    s = re.split(r'(?i)\s+(?:in|near|@)\s+', s)[0]
    # Drop rate tokens
    s = re.sub(r'\$\s*\d.*$', '', s)
    # BUG FIX ("Financial/Operations Data Analyst" truncated to just
    # "Financial", "UI/UX Designer" to just "UI", "QA/Test Engineer" to
    # just "QA"): this was meant to drop a double-slash bulk separator
    # noise pattern like "//Local to X" (per this comment, and matching
    # the same "//"-based separator role_from_subject already splits on
    # earlier via "Split on pipe || or double-slash // bulk separators"),
    # but [/\\]+ matches ONE OR MORE slashes — so it fired on every
    # completely ordinary single-slash dual-specialization title too,
    # deleting everything after the FIRST slash. A single "/" inside a
    # title is extremely common (UI/UX, QA/Test, DevOps/SRE, Financial/
    # Operations) and is real title content, not noise. Requiring {2,}
    # (two or more slashes together) keeps the intended "//Local to X"
    # case working while no longer eating a single-slash title.
    s = re.sub(r'(?i)[/\\]{2,}\s*\w.*$', '', s).strip()
    # Strip leading "Requirement for / Opening for / Need for" prefix
    #
    # BUG FIX ("need for QA Lead Local to Naperville IL 45 max" and
    # "Need Java Developer who working with Capital One" both saved
    # verbatim as the role — confirmed on real requirement rows): "need"
    # was missing from both this line and _MARKETING_PHRASES below —
    # only the past-tense "needed" was covered, not the equally common
    # base form recruiters actually lead a subject/sentence with
    # ("Need Java Developer...", "need for QA Lead...").
    s = re.sub(r'(?i)^\s*(?:requirement|req|opening|posting|need)\s+for\s+', '', s).strip()
    # BUG FIX ("Trying To Reach To You- Material Planning & Logistics
    # Master Data Functional Lead- Immediate Interviews- Applynow!!!"
    # parsed as the role verbatim): broadcast recruiter subjects commonly
    # sandwich the real title between a marketing OPENER and a marketing
    # CLOSER, both dash-separated ("<opener>- <Real Title>- <closer>").
    # The prefix stripper below only ever handled the opener side, and
    # its keyword list didn't include this exact opener phrasing either
    # ("Trying To Reach To You" wasn't in it at all) — so neither end got
    # cleaned and the WHOLE noisy subject fell through as the "role".
    # Shared vocabulary so the leading and trailing stripers can't drift
    # out of sync with each other.
    _MARKETING_PHRASES = (
        r'urgent\s+requirements?|new\s+requirements?|immediate\s+requirements?|'
        r'needed|need|required|urgent(?:ly)?|immediate(?:ly)?|hiring(?:\s+now)?|hot|hire|'
        r'opportunit(?:y|ies)|apply\s*now|apply|'
        # BUG FIX ("Local Preferred: ServiceNow Developer" left "Preferred:"
        # dangling on the front of the role): "local" alone matched and
        # stripped, but "preferred" isn't a recognized marketing word on
        # its own, so this common two-word recruiter qualifier
        # ("Local Preferred" / "Locals Preferred" / "Local Only Preferred")
        # never got fully removed. Listed BEFORE the bare "local"
        # alternative so the longer, more specific phrase wins the match
        # first — regex alternation takes the first alternative that
        # matches, not the longest, so order here matters.
        r'local(?:s)?\s+(?:only\s+)?preferred|local(?:s)?\s+only|local|'
        r'trying\s+to\s+reach(?:\s+(?:out|to\s+you|you))?|reach(?:ing)?\s+out|'
        r'immediate\s+interviews?|interviews?\s+(?:today|now|asap)|asap|'
        # "Have Interview Slots" is another common urgency/marketing opener.
        r'have\s+(?:\d+\s+)?interview\s+slots?'
    )
    # Drop marketing keywords only at START — a mid-string match like
    # "Hiring!! Financial Data Analyst" would wipe the whole role with .*$
    # Looped: multi-word prefixes like "Urgent hiring for X" need more than
    # one pass -- a single pass only strips "Urgent", leaving "hiring for X"
    # behind, since each pass only consumes one keyword from the group.
    _marketing_prefix_re = re.compile(
        r'(?i)^\s*(?:' + _MARKETING_PHRASES + r')\b\s*(?:for\s+)?[\s:\-!.]*'
    )
    for _ in range(6):
        stripped = _marketing_prefix_re.sub('', s)
        if stripped == s:
            break
        s = stripped
    # Mirror of the prefix stripper, anchored at the END instead — strips
    # a dash-separated marketing CLOSER ("- Immediate Interviews",
    # "- Apply Now!!!"). Looped since these commonly chain two or more
    # in a row, same reasoning as the prefix loop above. Requires the
    # ENTIRE remaining tail after the dash to be just the marketing
    # phrase (plus trailing "!"s) — never eats a dash-separated segment
    # that has anything else in it, so a real trailing qualifier like
    # "- PeopleSoft" is untouched.
    _marketing_suffix_re = re.compile(
        r'(?i)\s*[\-\u2013]\s*(?:' + _MARKETING_PHRASES + r')\s*!*\s*$'
    )
    for _ in range(4):
        stripped = _marketing_suffix_re.sub('', s)
        if stripped == s:
            break
        s = stripped
    _marketing_suffix_nodash_re = re.compile(
        r'(?i)\s+(?:' + _MARKETING_PHRASES + r')\s*!*\s*$'
    )
    for _ in range(4):
        stripped = _marketing_suffix_nodash_re.sub('', s)
        if stripped == s:
            break
        s = stripped
    s = s.strip()
    # Drop leading punctuation left behind after stripping
    s = re.sub(r'^[\s!?.,:;\-]+', '', s)
    # BUG FIX (dangling trailing dash, e.g. "Senior Python Developer -"):
    # clean_role() (used on body-extracted values) already strips trailing
    # punctuation left behind after its own cleanup passes — this function
    # never had the equivalent, so a trailing "-"/":" left over from the
    # dash_m location-stripping block above (when the kept portion right
    # before a dropped Remote/Hybrid/location trigger itself ended in a
    # dash) survived into the final result. Same pattern as clean_role's.
    # Includes "." too (see clean_role's matching fix) for a recruiter's
    # own trailing sentence-ending period, e.g. "HELP DESK ANALYST II .".
    s = re.sub(r'[\-\u2013,:;.]+\s*$', '', s).strip()
    s = sanitize_text(s)
    if not s:
        return None
    return s if len(s) <= 80 else s[:77] + '...'


_EMAIL_ADDR_PATTERN = re.compile(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+')


def role_from_bold_lead(norm_text: str) -> Optional[str]:
    """
    Fallback for the common recruiter-template pattern where the role is
    stated plainly at the top of the body with NO label at all — just
    emphasis markup, e.g. "*QA Engineer  *" or "**QA Engineer**" as
    literally the first substantive line. None of the other fallbacks
    catch this: first_match(ROLE_PATTERNS, ...) requires an explicit
    "Job Title:"/"Role:"-style label (there isn't one here), and
    role_from_body_lead grabs everything up to the next field label —
    which, with no colon-labeled role to stop at either, swallows the
    preceding "Hi, find the below JD" preamble right along with the
    actual title, e.g. "Hi find the below JD *QA Engineer *".

    Scoped to the first 800 chars (the title is always near the top in
    this template) and deliberately conservative — same philosophy as
    every other fallback in this file: a false positive (grabbing the
    wrong bold span as the role) is worse than falling through to the
    next fallback, so this only returns something when it's confident.
    Rejects any bold span that's actually a known section-header word
    (reuses FIELD_BOUNDARIES, e.g. "*Job Overview*", "*Key
    Responsibilities*" — real section headers in this exact template,
    not roles) or contains a field-label colon (e.g. a bold-wrapped
    "*Location: San Jose*").
    """
    if not norm_text:
        return None
    header_slice = norm_text[:800]
    boundary_words = {b.lower() for b in FIELD_BOUNDARIES}
    # Reject a bold span that's just recruiter marketing noise ("*URGENT*",
    # "*Hiring Now*", "*Hot Requirement*") rather than the actual title —
    # these commonly appear as the FIRST bold span, ahead of the real
    # title, in exactly this kind of unlabeled template. Same noise-word
    # spirit as role_from_subject's own marketing-prefix stripper, just
    # applied to the whole candidate here rather than a leading prefix.
    _noise_only_re = re.compile(
        r'(?i)^(?:urgent|immediate|hot|new|hiring(?:\s+now)?|apply(?:\s+now)?|'
        r'needed|required|please\s+respond|respond\s+asap|asap)\s*!*$'
    )
    # BUG FIX (a bolded PHRASE INSIDE A BULLET POINT further down the body
    # — e.g. "*7+ years*" in a "Required Qualifications" list — mistaken
    # for the role, while a real PLAIN-TEXT title sitting unlabeled at the
    # very top of the same email, with no emphasis markup at all, was
    # never even looked at): this used to loop with re.finditer and, once
    # the first candidate was rejected (e.g. "*Location:*", correctly
    # excluded for containing a colon), kept scanning forward through the
    # ENTIRE 800-char window for ANY other acceptable-looking emphasis
    # span — however far into the document, including deep inside
    # unrelated bullet content. A genuine "bold lead" title is by
    # definition the very first substantive thing in the body (see this
    # function's own docstring); only ever consider that FIRST emphasis
    # span, and only when nothing but blank lines/whitespace precedes it.
    # If that first candidate doesn't pass the exclusion checks below,
    # there's no bold-lead pattern in this email at all — falling through
    # to role_from_body_lead (which already handles an unlabeled PLAIN
    # first-line title correctly) is the right outcome, not searching
    # deeper for a different bold phrase to misattribute as the role.
    m = re.search(r'\*{1,2}([^*\n]{2,60}?)\*{1,2}', header_slice)
    if not m or header_slice[:m.start()].strip():
        return None
    candidate = sanitize_text(m.group(1))
    if not candidate:
        return None
    if ':' in candidate or '-' in candidate:
        return None
    if candidate.lower().strip() in boundary_words:
        return None
    if _noise_only_re.match(candidate.strip()):
        return None
    if is_email_body(candidate) or len(candidate.split()) > 8:
        return None
    return candidate


def role_from_numbered_label(text: str) -> Optional[str]:
    """
    Fallback for a common multi-posting broadcast pattern: "Role: 1" /
    "Position: 2" used as a bare SEQUENCE NUMBER header for that posting
    within the email, with the actual title sitting unlabeled on the very
    next line — e.g.:
        Role: 1
        Senior AWS Application Architect
        Location: New York, NY ...

    first_match()'s bare-numeric-value guard correctly refuses to treat
    "1"/"2" itself as a role (see that guard's own comment), but on its
    own that just means NOTHING gets extracted from the body for this
    segment — falling all the way through to role_from_subject(), which
    returns the SAME shared, generic subject text (e.g. "Multiple
    Requirements") for every numbered posting in the email instead of
    each one's own specific, correct title. Scoped to the first 500
    chars — this pattern always appears right at the top of a segment,
    right after split_into_requirement_segments() has already split on
    these exact "Role: N" anchors.
    """
    if not text:
        return None
    m = re.search(
        r'(?im)^[ \t]*(?:role|position)\s*[:\-]?\s*\d+\s*[.):]?[ \t]*\n[ \t]*(.+)',
        text[:500],
    )
    if not m:
        return None
    candidate = sanitize_text(m.group(1).split('\n', 1)[0])
    if not candidate:
        return None
    candidate = crop_at_next_field(candidate)
    if not candidate or is_email_body(candidate) or len(candidate) > 100:
        return None
    return candidate


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
    # BUG FIX (ported from rap_python_cron's identical fix — role
    # captured as "Hi,"/"Hello,"/a bare greeting even when the real role
    # is stated cleanly further down): the blank-line cut a few lines
    # below (itself being ported in alongside this fix) exists to handle
    # a genuine bare-title-then-prose opener ("Java Full Stack Developer
    # \n\nWe are looking for..."), where "the line before the first
    # blank line" really is the title. A greeting paragraph has the
    # exact same shape (one short line, then a blank line) but obviously
    # isn't a title. Skip past a leading greeting paragraph first so the
    # blank-line heuristic correctly applies to whatever comes after it
    # instead of the greeting itself.
    greet_m = re.match(
        r'(?i)^\s*(?:hi|hello|hey|greetings|dear|good\s*(?:morning|afternoon|evening))'
        r'\b[^\n]{0,40}\n\s*\n',
        text
    )
    if greet_m:
        text = text[greet_m.end():].lstrip()
    # BUG FIX ("role: 'Hope you are doing great!!!'" -- confirmed real
    # case): the greeting strip above only ever removes ONE leading
    # paragraph, matched against a fixed set of greeting-word triggers
    # ("Hi,", "Hello,", ...). Recruiter templates commonly stack a second
    # pleasantry line ("Hope you are doing great!!!") and/or a generic
    # request line ("Please share suitable profile...") right after it,
    # before the actual role content starts -- neither starts with any of
    # those trigger words, so neither was recognized as boilerplate; the
    # first one fell straight through to become "the role" via the
    # blank-line heuristic below. Loops up to twice more against whatever
    # is left after the first strip.
    for _ in range(2):
        pleasantry_m = re.match(
            r'(?i)^\s*(?:hope|trust|wish(?:ing)?)\s+(?:you|this|everything)'
            r'\b[^\n]{0,60}\n\s*\n'
            r'|^\s*please\s+(?:share|review|find|see|go\s+through|check)'
            r'\b[^\n]{0,80}\n\s*\n',
            text
        )
        if not pleasantry_m:
            break
        text = text[pleasantry_m.end():].lstrip()
    cut = len(text)
    m = NEXT_FIELD_PATTERN.search(text)
    if m:
        cut = min(cut, m.start())
    # BUG FIX (ported from rap_python_cron's identical fix): a bare
    # unlabeled title line followed by a blank line then ordinary prose
    # ("Java Full Stack Developer\n\nWe are looking for a skilled
    # developer...") had no cut point at all before this — NEXT_FIELD_
    # PATTERN only fires on an actual labeled field further down (if one
    # exists at all), so the whole candidate included that prose
    # sentence too, which then failed the is_email_body() check below
    # and returned None entirely — even though the very first line alone
    # was a perfectly good, obvious title. A blank line very reliably
    # marks "the title is just what came before this" in this specific
    # bare-lead-line context (now that a leading greeting is skipped
    # past first, per the fix above).
    blank_m = re.search(r'\n\s*\n', text)
    if blank_m:
        cut = min(cut, blank_m.start())
    loc_m = _find_city_state_match(text)
    if loc_m:
        cut = min(cut, loc_m.start())
    candidate_raw = text[:cut]
    # BUG FIX (ported from rap_python_cron's identical fix): when the
    # candidate's own text started with a bare label word ALONE ON ITS
    # OWN LINE (the "Role" in "Role\nJava Developer" from a stacked
    # table row), sanitize_text() below collapses that newline to a
    # space, leaving the label word glued onto the front of the actual
    # value — "Role Java Developer" instead of "Java Developer". Checked
    # against the ORIGINAL text (own-line, real newline required) rather
    # than a generic "starts with this word" strip on the already-
    # flattened candidate — a real title that happens to start with one
    # of these words as actual title content ("Title Insurance
    # Analyst") has no newline right after it, so it's correctly left
    # untouched.
    lead_m = re.match(
        r'(?i)^(?:' + '|'.join(re.escape(w) for w in NEXT_FIELD_LABELS) + r')\s*\n',
        candidate_raw
    )
    if lead_m:
        candidate_raw = candidate_raw[lead_m.end():]
    candidate = sanitize_text(candidate_raw)
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
    text = text.strip()
    # BUG FIX: plain-text emails using "*bold*"-style markdown emphasis
    # around field labels/values (e.g. "*Location: San Jose*") leave a
    # stray leading/trailing "*" stuck to the captured value once the
    # label itself is stripped off. Strip it the same way pipes are
    # already stripped above.
    text = re.sub(r'^\*+\s*|\s*\*+$', '', text)
    return text.strip()


def clean_whitespace(text: Optional[str]) -> Optional[str]:
    """Clean whitespace from text."""
    if not text:
        return None
    return ' '.join(text.split())

# BUG FIX ("did a real posting come in as a reply/forward and get
# dropped?"): the previous version treated Re:/Fwd:/Fw: identically --
# both hard-blocked from ever being a fresh requirement. That's correct
# for a REPLY (thread back-and-forth -- "thanks", "sending resume",
# quoting the same JD everyone already has), but wrong for a FORWARD:
# in this domain, forwarding a JD to another recruiter/contact is a
# normal, primary way a genuine posting circulates, not a sign it's
# stale. Blanket-skipping every Fwd: silently dropped real requirements.
# Split the two apart: is_reply_email() now only covers pure reply
# shapes (Re: prefix, "On ... wrote:" quote intro, heavy '>' quoting) --
# genuinely forward-shaped mail (Fwd:/Fw: prefix, "---Forwarded
# message---", "Begin forwarded message:", or an Outlook forward header
# block) is NOT auto-skipped here; it's left for is_job_requirement_email()
# to decide based on content, same as any other email. Worst case a
# re-forwarded duplicate posting reaches dedup.py and gets caught there --
# a much safer failure mode than silently losing a real one. A subject
# with BOTH markers (e.g. "Re: Fwd: Java Developer") is treated as a
# forward: the forward marker wins, since the alternative (treating it
# as a reply and skipping) risks losing content someone deliberately
# forwarded onward.
_REPLY_ONLY_BODY_MARKERS = [
    r'-{2,}\s*original message\s*-{2,}',
    r'\bon\s+.{5,80}?\s+wrote:',
]

_FORWARD_BODY_MARKERS = [
    r'-{2,}\s*forwarded message\s*-{2,}',
    r'^begin forwarded message:',
    # BUG FIX ("Re: Python AI Engineer..." -- a full, genuine JD quoted
    # under a bare "Re:" reply -- confirmed real case, silently dropped
    # as "Reply or Forward email"): this 4-line block required an exact
    # From:/Sent:/To:/Subject: sequence, but Outlook doesn't always
    # include a "To:" line in its quoted-header block (e.g. when the
    # original went to a distribution address rather than a named
    # recipient) -- this real email's quoted header was only From:/
    # Sent:/Subject:, three lines, and the required fourth line made the
    # whole pattern silently fail to match, so is_forward_email()
    # returned False and the email fell through to is_reply_email(),
    # which saw the bare "Re:" subject and auto-skipped the entire
    # quoted JD. The "To:" line is now optional. This doesn't loosen
    # anything else -- the From:/Sent:/Subject: sequence is exactly as
    # strict as before, so unrelated three-line text (e.g. a paragraph
    # that separately happens to start lines with "From my perspective",
    # "Sent the update", "Subject to review") still won't match unless it
    # actually forms that literal consecutive header block.
    r'^from:\s*.+\n^sent:\s*.+\n(?:^to:\s*.+\n)?^subject:\s*.+',
]

def is_forward_email(subject: str, body_text: str = "") -> bool:
    """True for a forwarded email (Fwd:/Fw: or a forward-shaped body) --
    NOT auto-excluded from being a fresh requirement; see the block
    comment above."""
    subject = subject or ""
    # Match the whole leading chain of Re:/Fwd:/Fw: prefixes (not just the
    # very first one), so "Re: Fwd: Java Developer" -- a reply sent within
    # an already-forwarded thread -- is still recognized as a forward
    # rather than only checking the first token ("Re:").
    prefix_chain = re.match(r'^\s*(?:(?:re|fw|fwd)\s*:\s*)+', subject, re.IGNORECASE)
    if prefix_chain and re.search(r'\bfwd?\s*:', prefix_chain.group(0), re.IGNORECASE):
        return True
    if not body_text:
        return False
    for pattern in _FORWARD_BODY_MARKERS:
        if re.search(pattern, body_text, re.IGNORECASE | re.MULTILINE):
            return True
    return False

def is_reply_email(subject: str, body_text: str = "") -> bool:
    """Detect if an email is a pure reply (not a forward -- see
    is_forward_email()), which should never be treated as a fresh
    requirement."""
    subject = subject or ""
    if is_forward_email(subject, body_text):
        return False
    if re.match(r'^\s*re\s*:', subject, re.IGNORECASE):
        return True
    if not body_text:
        return False
    for pattern in _REPLY_ONLY_BODY_MARKERS:
        if re.search(pattern, body_text, re.IGNORECASE | re.MULTILINE):
            return True
    if len(re.findall(r'^\s*>', body_text, re.MULTILINE)) >= 3:
        return True
    return False

def is_job_requirement_email(text: str) -> bool:
    """Return True when enough job-requirement indicators are found."""
    if not text:
        return False
    indicators = [
        r'\bjob\s+title\b', r'\bposition\b', r'\bopening\b', r'\brequirements?\b',
        r'\bclient\b', r'\blocation\b', r'\brate\b', r'\bduration\b',
        r'\bcontract\b', r'\bskills\b', r'\bexperience\b',
        r'\$\d+', r'\bC2C\b', r'\bW2\b', r'\b1099\b',
        r'\bremote\b', r'\bonsite\b', r'\bon-site\b', r'\bhybrid\b', r'\byears?\b',
        # BUG FIX (ported from rap_python_cron's already-fixed copy of this
        # same function -- the two parser.py files had diverged, and this
        # file never received either fix): "role"/"title" -- arguably the
        # single most fundamental job-posting signal there is -- were
        # missing from this list entirely. This mattered most for a
        # per-segment check on an individual posting split out of a
        # multi-posting email: those segments are naturally short (just a
        # title + location + duration, say), so they'd often clear this
        # >=2 threshold on ONE indicator at most under the old list and
        # get rejected outright. Also fixed the plural "requirements?"
        # above (was singular-only "requirement\b") for the same reason
        # -- "Requirements:" is by far the more common JD section heading.
        # BUG FIX: bare "title" and "job title" above can match the SAME
        # phrase ("job title" contains "title"), double-counting one
        # piece of evidence as two indicators. Negative lookbehind stops
        # bare "title" from re-matching the "job title" phrase already
        # counted above.
        r'\brole\b', r'(?<!job\s)\btitle\b',
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
    # BUG FIX: this required the literal unhyphenated word "hotlist" --
    # a real broadcast saying "Updated Hot-List of <company> consultants"
    # (hyphenated, extremely common phrasing) never matched at all, since
    # \b requires a word/non-word transition and the hyphen breaks the
    # match. \s? / \-? between "hot" and "list" catches both spellings.
    # BUG FIX ("Sr. React Native Developer" JD misclassified as a
    # hotlist broadcast, role came out UNKNOWN): a genuine single job
    # posting's own boilerplate footer often reads "Please add
    # hotlist@ampstek.com in your distribution list" -- the sender's own
    # mailing-list mailbox happens to be NAMED "hotlist@...", which is a
    # completely different thing from the email BEING a hotlist
    # broadcast. Excludes the match when immediately followed by "@"
    # (i.e. it's the local part of an email address, not the phrase).
    r'(?i)\bhot[\s\-]?list\b(?!@)|'
    r'\bour\s+(?:consultants?|resources?|candidates?)\s+(?:are|is)\b|'
    r'\bconsultants?\s+(?:are\s+)?ready\s+to\s+join\b|'
    # BUG FIX: only matched "bench consultants"/"bench resources" --
    # "bench Candidates" (confirmed real case, Mounica/Consigatech
    # broadcast) is just as common a variant and wasn't covered.
    r'\bbench\s+(?:consultants?|resources?|candidates?)\b|'
    r'\bbench\s*list\b|'
    r'\bconsultants?\s+coming\s+out\s+of\s+(?:the\s+)?projects?\b|'
    r'\badd\s+[\w.+\-]+@[\w.\-]+\s+to\s+(?:your\s+)?requirements?\b|'
    r'\bsend\s+(?:me\s+)?(?:the\s+)?requirements?\s+(?:to\s+my\s+email|on\s+(?:a\s+)?daily\s+basis)\b|'
    # BUG FIX: a distinct, very common recruiter-to-recruiter broadcast
    # shape -- "we are looking C2C role for below candidates, if you
    # have any [suitable/related] requirements please let me know, please
    # share the Jd to..." -- named none of the phrases above and had no
    # literal "hotlist"/"bench" word anywhere in the body at all, so it
    # slipped straight through as if it were a real job posting, with the
    # recruiter's own broadcast subject line then mistaken for the role.
    # None of these phrases occur in a genuine job requirement -- a real
    # JD IS the requirement, it never asks the reader to "share the JD"
    # or "send your requirements" in return.
    r'\bplease\s+share\s+(?:the\s+)?(?:jd|job\s+description)s?\b|'
    # BUG FIX ("...check if you have any C2C requirements..." -- confirmed
    # real case, Mounica/Consigatech): the filler-word class between "any"
    # and "requirements" was [a-z]+, which only matches letters -- it
    # silently rejected any filler token containing a digit, like "C2C" or
    # "H1B". [a-z0-9]+ keeps the same 0-2-word cap but no longer breaks on
    # the single most common staffing-domain filler word there is.
    # BUG FIX ROUND 2 ("...if you have any openings matching this skill
    # set..." -- untested candidate phrasing, added proactively): only
    # "requirements" was accepted as the object noun; "openings" is just
    # as common and carries the same direction-safe guarantee -- a real
    # JD already IS the opening, it never asks the reader whether THEY
    # have openings. Tested against a genuine multi-opening JD digest
    # ("We have multiple openings for Senior Java Developers...") to
    # confirm the different sentence shape there doesn't trip this.
    r'\bif\s+you\s+have\s+(?:any\s+)?(?:[a-z0-9]+\s+){0,2}(?:requirements?|openings?)\b|'
    r'\bsend\s+(?:me\s+)?your\s+(?:job\s+)?requirements?\b|'
    r'\bcandidates?\s+available\b|'
    # BUG FIX ("Available Consultants" hotlist table treated as a real
    # requirement, role ending up as just the raw subject line): "Please
    # add my email ID to your distributing list and send daily
    # requirements" — a real, common recruiter hotlist-signup ask —
    # matched neither existing pattern above: the "add ... to" one
    # requires a literal email address right after "add" (this says "my
    # email ID" instead), and the "send requirements" one requires
    # "requirements" before "daily basis" (this says "daily
    # requirements", the reverse order). Both are just as common as the
    # phrasing already covered.
    r'\badd\s+(?:my\s+)?(?:email(?:\s*id)?|address)\s+to\s+(?:your\s+)?'
    r'(?:distribut(?:ion|ing)|mailing)\s+list\b|'
    # BUG FIX ("HOTLIST OF Sr. Data Engineer candidates..." body-only
    # detection missing it -- confirmed real case, is_hotlist_email() is
    # deliberately called on the BODY ONLY in production, see
    # requirements_sync.py): "We have highly skilled consultants
    # available on the bench" and "Please find our available consultants
    # below" are both extremely common bench-broadcast phrasings, but in
    # the REVERSED word order from what was already covered --
    # "available" before "consultants"/"bench", not after -- so neither
    # `bench\s+consultants` nor `candidates?\s+available` (existing
    # patterns above) matched. Bare "available consultants" (either
    # order relative to "bench") is essentially never used to describe a
    # single role being filled -- a real JD doesn't have "available
    # consultants" of its own to offer.
    r'\b(?:consultants?|resources?)\s+available\s+on\s+(?:the\s+)?bench\b|'
    # BUG FIX ("...our available genuine Candidates..." -- confirmed real
    # case, IT Career Inc): the bare "available consultants?" pattern
    # required the noun immediately after "available" with nothing in
    # between -- an inserted adjective like "genuine" (or "certified",
    # "qualified", etc.) broke the match entirely. Allows up to 2 filler
    # words between "available" and the noun, same cap already used
    # elsewhere in this pattern set for the same reason.
    r'\bavailable\s+(?:[a-z]+\s+){0,2}(?:consultants?|candidates?|resources?)\b|'
    r'\bplease\s+find\s+(?:our\s+)?available\s+(?:consultants?|candidates?|resources?)\b|'
    r'\bsend\s+(?:me\s+)?daily\s+requirements?\b|'
    # BUG FIX (real-world recruiter phrasing not yet covered): "I have a
    # candidate/consultant available" and "My candidate/consultant is
    # available" are common first-person variants of the exact same
    # bench-broadcast pitch the "our consultants are available" pattern
    # above already covers -- just with "I"/"my" instead of "our".
    # Requiring "available" right after (rather than a bare "I have a
    # candidate") is deliberate: "I have a candidate for this role, here
    # is his resume" (a recruiter submitting ONE candidate against an
    # existing posting, not broadcasting their bench) reads very
    # differently and must NOT trip this -- tested against that exact
    # phrasing to confirm it doesn't.
    r'\bi\s+have\s+(?:a\s+)?(?:candidates?|consultants?)\s+available\b|'
    r'\bmy\s+(?:candidate|consultant)\s+is\s+available\b|'
    # BUG FIX ("...our W2 Candidates, who are available immediately..."
    # confirmed real case, Sravani/Techwizens; "...consultants who are
    # readily available..." confirmed real case, CSCS): every existing
    # "available" pattern above requires the noun and "available" to sit
    # right next to each other. Real bench pitches very commonly insert a
    # relative clause -- "candidates, who are available", "consultants who
    # are readily available" -- which none of them catch. This is checked
    # as its own pattern rather than widened filler on the existing ones
    # because "who is/are available" is an unambiguous bench-broadcast
    # construction on its own -- a real JD describes ONE role, it doesn't
    # refer to plural "candidates/consultants who are available".
    r'\b(?:consultants?|candidates?|resources?)\s*,?\s+who\s+(?:are|is)\s+'
    r'(?:readily\s+|currently\s+)?available\b|'
    # BUG FIX ("Please share your C2C roles..." confirmed real case, Blue
    # Space Technologies, appears across multiple broadcasts; "...share
    # your daily C2C/C2H positions with us" confirmed real case, IT
    # Career Inc): a recruiter asking the READER to share ROLES/positions
    # is the opposite direction of a real job requirement -- a genuine JD
    # already IS the role being shared, it never asks the reader to send
    # roles back. Distinct from the existing "please share the JD" and
    # "send your requirements" patterns above, which don't cover "share
    # your roles/positions" phrasing.
    r'\bshare\s+your\s+(?:daily\s+)?(?:c2c\s*/?\s*c2h|c2c|c2h)?\s*'
    r'(?:roles?|requirements?|positions?)\b|'
    # BUG FIX ROUND 2 ("Kindly share your open requirements..." --
    # untested candidate phrasing, added proactively): the pattern above
    # only allowed "daily" or a c2c/c2h token between "your" and the
    # object noun. Widened to a generic 0-2-word filler cap (matching the
    # convention used elsewhere in this pattern set) and added
    # "openings" as a recognized object -- both direction-safe for the
    # same reason as the "if you have any requirements/openings" fix
    # above.
    r'\bshare\s+your\s+(?:[a-z]+\s+){0,2}(?:roles?|requirements?|positions?|openings?)\b|'
    # BUG FIX ROUND 2 (bare "please share requirements", no "your" --
    # untested candidate phrasing, e.g. "...please share requirements"):
    # every "share...requirements" pattern above requires "your"
    # explicitly. A real JD never asks the reader to "share the
    # requirements" in any form, since the JD already contains them --
    # direction-safe on its own without needing "your".
    r'\bplease\s+share\s+(?:the\s+)?requirements?\b|'
    # BUG FIX ROUND 2 ("We would love to submit our consultants to your
    # open positions" -- untested candidate phrasing, added proactively):
    # distinct from "our consultants are/is available" above -- "submit"
    # is the verb here, not "are/is". Unambiguously recruiter-source
    # pitch language; a real JD is never the one doing the submitting.
    r'\bsubmit\s+our\s+(?:consultants?|candidates?|resources?)\b|'
    # BUG FIX ("...include my email siva@careits.com in your daily
    # requirements distribution" confirmed real case, Care IT Services;
    # "Please add me to your mailing list" same email): "requirements
    # distribution" and "add me to your mailing list" are both
    # recruiter-signup phrasings distinct from the existing "add
    # <email> to requirements" pattern above (that one requires a literal
    # email address glued right after "add" -- this phrasing reverses the
    # structure entirely, asking to be added generically first).
    r'\brequirements?\s+distribution\b|'
    r'\badd\s+me\s+to\s+your\s+mailing\s+list\b|'
    # BUG FIX ("...if you'd like to receive his resume, please reply to
    # this email with job details..." confirmed real case, Thoughtwave
    # Software): a single-candidate bench pitch offering to SEND a
    # resume in exchange for the reader's job details -- the reverse
    # direction of a real JD, which never offers up a candidate's resume
    # or asks the reader to reply with details of their own opening.
    r'\bif\s+you\W?d\s+like\s+to\s+receive\s+(?:his|her|their)\s+resume\b|'
    r'\bplease\s+reply\s+(?:to\s+this\s+email\s+)?with\s+(?:the\s+)?'
    r'(?:job\s+)?details\b|'
    # BUG FIX ("Consultant Name / Technology / Visa" table with no
    # qualifying sentence at all -- confirmed real case, Techrakers
    # broadcast): a plain bench-consultant listing table can carry NONE
    # of the phrase-level signals above, just column headers. "Consultant
    # Name" as an exact two-word phrase is deliberately required (rather
    # than bare "consultant") -- a real JD commonly says "the consultant
    # must have..." but essentially never uses "Consultant Name" as its
    # own two-word phrase, since a JD describes one role, not a roster of
    # named consultants. Requires "Consultant Name" to be followed,
    # within a bounded span, by both a "Technology"/"Skill Set" column
    # and a "Visa" column -- tested against realistic JD prose that
    # merely mentions "consultant", "technology" and "visa" scattered
    # separately (no false match, since it lacks the "Consultant Name"
    # anchor) to confirm this doesn't fire on genuine postings.
    r'\bconsultant\s*name\b[\s\S]{0,150}?\b(?:technology|skill\s*sets?)\b'
    r'[\s\S]{0,300}?\bvisa\b|'
    # BUG FIX ROUND 2 (sender signature carries a "Bench Sales" job title
    # -- untested candidate phrasing, added proactively): the single
    # clearest signal available is often the SENDER's own stated role,
    # not body phrasing at all -- "Bench Sales Recruiter", "US IT Bench
    # Sales", "Sr. Bench Sales Manager" etc. This job title is
    # essentially unique to people whose job is selling bench
    # consultants; no genuine JD sender (account manager, technical
    # recruiter, hiring manager) signs off this way. Tested against
    # realistic non-bench-sales signatures ("Technical Recruiter",
    # "Senior Talent Acquisition Specialist") to confirm those don't trip
    # this.
    r'\bbench\s+sales\b|'
    # BUG FIX ROUND 2 ("Please go through the profile and let us know
    # your thoughts" -- untested candidate phrasing, added proactively):
    # a recruiter-pitch review ask distinct from anything above.
    r'\bgo\s+through\s+the\s+profiles?\b|'
    # BUG FIX ROUND 2 (regression found via real-corpus batch testing:
    # "Ideal Candidate Profile" / "Desired Candidate Profile" is a
    # completely standard JD section heading listing the soft-skills/
    # traits an employer wants -- e.g. "...12+ years required...Ideal
    # Candidate Profile: Highly organized and execution-focused..." --
    # confirmed false positives on two real, fully-detailed JDs, "Opening
    # for Scrum Master - NYC" and "Gen AI/Agentic AI Lead / AI Architect".
    # The original bare "consultant/candidate profile" noun-phrase match
    # couldn't tell that heading apart from a genuine bench pitch offering
    # up a specific candidate's profile ("I'm sharing a strong SAP PP/QM
    # Consultant profile for your review"). Rather than blacklist
    # "ideal"/"desired" (which would miss other heading variants), this
    # now requires an actual OFFERING verb within a few words before the
    # phrase -- sharing/attached/find/see/review/below/following -- which
    # is what genuinely distinguishes "here is a candidate's profile for
    # you" from a JD's own descriptive heading. Tested against both real
    # false-positive cases (neither has an offering verb nearby -- "Ideal"
    # alone precedes it) and against the original confirmed true positives
    # (all still match) to confirm this doesn't reintroduce the leak it
    # was fixing.
    r'\b(?:sharing|share|attached|find|see|review|below|following)\b'
    r'(?:\s+\S+){0,4}\s+(?:consultant|candidate)\s+profiles?\b|'
    # BUG FIX ROUND 2 ("Kindly utilize this resource for any matching
    # requirements" -- untested candidate phrasing, added proactively):
    # tested against "this role will utilize resources across multiple
    # teams" (plural "resources", a plausible genuine-JD sentence) to
    # confirm the singular-only match here doesn't trip on it.
    r'\butilize\s+(?:this\s+)?(?:resource|consultant|candidate)\b|'
    # BUG FIX ROUND 2 ("Please find attached resume of our Java
    # consultant..." / "Attached is the resume of our Senior DevOps
    # consultant..." -- untested candidate phrasing, added proactively):
    # offering up a THIRD PARTY's resume as an attachment -- the reverse
    # direction of a real JD, which never attaches or references
    # "the resume of" someone else. Distinct from the existing "if
    # you'd like to receive his resume" pattern above (that one is
    # conditional/offered; this one states the resume is already
    # attached).
    r'\bplease\s+find\s+attached\s+(?:the\s+)?resume\b|'
    r'\battached\s+is\s+(?:the\s+)?resume\s+of\b|'
    # BUG FIX ROUND 2 ("We are pleased to share the below profile for
    # your review" -- untested candidate phrasing, added proactively):
    # distinctive bench-broadcast framing not covered by any pattern
    # above.
    r'\bpleased\s+to\s+share\s+(?:the\s+)?(?:below|following)\s+profiles?\b'
)

# BUG FIX ("HOTLIST(AI ENGINEER LOOKING PROJECT ALL OVER USA...)" parsed as
# a real job requirement, role coming out as the raw subject line): the
# word "hotlist" was RIGHT THERE in the subject, but is_hotlist_email() is
# deliberately scoped to the body only (see the class-level comment above
# _HOTLIST_INDICATORS and parse_requirement()'s own bug-fix note) because
# a genuine job posting's SUBJECT sometimes uses "Hotlist" as a generic
# batch label while its BODY is still a real JD -- so trusting the
# subject word alone would wrongly reject real postings. This particular
# email's body, though, isn't a JD at all -- it's literally the
# candidate's own resume (a bench-consultant profile broadcast for
# someone else to place), and resumes have a structural signature no
# real job requirement shares: named sections like "PROFESSIONAL
# SUMMARY", "WORK EXPERIENCE", "EDUCATION", "SKILLS & CERTIFICATIONS"
# appearing together, usually HTML-glued with no space before whatever
# follows (e.g. "EDUCATIONPace University"), so no TRAILING word
# boundary is required -- only a LEADING one, to avoid a mid-word false
# hit like "framework experience" being mistaken for the "WORK
# EXPERIENCE" section header. Requires 2+ distinct headers to fire,
# since any single one of these words alone occasionally shows up in a
# real JD's filler text.
#
# BUG FIX ("Data Scientist" JD misclassified as a hotlist/resume, role
# came out UNKNOWN): the plain phrase-anywhere search below matched
# "...of impactful work experience in AI, ML, or NLP" -- completely
# ordinary JD prose, not a section header at all -- alongside a
# legitimate "Education:" label, hitting the 2+ threshold on a real
# single-role posting. The class comment above already describes the
# actual structural signal a resume header has (start of its own line,
# or "HTML-glued" straight onto a colon/following word with no space)
# -- the old regex just never enforced it. Now requires the phrase to
# sit at the start of a line, or be immediately followed by a colon;
# "work experience" buried mid-sentence satisfies neither. No \b after
# the start-of-line alternative -- two adjacent letters (as in the
# glued "PROFESSIONAL SUMMARYExperienced...") never form a word
# boundary, so requiring one there would have defeated the whole point
# of catching the glued case.
_RESUME_SECTION_HEADERS = re.compile(
    r'(?im)^[ \t]*(?:professional\s+summary|work\s+experience|'
    r'skills\s*(?:&|and)\s*certifications|education)|'
    r'(?<!\w)(?:professional\s+summary|work\s+experience|'
    r'skills\s*(?:&|and)\s*certifications|education)\s*:'
)

# BUG FIX ("HOTLIST(DevOps, SRE Engineer looking Project all over USA,
# H1B)" -- confirmed real case, RWaltz/Paul(SUDIEEP)): this was a
# candidate's full resume pasted as the email body (thousands of words of
# work history), but it uses "Professional Experience" / "Technical
# Skills" as its own section labels rather than the four literal phrases
# _RESUME_SECTION_HEADERS recognizes, so the 2-header threshold never
# fired and the whole resume ran through full JD extraction. Adding
# "professional experience"/"technical skills" as recognized headers was
# considered and rejected -- both are common, legitimate labels in a
# real JD too ("Professional Experience Required: 5+ years", "Technical
# Skills: Java, AWS"), so it would trade this false negative for a new
# false positive on genuine postings.
#
# Instead this targets what's actually unique to a resume: 3+ distinct
# "MM/YYYY - MM/YYYY" (or "- Till Date" / "- Present") employment date
# ranges, i.e. an actual work-history timeline. A real single-role JD may
# mention one contract duration ("06/2026 - 12/2026") or occasionally two
# project-phase dates, but a candidate's resume listing successive past
# jobs is the only shape that produces three or more of these -- tested
# against realistic JD duration/timeline phrasing (including a
# deliberately adversarial two-phase project timeline) to confirm neither
# trips this at the 3+ threshold.
# BUG FIX ROUND 2 (RWaltz resume date ranges still not detected in the
# real email -- confirmed real case): the leading \b in
# _EMPLOYMENT_DATE_RANGE required a word/non-word transition right before
# the digit, but this resume's HTML-flattened text glues the date
# directly onto the preceding word with no space at all ("Platform
# Engineer04/2025- Till date", "Operations03/2023 -03/2025") -- letters
# and digits are both \w characters, so no boundary exists between them,
# and the leading \b silently rejected every glued date. Dropped the
# leading \b (the trailing \b is kept and still works, since a date range
# is always followed by a space or punctuation, never another digit
# glued on). Tested against digit strings that happen to contain a
# similar shape (a reference number, a phone extension) to confirm this
# doesn't produce spurious partial matches.
_EMPLOYMENT_DATE_RANGE = re.compile(
    r'(?i)\d{1,2}/\d{4}\s*[-\u2013]\s*(?:\d{1,2}/\d{4}|till\s*date|present|current)\b'
)


def _looks_like_resume_body(text: str) -> bool:
    """True when `text` has 2+ distinct resume-style section headers, or
    3+ distinct past-employment date ranges (see _RESUME_SECTION_HEADERS
    and _EMPLOYMENT_DATE_RANGE docstrings above) -- i.e. this is a
    candidate's resume/profile, not a job requirement JD."""
    if not text:
        return False
    hits = {m.group(0).lower() for m in _RESUME_SECTION_HEADERS.finditer(text)}
    if len(hits) >= 2:
        return True
    date_ranges = {m.group(0) for m in _EMPLOYMENT_DATE_RANGE.finditer(text)}
    return len(date_ranges) >= 3


def is_hotlist_email(text: str) -> bool:
    """True for recruiter 'available consultants' broadcasts -- the
    opposite of a job requirement email. See _HOTLIST_INDICATORS above."""
    if not text:
        return False
    return bool(_HOTLIST_INDICATORS.search(text)) or _looks_like_resume_body(text)


# BUG FIX ("role: 'to Work - Senior US IT Recruiter'" from a candidate's
# own "Open to Work" self-promotion email, confirmed real case): neither
# is_job_requirement_email() nor is_hotlist_email() covers this shape --
# it's not "supplying candidates for someone else to place" (hotlist),
# it's a single individual describing THEIR OWN availability and asking
# to be considered, in first person. It still uses ordinary staffing
# vocabulary (experience, remote, visa types) so is_job_requirement_email
# 's keyword-count gate can't tell it apart either -- it silently ran
# full role/client/location extraction and produced garbage. Distinctive
# first-person "I am looking for a role" framing, never used in a real
# JD (a real JD describes a role for someone ELSE to fill, never the
# sender's own job search) is checked for; deliberately excludes bare
# "Work Preference:" alone -- a real JD can legitimately use that exact
# label for its own on-site/remote requirement (confirmed false-positive
# risk, tested and excluded) -- "Preferred Roles:" (plural, a candidate
# listing several roles THEY would accept) doesn't have that ambiguity.
_CANDIDATE_SELF_PROMO_INDICATORS = re.compile(
    r'(?i)\bopen\s+to\s+work\b|'
    r'\bi\s+am\s+(?:currently\s+)?(?:looking|seeking)\s+for\b[^\n.]{0,60}'
    r'(?:opportunit|role|position)|'
    r'\b(?:share|forward)\s+(?:any\s+)?(?:relevant|suitable)\s+openings?\s+'
    r'(?:within|in|with)\s+(?:your|the)\s+(?:organization|company|network)\b|'
    r'\bi\s+would\s+be\s+happy\s+to\s+share\s+my\s+(?:updated\s+)?resume\b|'
    r'\bpreferred\s+roles?\s*:'
)


def is_candidate_self_promo_email(text: str) -> bool:
    """True when a candidate/recruiter is advertising THEIR OWN
    availability for work ("Open to Work" style), not describing an
    actual role to be filled -- see _CANDIDATE_SELF_PROMO_INDICATORS."""
    if not text:
        return False
    return bool(_CANDIDATE_SELF_PROMO_INDICATORS.search(text))


# BUG FIX ("role: 'Jobs matching your criteria for...'" from an automated
# job-board digest listing 13 unrelated jobs / "role: 'iLabor Daily
# Digest...'" from an ATS system's own bulk requisition-release
# notification -- both confirmed real cases): neither describes ONE job
# to be filled -- each lists many, or is itself a notification ABOUT
# postings elsewhere, not a JD. is_job_requirement_email()'s keyword-count
# gate still fires on these (they're full of the same staffing vocabulary
# any real JD table uses), so full single-posting extraction ran on
# multi-row table text and produced nonsense. Covers both the job-board
# "job agent" digest shape (jobagent@..., "N new jobs found by your job
# agent", "Jobs matching your criteria") and the ATS bulk-notification
# shape (iLabor360-style "Requisition List" / "Requisitions Released
# from ... to ..." / "requisition(s) that have been released or
# edited") -- distinct senders, same underlying problem: many postings
# in one email rather than one.
_JOB_DIGEST_INDICATORS = re.compile(
    r'(?i)\bnew\s+jobs?\s+found\s+by\s+your\s+job\s+agent\b|'
    r'\bjobs?\s+matching\s+your\s+criteria\b|'
    r'\byour\s+job\s+agent\b|'
    r'\brequisition(?:s|\(s\))?\s+(?:released|that\s+have\s+been\s+released)\b|'
    r'\brequisition\s+list\b|'
    r'\brequisitions?\s+released\s+from\b.{0,20}\bto\b'
)


def is_job_digest_email(text: str) -> bool:
    """True for a multi-posting job-board digest ("N new jobs found...")
    or an ATS's own bulk requisition-release notification -- neither is
    a single job requirement to extract -- see _JOB_DIGEST_INDICATORS."""
    if not text:
        return False
    return bool(_JOB_DIGEST_INDICATORS.search(text))


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
            # BUG FIX ("role: 'Blue Yonder Consultant(SCPO Experience
            # Mandatory, IBP Nice to'" -- cut off mid-parenthetical, the
            # real value continues "have)" on the next physical line of
            # the source email): the pattern's ".+" can't cross a
            # newline at all, so the captured value itself already ends
            # exactly where the line wraps -- the missing continuation
            # lives just past match.end() in the ORIGINAL text, not
            # inside the match. A long parenthetical qualifier is a
            # common place for recruiter templates to wrap. Only pull in
            # that next physical line to CLOSE an unbalanced paren, not
            # the value's entire continuation, and only when that next
            # line is short (a genuine wrapped fragment, not another
            # paragraph/field) and actually contains the closing paren.
            if first_line.count('(') > first_line.count(')'):
                _after = text[match.end():match.end() + 80].lstrip('\n')
                _next_line = _after.split('\n', 1)[0].strip()
                if _next_line and len(_next_line) <= 40 and ')' in _next_line and ':' not in _next_line:
                    first_line = first_line.rstrip() + ' ' + _next_line
            value = sanitize_text(first_line)
            if not value:
                continue
            value = crop_at_next_field(value)
            if not value or is_email_body(value) or len(value) > 200:
                continue
            # BUG FIX ("Position -      1." parsed as role="1."): this
            # docstring's own earlier fix (earliest-match-wins) only helps
            # when a DIFFERENT, correctly-labeled match exists somewhere
            # else in the text to win instead — it doesn't help when the
            # bad match is the ONLY one found, which is exactly what
            # happens in a multi-posting broadcast email numbered
            # "Position -      1." / "Position-    2" (posting SEQUENCE
            # numbers, not a job title) alongside a "Role Name:" label
            # ROLE_PATTERNS didn't recognize at all. A value that's
            # purely digits (with optional trailing "." or ":") can never
            # be a real job title/position name under any of these
            # patterns — reject it here and let the next pattern (or
            # fallback tier) have a chance instead of "winning" on a bare
            # number.
            if re.fullmatch(r'\d+\s*[.:)]?', value.strip()):
                continue
            if best_pos is None or match.start() < best_pos:
                best_pos = match.start()
                best_value = value
    return best_value


# BUG FIX ("role: 'UNKNOWN'" from "Role: Contract\nJob Title: Business
# Intelligence Developer\n..."): parse_requirement's own role-resolution
# block already knows to reject a "Role:" value that's just an
# employment-type word ("Contract", "C2C", "W2") -- the real title
# usually sits under a separate "Job Title:"/"Position:" label elsewhere
# in the same email. But first_match() only ever returns the SINGLE
# earliest match across all ROLE_PATTERNS; once that one match gets
# rejected by the caller, nothing re-scans this pattern source for the
# NEXT, later match -- the whole source is abandoned and the fallback
# chain moves on to progressively weaker mechanisms that may find
# nothing at all. Wraps first_match() (reusing all of its existing
# crop/is_email_body/length guards unchanged) in a small retry loop: on
# a rejected match, slice past it and try again.
def first_valid_role_match(patterns: List[str], text: str) -> Optional[str]:
    remaining = text
    for _ in range(3):
        candidate = first_match(patterns, remaining)
        if not candidate:
            return None
        cand_lower = candidate.strip().lower().rstrip('.')
        if cand_lower not in _EMPLOYMENT_TYPE_BARE_WORDS and not _looks_like_generic_role_header(candidate):
            return candidate
        idx = remaining.lower().find(candidate.lower())
        if idx == -1:
            return None
        remaining = remaining[idx + len(candidate):]
    return None


# Words that commonly follow "hybrid" in a TECHNICAL sense unrelated to
# work arrangement (hybrid-cloud, hybrid identity architecture, etc.) --
# used to reject those matches so bare "hybrid" isn't mistaken for a work
# mode indicator when the email is just discussing infrastructure.
_HYBRID_NON_WORKMODE_CONTEXT = re.compile(
    r'(?i)^\s*[\-]?\s*(?:cloud|identity|architecture|infrastructure|'
    r'deployment|deployments|environment|environments|approach|strategy|'
    r'model|integration|integrations|connectivity|networking|network)\b'
)

# BUG FIX: "remote access solutions" / "remote desktop" / "remote support"
# / "remote monitoring" -- IT/networking job DUTIES the role performs --
# were being read as if "remote" stated the ROLE'S OWN work arrangement.
# Same shape as _HYBRID_NON_WORKMODE_CONTEXT above, checked against the
# words immediately AFTER "remote".
_REMOTE_NON_WORKMODE_CONTEXT = re.compile(
    r'(?i)^\s*(?:access|desktop|support|connectivity|monitoring|'
    r'management|administration|troubleshooting|login|session|sessions|'
    r'vpn|control|assistance|diagnostics)\b'
)

# BUG FIX ("work_mode: 'REMOTE'" from "Remote work is not an option; this
# is 100% onsite"): none of the WORK_MODE_PATTERNS/context checks had any
# negation awareness at all -- unlike extract_employment_types() below
# (see _NEGATION_BEFORE), which already handles "No C2C"/"Not W2". Here
# the negation sits AFTER the keyword ("Remote work is NOT an option"),
# not before it, so a leading-negation check wouldn't catch this shape --
# checked against the text immediately following the match instead.
_WORKMODE_TRAILING_NEGATION = re.compile(
    r'(?i)^\s*(?:work\s+)?(?:is\s+|isn\'?t\s+|is\s+not\s+)?'
    r'(?:not\s+(?:an?\s+)?option|not\s+available|not\s+possible|'
    r'no\s+longer\s+(?:an?\s+)?(?:option|available)|not\s+permitted|'
    r'not\s+allowed)\b'
)

# BUG FIX: "Remote"/"Hybrid"/"Onsite" glued directly onto an adjacent
# word with no space at all (e.g. "-RemoteAbout the Role", "F2FOnsite
# Flexibility:") makes the keyword invisible to its own \b-bounded regex
# entirely -- \b requires an actual word/non-word transition on both
# sides, and there is none between two glued letters. Confirmed this
# silently dropped a real "Remote" statement, letting an unrelated later
# "onsite" mention win by default. Inserts a space at exactly these glue
# points during normalize_text() -- see below.
_GLUED_WORKMODE_PATTERN = re.compile(
    r'(?<=[A-Za-z0-9])(?=(?:Remote|Hybrid|Onsite)\b)|'
    r'(?<=\b(?:Remote|Hybrid|Onsite))(?=[A-Z])'
)


def extract_work_mode(text: str) -> str:
    """Extract work mode from text.

    BUG FIX: previously iterated WORK_MODE_PATTERNS as a fixed-priority
    dict (REMOTE checked before HYBRID checked before ONSITE) and
    returned the first mode with ANY match ANYWHERE in the whole
    document. A JD stating "Work Type: Hybrid: Onsite 4 days Per Week,
    Remote Fridays" came back as REMOTE, purely because a bare "remote"
    keyword happened to exist somewhere later in the text -- even though
    "Hybrid" is the officially stated work type and appears earlier in
    the very same sentence. Now finds the EARLIEST matching keyword
    across all three modes (same earliest-wins approach first_match()
    already uses for other fields), so whichever mode is actually stated
    first in the document wins, regardless of pattern dict order.
    """
    if not text:
        return "UNKNOWN"
    text_lower = text.lower()
    best_pos = None
    best_mode = "UNKNOWN"
    for mode, patterns in WORK_MODE_PATTERNS.items():
        for pattern in patterns:
            for m in re.finditer(pattern, text_lower):
                if mode == 'HYBRID':
                    trailing = text_lower[m.end():m.end() + 25]
                    if _HYBRID_NON_WORKMODE_CONTEXT.search(trailing):
                        continue  # false positive -- try next occurrence
                if mode == 'REMOTE':
                    trailing = text_lower[m.end():m.end() + 25]
                    if _REMOTE_NON_WORKMODE_CONTEXT.search(trailing):
                        continue  # false positive -- try next occurrence
                if _WORKMODE_TRAILING_NEGATION.search(text_lower[m.end():m.end() + 40]):
                    continue  # negated -- try next occurrence
                if best_pos is None or m.start() < best_pos:
                    best_pos = m.start()
                    best_mode = mode
                break  # earliest valid match for this pattern is enough
    return best_mode


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


# BUG FIX: "Candidate should NOT be more than 15 years of experience" (a
# maximum cap) was being returned as if it were the real required
# experience level -- it's the only phrase in the email literally
# containing "years...experience" together, since the real "5+ years of
# backend software engineering..." requirement doesn't have the word
# "experience" immediately after "years" at all and never matched any
# pattern here in the first place.
_EXPERIENCE_CAP_CONTEXT = re.compile(
    r'(?i)\b(?:not\s+(?:be\s+)?more\s+than|no\s+more\s+than|should\s+not\s+exceed|'
    r'not\s+to\s+exceed|no\s+longer\s+than|less\s+than|under)\b[\s\S]{0,15}$'
)


def extract_experience(text: str) -> Optional[str]:
    """Extract experience requirement from text."""
    if not text:
        return None
    for pattern in EXPERIENCE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            preceding = text[max(0, match.start() - 40):match.start()]
            if _EXPERIENCE_CAP_CONTEXT.search(preceding):
                continue  # this occurrence is a maximum/cap, not a requirement
            exp = match.group(1).strip()
            year_match = re.search(r'\d+\+?\s*(?:-\s*\d+\s*)?years?', exp, re.IGNORECASE)
            if year_match:
                value = year_match.group(0)
                value = re.sub(r'(?i)\byrs?\.?\b', 'years', value)
                return value
            if re.fullmatch(r'\d+\+?', exp):
                return f"{exp} years"
            return exp
    for match in re.finditer(
        r'(\d+\+?)\s*[-\u2013]?\s*(?:\d+\+?\s*)?years?', text, re.IGNORECASE
    ):
        preceding = text[max(0, match.start() - 40):match.start()]
        if _EXPERIENCE_CAP_CONTEXT.search(preceding):
            continue
        return f"{match.group(1)} years"
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
    # Mainframe / legacy enterprise systems -- confirmed missing entirely
    # (a real mainframe JD's skills list matched almost nothing without
    # these; only "C++" was picked up out of ~20 real skills).
    'z/OS', 'ISPF', 'CICS', 'DB2', 'IMS', 'COBOL', 'PL/I', 'Assembler',
    'Rexx', 'JCL', 'VSAM', 'IDz', 'IBM File Manager', 'FastRexx',
    'BMC FileAid', 'CA Broadcom', 'Macro4', 'Microsoft PowerPoint',
    # Data engineering -- "ETL" itself was missing, despite being the
    # literal first word of one JD's title.
    'ETL', 'ELT', 'Snowflake', 'Azure Data Factory', 'Databricks',
    # BI / analytics tooling
    'Looker', 'LookML', 'BigQuery', 'Tableau', 'Power BI',
    # Modern DevOps / AI-assisted development tooling
    'GitHub Copilot', 'Bamboo', 'Bitbucket', 'GitHub', 'CloudFormation',
    # Project management tools & certifications
    'Jira', 'Confluence', 'PMP', 'CSM', 'PMI-ACP',
    # Accessibility / QA testing
    'WCAG', 'Section 508', 'ARIA', 'WAI-ARIA', 'axe-core', 'axe DevTools',
    'Lighthouse', 'Accessibility Insights', 'NVDA', 'JAWS', 'VoiceOver',
    'TalkBack', 'HTML', 'CSS', 'DOM',
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
    # BUG FIX: a genuinely long, thorough JD's own clean "Core Technology
    # Stack: GCP | Terraform | ... | FinOps" summary line sat at char
    # 7,388 -- past the old 6000-char window -- so it was completely
    # invisible to extraction even though it was the single cleanest,
    # most authoritative skill list in the whole email.
    text_scope = text[:10000]

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


# Recognized "bullet" glyphs for splitting a skills section into one
# chunk per item. Beyond the standard •/\u2022, this also covers the
# Word/Outlook middle-dot (·, U+00B7) and a curated set of emoji glyphs
# commonly used as bullets in "hiring alert" style recruiter emails.
_BULLET_CHAR_PATTERN = re.compile(
    r'[•\u2022\u00b7\u25E6\u2043\u2219\u2705\u2714\u2611'
    r'\U0001F539\U0001F538\U0001F525\u2B50\U0001F4CC\U0001F3E2'
    r'\U0001F4BC\U0001F4CD\U0001F6A8]'
    r'|\n\s*[-\*]\s'
)


# BUG FIX (ported from rap_python_cron's identical fix — skills list
# swallowing the sign-off / boilerplate that follows it): STOP_PATTERN
# alone only recognizes a small fixed set of labeled field boundaries
# (FIELD_BOUNDARIES) -- it has no idea a decorative separator line
# ("===============================", common in multi-posting broadcast
# emails) or a bare "Requirement 2:" / "Position 2:" sequence header
# (singular "Requirement" was never in FIELD_BOUNDARIES at all, only the
# plural "Requirements") mark the end of real content. Worse, STOP_
# PATTERN's own bare-heading branch requires the boundary word to be
# followed by ONLY whitespace before the line ends -- a sign-off written
# as "Thanks," (trailing comma, the overwhelmingly common real-world
# phrasing) never matches it, so the capture ran straight through the
# recruiter's own name, company, and email address as if they were
# skills.
#
# Three additional stop signals, all cropped to whichever occurs
# EARLIEST (same "take the minimum" approach crop_at_next_field() already
# uses for NEXT_FIELD_PATTERN vs SIGNATURE_PATTERN):
#   1. SIGNATURE_PATTERN -- the same sign-off-word pattern every other
#      field (role/client/location/rate/duration) already crops against
#      via crop_at_next_field(). It has no punctuation-adjacency
#      requirement (plain \b...\b), so "Thanks," is caught correctly.
#   2. A decorative separator line (5+ repeated =/-/_/* characters).
#   3. A bare "Requirement N" / "Position N" / "Posting N" sequence
#      header line, common in multi-posting broadcast emails.
#   4. A "Work Auth:"/"Work Authorization:" label -- not in
#      FIELD_BOUNDARIES or NEXT_FIELD_LABELS at all (only the bare word
#      "visa" is), so a skills section immediately followed by one ran
#      straight through it too.
_SKILLS_SEPARATOR_LINE_PATTERN = re.compile(r'(?:^|\n)[ \t]*[=\-_*]{5,}[ \t]*(?=\n|$)')
_SKILLS_NUMBERED_POSTING_HEADER_PATTERN = re.compile(
    r'(?im)(?:^|\n)[ \t]*(?:requirement|position|posting|opening|role)\s*#?\s*\d+\s*[:.\)]?[ \t]*(?=\n|$)'
)
_SKILLS_WORK_AUTH_LABEL_PATTERN = re.compile(r'(?im)(?:^|\n)[ \t]*work\s*auth(?:orization)?\s*[:\-]')


def _crop_skills_boilerplate(skills_text: str) -> str:
    if not skills_text:
        return skills_text
    cut = len(skills_text)
    for pattern in (
        SIGNATURE_PATTERN,
        _SKILLS_SEPARATOR_LINE_PATTERN,
        _SKILLS_NUMBERED_POSTING_HEADER_PATTERN,
        _SKILLS_WORK_AUTH_LABEL_PATTERN,
    ):
        m = pattern.search(skills_text)
        if m:
            cut = min(cut, m.start())
    return skills_text[:cut].strip()


# BUG FIX (ported from rap_python_cron's identical fix — skills list
# swallowing trailing "call to action" sentences): even after cropping at
# a signature/separator/numbered-header boundary above, a skills section
# that's the LAST labeled field before a plain CTA sentence with no field
# label and no sign-off word at all -- "Interested candidates send resume
# to john.mathew@...", "Send resumes to team@recruitfast.com", "Call me
# at 214-555-0198 anytime" -- has no boundary to stop at, so that sentence
# gets bullet/comma-split right alongside the real skills. None of these
# are skill-shaped: a real skill token is never a full imperative sentence
# directed at the reader, and never contains an email address or phone
# number attached to several words of surrounding prose.
_CTA_LEAD_PATTERN = re.compile(
    r'(?i)^(?:interested\s+candidates?|please\s+(?:send|share|contact|reach|call)|'
    r'kindly\s+(?:send|share)|send\s+(?:your\s+|updated\s+)?resumes?|'
    r'share\s+(?:your\s+|updated\s+)?resumes?|call\s+me|contact\s+(?:me|us)|'
    r'reach\s+out|feel\s+free\s+to|for\s+more\s+(?:details?|info(?:rmation)?)\b)'
)


def _is_cta_or_contact_sentence(token: str) -> bool:
    """True for a trailing recruiter call-to-action / contact-info sentence
    that isn't a real skill -- see the BUG FIX comment above."""
    if not token:
        return False
    stripped = token.strip()
    if _CTA_LEAD_PATTERN.match(stripped):
        return True
    word_count = len(stripped.split())
    if word_count > 4 and (_EMAIL_ADDR_PATTERN.search(stripped) or PHONE_PATTERN.search(stripped)):
        return True
    return False


def extract_skills(text: str) -> List[str]:
    """Extract skills from text as a list. Thin wrapper around
    _extract_skills_labeled() -- see that function for the real logic.

    BUG FIX ("skills: []" for a real requirement row where the body's
    own HTML-to-text conversion glued an entire bulleted skills list
    into one unbroken run with NO whitespace at all between items --
    e.g. "Must Have Skills:AndroidJavaKotlinObjective CProblem
    SolvingRESTful (Rest-APIs)Testing" -- confirmed via batch analysis
    against production data): the bare "skills:" label DID match here
    (correctly), but the captured text has no comma/space/bullet
    delimiter anywhere for the downstream splitter to break on, so it
    stayed one giant token that the length cap then rejected outright --
    an empty result, even though _extract_skills_labeled() found a real,
    correctly-labeled section. Previously this exact case accidentally
    produced a NON-empty but garbled result via a different, since-fixed
    pattern matching a much longer unrelated span first (see "tech
    stack"'s own BUG FIX comment) and winning on length -- fixing that
    correctly rejected the bad match, but left nothing to replace it.
    Falls back to the keyword scanner in this situation too (previously
    it only ran when NO label matched at all), since it can never make
    things worse: the keyword scanner works directly off known tech
    names and doesn't depend on the run-on text having any delimiters.
    """
    labeled = _extract_skills_labeled(text)
    if labeled:
        return labeled
    return extract_skills_from_keywords((text or '')[:6000])


def _extract_skills_labeled(text: str) -> List[str]:
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
    #
    # BUG FIX ("skills undercounted — a short 'Mandatory Skills' one-liner
    # used instead of a much richer 'Skill Requirements' bulleted section
    # later in the same email"): this used to take whichever pattern
    # matched FIRST in list order and break immediately, never checking
    # whether a later pattern would have found a more complete section.
    # Now tries every pattern and keeps whichever candidate section is
    # LONGEST after the same stop-boundary cropping already applied below
    # — the richer section wins regardless of which pattern found it, and
    # the single-match case (the overwhelming majority of emails) behaves
    # exactly as before since there's only one candidate to compare.
    for pattern in SKILLS_PATTERNS:
        match = re.search(pattern, text_scope, re.IGNORECASE | re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            stop_match = STOP_PATTERN.search(candidate)
            if stop_match:
                candidate = candidate[:stop_match.start()].strip()
            candidate = _crop_skills_boilerplate(candidate)
            if candidate and (skills_text is None or len(candidate) > len(skills_text)):
                skills_text = candidate
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
    #
    # BUG FIX: this only ever recognized •/\u2022 and a newline-anchored
    # -/* as "a bullet". Real recruiter emails routinely use OTHER glyphs
    # for the same purpose -- middle-dot "·" (a common Word/Outlook paste
    # artifact) and emoji "bullets" (✅ 🔹 🔥 ⭐ 📌 etc., common in
    # "exciting hiring alert" style emails) -- none of which were
    # recognized, so those sections fell through to the flat comma/"and"
    # splitter and got shredded mid-sentence. Also: when a Word/Outlook
    # <li> list collapses to plain text, each item lands on its own line
    # with NO leading symbol at all -- handled as a second fallback below.
    if _BULLET_CHAR_PATTERN.search(skills_text):
        bullet_chunks = _BULLET_CHAR_PATTERN.split(skills_text)
        skills = []
        for chunk in bullet_chunks:
            skills.extend(_extract_skill_tokens(chunk))
        return list(dict.fromkeys(skills))[:20]

    # No recognized bullet glyph anywhere -- but the section may still be
    # "one item per line" with nothing prepended (the <li> case above).
    # Treat that the same as a bulleted list rather than falling through
    # to the flat splitter, which shreds phrases like "MicroStrategy Admin
    # and Technical knowledge" into meaningless fragments on the word
    # "and". Deliberately conservative (>=3 short lines) so a single
    # glued run-on paragraph with no line breaks at all still falls
    # through to flat mode below, unchanged.
    line_candidates = [ln.strip() for ln in skills_text.split('\n') if ln.strip()]
    # BUG FIX: genuine per-<li>-item bullet lists can contain a few long,
    # detailed items (parenthetical examples lists, etc.) -- requiring
    # EVERY line under 150 chars rejected the WHOLE list over a single
    # long-but-legitimate bullet, falling back to flat comma-splitting
    # and shredding parenthetical examples into dangling fragments like
    # "GCP)" and "Cloud Run)". Raised to 300.
    if len(line_candidates) >= 3 and all(len(ln) < 300 for ln in line_candidates):
        skills = []
        for chunk in line_candidates:
            skills.extend(_extract_skill_tokens(chunk))
        if skills:
            return list(dict.fromkeys(skills))[:20]

    if is_email_body(skills_text):
        return extract_skills_from_keywords(text_scope)

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
            if _is_cta_or_contact_sentence(skill):
                continue
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


def _split_respecting_parens(text: str, delimiter_pattern: str) -> List[str]:
    """Split `text` on `delimiter_pattern` matches, but never inside
    parentheses -- so a bullet like "container technologies (Azure
    Functions, AWS Lambda, ..., Cloud Run)" survives as ONE token instead
    of being shredded at every comma inside the parenthetical examples
    list, leaving dangling fragments like "GCP)" and "Cloud Run)".
    """
    delim_re = re.compile(delimiter_pattern)
    parts = []
    depth = 0
    last = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '(':
            depth += 1
            i += 1
            continue
        if ch == ')':
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            m = delim_re.match(text, i)
            if m:
                parts.append(text[last:i])
                i = m.end()
                last = i
                continue
        i += 1
    parts.append(text[last:])
    return parts


_TRAILING_PAREN_LIST = re.compile(r'^(.*?)\(([^()]+)\)\.?$')


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
    # BUG FIX: a plain comma-split ran straight through parentheses too,
    # shredding "container technologies (Azure Functions, AWS Lambda,
    # Kubernetes/AKS, ECS/Fargate, GKE, Cloud Run)" into fragments with
    # dangling brackets ("GCP)", "Cloud Run)"). Respects paren depth now.
    tokens = _split_respecting_parens(core, r',\s*|\s+and\s+')
    results = []
    for tok in tokens:
        # Strip only sentence punctuation from the right; preserve leading
        # periods because ".NET" is a legitimate technology name.
        tok = tok.strip().rstrip('.')
        # Remove bullet/emphasis markup that can survive fallback splitting.
        tok = re.sub(r'^[\-\u2013]\s*', '', tok).strip()
        tok = tok.strip('*').strip()
        # Strip stray leading conjunction/hedge words a comma-split can leave
        # behind, e.g. ", and REST APIs" -> "and REST APIs" -> "REST APIs".
        tok = re.sub(r'(?i)^(?:and|preferably|or)\s+', '', tok).strip()
        tok_lower = tok.lower()
        if not tok or tok_lower in _GENERIC_SKILL_WORDS:
            continue
        # A trailing parenthetical after a generic category phrase
        # usually lists the REAL concrete technologies -- pull those out
        # individually instead of keeping one long, generic-prefixed blob
        # that then gets rejected by the length cap below, silently
        # dropping every real technology name inside it.
        paren_m = _TRAILING_PAREN_LIST.match(tok)
        if paren_m:
            inner_items = [i.strip() for i in paren_m.group(2).split(',') if i.strip()]
            if len(inner_items) >= 2:
                for item in inner_items:
                    item = re.sub(r'(?i)^(?:or|and)\s+', '', item).strip()
                    if 1 < len(item) < 45:
                        results.append(item)
                continue
        if _is_cta_or_contact_sentence(tok):
            continue
        if 1 < len(tok) < 45:
            results.append(tok)
    return results


# BUG FIX: recruiting-broadcast-portal emails (ProHires Powerhouse and
# similar mailing-list relays) put the ACTUAL recruiter's identity in the
# body as its own mini "From:" block, while the outer email header 'From'
# is just the broadcast platform's relay address (e.g.
# phph001@prohirespowerhouse.com). A typical embedded block looks like:
#     From:
#     Sonam Kumari,
#     Tanisha Systems
#     sonam.kumari@tanishasystems.com
#     Reply to:   sonam.kumari@tanishasystems.com
# Confirmed across every broadcast-sourced sample seen so far -- vendor
# extraction was silently attributing every one of these to the broadcast
# platform itself instead of the real recruiter.
_EMBEDDED_FROM_BLOCK = re.compile(
    r'(?im)^[ \t]*From\s*:\s*\n+[ \t]*'
    r'([A-Za-z][\w\'.\-]*(?:\s+[A-Za-z][\w\'.\-]*){0,3}),?[ \t]*\n+[ \t]*'
    r'([A-Za-z0-9&.,\-\' ]{2,60}?)[ \t]*\n+[ \t]*'
    r'([\w.+-]+@[\w\-]+\.[a-zA-Z]{2,})'
)

# Plain "Email: name@company.com" line -- common when a recruiter pastes
# their own contact info directly into the body (no embedded From: block).
_BODY_EMAIL_LABEL_PATTERN = re.compile(
    r'(?im)^[ \t]*email\s*[:\-]\s*([\w.+-]+@[\w\-]+\.[a-zA-Z]{2,})'
)

# BUG FIX: real HTML-derived signature blocks often have several
# whitespace-only lines between the sign-off word and the actual name,
# frequently containing stray non-breaking spaces (\xa0) or tabs mixed in
# with the blank lines -- a single "\n+[ \t]*" was too rigid to span that
# reliably. Handled procedurally below instead of as one monolithic regex.
_SIGNOFF_WORD_PATTERN = re.compile(r'(?i)\b(?:regards|thanks|sincerely|best\s*regards)\b')
_PLAIN_NAME_SHAPE = re.compile(r"^[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,3}$")


# A name sharing its line with a title, e.g. "MD Irfan | Senior Talent
# Acquisition" -- extracts just the leading name-shaped portion before
# the separator.
_NAME_WITH_TRAILING_TITLE = re.compile(
    r"^([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,3})\s*[|,\-]\s*\S"
)


def _extract_signoff_name(body: str) -> Optional[str]:
    """Find the first plausible person-name-shaped line following a
    sign-off word ("Thanks", "Regards", "Sincerely", ...). Scans line by
    line and stops at the first non-blank line -- if that line isn't
    name-shaped, gives up rather than scanning arbitrarily deep into
    unrelated JD text below the signature.
    """
    m = _SIGNOFF_WORD_PATTERN.search(body)
    if not m:
        return None
    # Resume scanning from the line AFTER the sign-off phrase, not
    # mid-line -- "Thanks" matches inside "Thanks and Regards," and the
    # rest of that same line ("and Regards,") isn't blank, which would
    # otherwise look like a non-name first line and bail out immediately.
    line_end = body.find('\n', m.end())
    if line_end == -1:
        return None
    for line in body[line_end + 1:].split('\n')[:30]:
        candidate = line.strip('\xa0').strip()
        if not candidate:
            continue
        if _PLAIN_NAME_SHAPE.match(candidate) and len(candidate) <= 40:
            return candidate
        # BUG FIX: some signatures put the name and title on the SAME
        # line, separated by "|"/","/"-" (e.g. "MD Irfan | Senior Talent
        # Acquisition") -- extract just the leading name-shaped portion
        # instead of requiring the whole line to be the name.
        title_m = _NAME_WITH_TRAILING_TITLE.match(candidate)
        if title_m and len(title_m.group(1)) <= 40:
            return title_m.group(1)
        return None
    return None


# Personal webmail domains -- a bare "vendor: Gmail" guess (capitalizing
# the domain) is never a real company name, so these always warrant
# checking the body for the real sender identity instead.
_PERSONAL_WEBMAIL_DOMAINS = {
    'gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'live.com',
    'aol.com', 'icloud.com', 'ymail.com', 'msn.com', 'rediffmail.com',
}


def extract_vendor_from_body(body: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Best-effort extraction of the REAL sender's name/company/email from
    the email body, for use when the header 'From' is a mailing-list /
    broadcast-portal relay address, or a personal webmail address with no
    useful display name. Returns (name, company, email); any may be None.

    BUG FIX ("client: 'person'" root cause investigation surfaced a second,
    smaller bug): _EMBEDDED_FROM_BLOCK already successfully captures the
    company name as its own group (see that regex's own docstring example,
    "Sonam Kumari, / Tanisha Systems"), but this function used to only
    return group(1) (the person's name) and group(3) (the email), silently
    throwing away the company every single time this path fired -- every
    broadcast-relay email of this shape lost its agency name for no reason.
    """
    if not body:
        return None, None, None

    m = _EMBEDDED_FROM_BLOCK.search(body)
    if m:
        name = m.group(1).strip().strip('"\'')
        company = m.group(2).strip().strip('"\'')
        email = m.group(3).strip().lower()
        return (name or None), (company or None), email

    email = None
    email_m = _BODY_EMAIL_LABEL_PATTERN.search(body)
    if email_m:
        email = email_m.group(1).strip().lower()

    name = _extract_signoff_name(body)
    return name, None, email


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
    
    important_fields = ['client', 'location', 'rate', 'employment_types', 'role', 'skills']
    valid_fields = 0
    
    for field in important_fields:
        value = parsed.get(field)
        if field == 'employment_types' or field == 'skills':
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

def _cap_real_words(text: str, max_words: int) -> str:
    """
    Truncate `text` to at most `max_words` REAL (contains a letter/digit)
    words, keeping any punctuation-only tokens ("-", "&", "/", "|") that
    fall before the cutoff.

    BUG FIX ("... AI/ML & API Integration Domain: ..." truncated to "...
    AI/ML & API", losing "Integration"): the plain `words[:N]` slice this
    replaces counted every whitespace-separated token as a full "word" —
    including a standalone connector like a lone "-" or "&", which are
    completely normal inside real job titles ("AI/ML & API Integration",
    "Full Stack - Backend Developer"). That wasted slots in the cap on
    non-content tokens and cut off real title words sitting right after
    them. Only tokens with at least one letter or digit count toward the
    word budget; a bare punctuation token is kept but doesn't consume a
    slot itself.
    """
    words = text.split()
    kept = []
    real_word_count = 0
    for w in words:
        if real_word_count >= max_words:
            break
        kept.append(w)
        if re.search(r'[0-9A-Za-z]', w):
            real_word_count += 1
    return ' '.join(kept)


# BUG FIX ("role: 'Data Solution Platform Architect'" — dropped a real,
# meaningful "(Snowflake)" technology qualifier from the end of a title,
# confirmed via full-dataset batch analysis against real production
# data): clean_role()'s trailing-parenthetical stripper below exists to
# drop administrative noise like "(Onsite Role)" or "(USC & H4 Only)",
# but it was unconditional -- any "(...)" sitting at the very end of a
# role got removed, including a genuine, meaningful qualifier like
# "(Snowflake)" that recruiters commonly tack onto a title to name the
# specific platform/technology. Checked against the same TECH_KEYWORDS
# vocabulary the skills extractor already uses elsewhere in this file --
# if the parenthetical's content contains a recognized technology/
# product name, it's kept; otherwise it's still stripped exactly as
# before.
def _parenthetical_is_meaningful(content: str) -> bool:
    if not content or not content.strip():
        return False
    for _kw, _pattern in _TECH_KEYWORD_PATTERNS:
        if _pattern.search(content):
            return True
    return False


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
    # Strip any leaked HTML tags FIRST — before crop_at_next_field or any
    # other regex below runs — so those operate on genuinely clean text
    # instead of potentially matching against tag fragments. See _ANY_TAG_RE
    # above for the full rationale.
    role = _ANY_TAG_RE.sub(' ', role)
    role = sanitize_text(role)
    if not role:
        return None
    role = crop_at_next_field(role)
    # Drop trailing parenthetical asides: "(Onsite Role)", "(USC & H4 Only)"
    # — but see _parenthetical_is_meaningful()'s comment above: a
    # technology/product name in the parenthetical is kept, not stripped.
    for _ in range(3):
        _m = re.search(r'\s*\(([^)]*)\)\s*$', role)
        if not _m:
            break
        if _parenthetical_is_meaningful(_m.group(1)):
            break
        role = role[:_m.start()].strip()
    # Drop leading marketing words: "Hiring!!", "Urgent -", "!!"
    # BUG FIX ("role: 'to Work - Senior US IT Recruiter'" from "Open to
    # Work - Senior US IT Recruiter | ..." / "role: 'Jobs matching your
    # criteria for...'" from "New Jobs matching your criteria for ..." --
    # both confirmed real cases): this unconditionally treated a leading
    # "Open"/"New" as generic marketing filler ("Opening", "New
    # Requirement") regardless of what followed -- but "Open to Work" (a
    # candidate/job-seeker self-promotion phrase) and "New Jobs matching"
    # (an automated job-board digest subject) are different, unrelated
    # senses of the same words, and stripping "Open"/"New" off the front
    # left a mangled fragment instead. Negative lookaheads exclude just
    # these two specific known false-positive phrases; every other
    # leading "Open"/"New" usage (the actual marketing-filler case this
    # strip exists for) is stripped exactly as before.
    role = re.sub(
        r'(?i)^\s*(?:hiring(?:\s*now)?|urgent|immediate|hot|'
        r'new(?!\s+jobs?\s+(?:matching|found|for\s+you))|'
        r'open(?:ing)?(?!\s+to\s+work)|apply)'
        r'\b[\s:\-!.]*', '', role
    )
    role = re.sub(r'^[^0-9A-Za-z]+', '', role).strip()
    # BUG FIX ("HELP DESK ANALYST II ." — the recruiter's own trailing
    # sentence-ending period survived all the way through): this stripped
    # a trailing dash/comma/colon/semicolon but never a period, so
    # "Job Title: HELP DESK ANALYST II ." kept that literal " ." on the
    # end. A trailing period on a role title is always just punctuation,
    # never meaningful title content, so it's safe to strip unconditionally
    # here (a real title using periods, like "Sr." or "R&D", has them
    # mid-string, not as the very last character).
    role = re.sub(r'[\-\u2013,:;.]+\s*$', '', role).strip()
    # BUG FIX: emoji used as field-label bullets in "hiring alert" style
    # emails (💼, 📍, 🏢, etc.) sit directly adjacent to the next field's
    # label with no space, so once crop_at_next_field() correctly stops
    # the value right at that label, the emoji glyph itself -- being on
    # the "value" side of the boundary -- is left dangling on the end.
    role = re.sub(r'[\U0001F300-\U0001FAFF\u2600-\u27BF]+\s*$', '', role).strip()
    # ROLE-SPECIFIC PARSING: real job titles are short — typically 2-8
    # words, but a legitimate "Role A or Role B" dual/alternate-title
    # posting (recruiters commonly broadcasting two acceptable titles for
    # the same req, e.g. "Supply Chain Data & KPI Analyst or SAP IBP
    # Business/Data Analyst") runs longer while still being entirely real
    # title content, not runaway sentence text. If crop_at_next_field()
    # didn't find a clean boundary (e.g. HTML-collapsed single-line emails
    # with no recognizable "Location:"/signature marker nearby), this cuts
    # off at the point runaway sentence text starts, instead of falling
    # through to a blunt 60-char truncation that grabs unrelated trailing
    # words like "AWS Engineer so on more unwanted...". Capped at 12 (was
    # 8) to comfortably fit a real two-title posting while still catching
    # genuine runaway text well before it reaches a 60-char cutoff.
    words = role.split()
    if len(words) > 12:
        role = _cap_real_words(role, 12)
    # BUG FIX: this blunt char-cap ran regardless of the word-cap outcome
    # above, so raising the word cap alone wasn't enough — a real 10-word
    # dual-title role like "Supply Chain Data & KPI Analyst or SAP IBP
    # Business/Data Analyst" (64 chars) still got hard-cut mid-word here.
    # Raised to 80 to match role_from_subject()'s own length limit, so
    # both fallback paths agree on how long a real title is allowed to be.
    if len(role) > 80:
        role = role[:77] + '...'
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
    # Same HTML-tag safety net as clean_role() — see _ANY_TAG_RE's comment.
    client = sanitize_text(_ANY_TAG_RE.sub(' ', client))
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
    # BUG FIX ("client: 'Rule Engines / Decision Engines'"): a real
    # company/client name is never phrased as a slash-separated list of
    # alternatives -- that shape is exactly how recruiter emails phrase
    # location/schedule OPTIONS instead ("Charlotte, NC / New Jersey
    # (Hybrid) / US Remote (Preferred)"), and is a strong sign the
    # extractor grabbed a tool/skill phrase instead of an actual client.
    if ' / ' in client:
        return None
    # ROLE-SPECIFIC PARSING: real client/company names are short (2-10 words).
    # Same runaway-text problem as role — cap word count before falling
    # back to a blunt character truncation.
    words = client.split()
    if len(words) > 10:
        client = _cap_real_words(client, 10)
    if len(client) > 50:
        client = client[:47] + '...'
    return client or None


_LOCATION_PREFERENCE_PATTERN = re.compile(
    r'(?i)\b(?:preference|preferred|ideally|nice\s+to\s+have|'
    r'in\s+or\s+near|candidates?\s+(?:in|near|located|based))\b'
)

# BUG FIX ("location: 'SAP'" from a SailPoint Developer email whose body
# happened to list "...Active Directory, LDAP, JDBC, Delimited File, Web
# Services, SAP, Oracle, and cloud applications..." as connector/
# integration technologies — confirmed on a real requirement row): a bare
# 2-6 letter all-caps enterprise-software/tech acronym is never itself a
# real location value, but nothing validated that a non-AI OR AI-sourced
# "location" was actually location-shaped before accepting it. A short
# curated list of the acronyms most likely to appear near a location
# field in a JD (identity/access-management and generic enterprise
# jargon) — deliberately NOT the full TECH_KEYWORDS list, since that
# includes multi-word/longer names ("React.js", "PostgreSQL") that could
# never be mistaken for a location anyway and this only needs to catch
# the short bare-acronym case.
_LOCATION_NON_LOCATION_ACRONYMS = {
    'sap', 'ldap', 'jdbc', 'rbac', 'sod', 'iiq', 'isc', 'mfa', 'sso',
    'vpn', 'api', 'apis', 'crm', 'erp', 'etl', 'bi', 'rest', 'soap',
    'json', 'xml', 'sql', 'saml', 'oauth', 'ad', 'hris', 'jd', 'atf',
    'ci', 'cd', 'cicd',
}


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
    # BUG FIX ("location: 'SAP'" — see _LOCATION_NON_LOCATION_ACRONYMS'
    # comment above): reject outright when the ENTIRE value is just one
    # of these bare tech acronyms, regardless of which extractor (AI or
    # regex) produced it — a real location is never a single bare
    # 2-6 letter enterprise-software acronym. Returning None here lets
    # the caller's existing "if not location: <try next fallback>" logic
    # move on to a better candidate instead of keeping this one.
    if location.strip().lower() in _LOCATION_NON_LOCATION_ACRONYMS:
        return None
    # BUG FIX ("location: 'design'" from an Adobe Architect email — the
    # real location, "NYC, NY Hybrid 3 days", sat in a clean stacked-bold
    # block near the top, but the AI extractor instead picked up a
    # lowercase word from deep in an unrelated bulleted responsibility,
    # "Design end-to-end experience architecture...", confirmed on a real
    # requirement row): a genuine location value — a city name, a state,
    # or a Remote/Hybrid/Onsite keyword — is always capitalized in normal
    # recruiter-template usage. A value that starts with a lowercase
    # letter is essentially always a stray word plucked out of running
    # prose, not a real location. Same reject-and-let-the-caller-retry
    # approach as the acronym check above.
    if location[0].islower():
        return None
    # Same HTML-tag safety net as clean_role() — see _ANY_TAG_RE's comment.
    location = sanitize_text(_ANY_TAG_RE.sub(' ', location))
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
    # Same emoji-glyph-glued-to-next-label cleanup as clean_role() -- see
    # that function's comment for the full explanation.
    location = re.sub(r'[\U0001F300-\U0001FAFF\u2600-\u27BF]+\s*$', '', location).strip()
    return location or None


def clean_rate(rate: Optional[str]) -> Optional[str]:
    """Clean rate — handles range ($55-65/hr), single ($65/hr), annual ($120k)."""
    if not rate:
        return None
    rate = sanitize_text(normalize_text(rate))
    if not rate:
        return None
    # Same HTML-tag safety net as clean_role() — see _ANY_TAG_RE's comment.
    rate = sanitize_text(_ANY_TAG_RE.sub(' ', rate))
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
    # Same HTML-tag safety net as clean_role() — see _ANY_TAG_RE's comment.
    duration = sanitize_text(_ANY_TAG_RE.sub(' ', duration))
    if not duration:
        return None
    duration = crop_at_next_field(duration)
    # BUG FIX: neither numeric pattern allowed a "+" between the number and
    # its unit ("6+ Months"), so anything phrased that way failed BOTH
    # numeric patterns and fell all the way through to the bare "contract"
    # match -- e.g. "6+ Months Contract" came back as just "Contract",
    # silently dropping the actual length. Also added a dedicated
    # "long term contract" alternative ahead of the bare "long term" one,
    # so "Long Term Contract" is captured as one phrase instead of
    # stopping at "Long Term" and dropping "Contract". Both numeric
    # patterns now optionally capture a trailing "Contract"/"Contract to
    # hire" word too, via the unnamed trailing group -- m.group(0) below
    # already returns the whole match, contract-suffix included.
    duration_patterns = [
        r'(\d+)\s*[-\u2013]\s*(\d+)\+?\s*(months?|weeks?)(?:\s*(contract(?:\s*to\s*hire)?))?',
        r'(\d+)\+?\s*(months?|weeks?|days?)(?:\s*(contract(?:\s*to\s*hire)?))?',
        r'(long\s*term\s*contract)',
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
    r'hire our it recruiter at just \$|'
    # BUG FIX ("Technical Business Analyst" JD misclassified as a
    # hotlist broadcast via its own footer, role came out UNKNOWN): a
    # common recruiter footer convention asks the reader to reciprocally
    # add the sender to their distribution list and share their own
    # hotlist back. That phrase is a genuine hotlist-broadcast signal on
    # its own, but it's boilerplate signature noise tacked onto an
    # otherwise perfectly normal single-role JD, not a description of
    # the CURRENT email. Truncating it here, like every other footer
    # signature, keeps it from ever reaching is_hotlist_email() at all.
    r'please\s+add\s+(?:my\s+)?email(?:\s*id)?\s+to\s+(?:your\s+)?distribution'
)

# BUG FIX ("JR Project Manager SC Locals Only..." and "Datacenter Lead
# Madison, WI..." both real, fully-detailed single-role JDs -- silently
# dropped as newsletter/spam, confirmed on a real batch export): this
# portal (PROHIRES POWERHOUSE) puts its unsubscribe/mailing-list-signup
# boilerplate at the very TOP of the body, not the bottom -- e.g. "Remove/
# unsubscribe | Update your contact and subscribed mailing list(s) |
# Subscribe to mailing list(s) to receive requirements & resumes". The
# existing _BOILERPLATE_FOOTER_PATTERN above only handles TRAILING
# footers (it truncates everything from the match point to the end of
# the text) -- naively adding this phrase to that same pattern would
# truncate the ENTIRE email, including the real JD that follows it.
# This is a separate, narrow pattern for known LEADING boilerplate: it
# excises just the matched header span itself (not everything after it),
# and only looks in the first 600 characters, so it can never accidentally
# eat real JD content deeper in a normal email. Once this span is
# removed, regex_classifier.py's NEWSLETTER_KEYWORDS check (which looks
# for the word "unsubscribe" anywhere in the body) no longer sees it and
# stops misfiring on emails from this portal.
_LEADING_BOILERPLATE_HEADER_PATTERN = re.compile(
    r'(?i)remove\s*/\s*unsubscribe\s*\|\s*update\s+your\s+contact\s+and\s+subscribed\s+'
    r'mailing\s+list\(s\)\s*\|\s*subscribe\s+to\s+mailing\s+list\(s\)\s+to\s+receive\s+'
    r'requirements?\s*&\s*resumes'
)


def strip_boilerplate_footer(text: str) -> str:
    """Truncate `text` at the earliest known TRAILING boilerplate-footer
    signature, if any, and excise any known LEADING boilerplate header
    (see _LEADING_BOILERPLATE_HEADER_PATTERN above) -- the two use
    different strip strategies (truncate-to-end vs excise-just-the-span)
    because one sits at the bottom of the email and one at the top.
    Leaves text unchanged when neither is detected."""
    if not text:
        return text
    lead_m = _LEADING_BOILERPLATE_HEADER_PATTERN.search(text[:600])
    if lead_m:
        text = text[:lead_m.start()] + text[lead_m.end():]
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

    # BUG FIX: footer/mailing-list boilerplate was only ever stripped
    # inside extract_skills() -- every OTHER field extractor scanned the
    # raw, unstripped text. A Google Group literally named "C2C
    # REQUIREMENTS" was leaking into employment_types as if the email
    # itself had stated a real C2C requirement. Stripping once here,
    # before any field extraction runs, protects every field the same
    # way skills extraction already was.
    safe_body_for_parsing = strip_boilerplate_footer(safe_body)

    # Normalize BEFORE the is_job_requirement_email gate so that HTML-collapsed
    # labels (e.g. "LeadLocation:") get un-glued and register as indicators.
    full_text = normalize_text(f"{safe_subject}\n{safe_body_for_parsing}")
    norm_body = normalize_text(safe_body_for_parsing)

    # BUG FIX (ported from rap_python_cron's identical fix): is_hotlist_email()
    # was being checked against full_text (subject+body combined) here, same
    # as is_job_requirement_email() just before it -- but is_hotlist_email()
    # only has ONE positive-signal-free indicator, a bare `\bhotlist\b`
    # match, and staffing broadcast subject lines routinely use "Hotlist" as
    # a generic label for "batch of postings" even when the body is a single
    # genuine job requirement (e.g. "Hotlist :: Multiple Openings" relaying
    # one real posting). Combining subject+body meant that word alone,
    # anywhere in the subject, discarded an otherwise well-formed real
    # posting. is_job_requirement_email() stays on full_text (a POSITIVE
    # signal -- subject words like "Requirement"/"Contract" genuinely help
    # there); is_hotlist_email() is scoped to norm_body only.
    if not is_job_requirement_email(full_text) or is_hotlist_email(norm_body):
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
    # then body-only regex to prevent subject-line poisoning, then a
    # bold/emphasis-wrapped bare title (common template: role stated
    # plainly at the top with no label at all, just "*Role Name*"), then a
    # "Role: N" sequence-number header with the real title unlabeled on
    # the next line.
    #
    # BUG FIX (multi-posting email "Role: 1 / <Title 1>" and "Role: 2 /
    # <Title 2>" both came out as the SAME generic "Multiple
    # Requirements" — the shared email subject): first_match(...,
    # full_text) — the body+subject-combined fallback — used to run
    # BEFORE role_from_numbered_label below. Since full_text prepends the
    # SAME subject ahead of every segment's own body, and a generic
    # broadcast subject can itself match a ROLE_PATTERNS label (e.g.
    # "Urgent Hiring: Multiple Requirements" matches the "hiring:"
    # pattern), that subject-sourced match won for every segment before
    # the per-segment numbered-label fallback ever got a turn — the exact
    # same subject-leaking-into-full_text class of bug already fixed once
    # for "QA Engineer requirement - San Jose", just via a different
    # ROLE_PATTERNS entry this time. Moved the full_text fallback to AFTER
    # every body-only fallback (bold-lead, numbered-label) — it's a last-
    # resort catch for the rare case where the body truly has no usable
    # signal at all, not something that should out-rank a more specific,
    # already-successful body-only signal.
    role = clean_role(_ai_field('role', unknown_value='UNKNOWN'))
    # BUG FIX: some recruiter templates reuse the "Role:" label to mean
    # engagement/employment type ("Role: Contract") instead of job title,
    # with the real title sitting under a separate "Job Title:"/
    # "Position:" label elsewhere in the same email. The AI extractor
    # sometimes takes that literal "Role:" label at face value and
    # returns the engagement type itself ("Contract", "C2C", "Full
    # Time", etc.) as if it were the job title. Discard a role value that
    # reduces to nothing but a known employment-type word/phrase so the
    # regex fallback chain below (which already prefers "Job Title:"/
    # "Position:" over a bare "Role:" label via first_match()'s
    # earliest-match-wins logic) gets a chance to find the real title.
    _employment_terms = {
        term for terms in EMPLOYMENT_KEYWORDS.values() for term in terms
    }
    if role and role.strip().lower().rstrip('.') in _employment_terms:
        role = None
    if role and _looks_like_generic_role_header(role):
        role = None
    # BUG FIX (see _role_echoes_non_role_label's comment above): catches
    # the AI extractor grabbing a DIFFERENT field's labeled value
    # (Visas:, Interview Mode:, ...) instead of the real "Role:" label.
    if role and _role_echoes_non_role_label(role, full_text):
        role = None
    if not role:
        # BUG FIX (ported from rap_python_cron's identical fix):
        # role_from_subject() has no way to tell a real title apart from
        # a generic broadcast phrase in the subject line itself ("New
        # Job Opening", "Urgent Requirement", "Job Alert", "Job
        # Opportunity") — it just returns whatever's there, cleaned up.
        # Since it ran BEFORE role_from_body_lead() in this chain, that
        # non-empty (but useless) result won over a real, specific title
        # sitting right there in the body purely by running first —
        # role_from_body_lead(), the correct fallback for exactly this
        # case, never got a turn. Reject a subject-derived role that's
        # just one of these generic phrases and let body_lead try first
        # instead; only fall back to the generic phrase as an actual
        # last resort if body_lead ALSO finds nothing (still better than
        # "UNKNOWN").
        _subject_role = clean_role(role_from_subject(safe_subject))
        if _subject_role and _GENERIC_SUBJECT_ROLE_PATTERN.match(_subject_role.strip()):
            _subject_role = None

        # BUG FIX: this used to be a single `or` chain, with the
        # employment-term/generic-header checks applied only once, to
        # whichever candidate won the whole chain. That meant a generic
        # value from an EARLY candidate (e.g. first_match(ROLE_PATTERNS,
        # norm_body) returning "Developer" from a "Role name:      Developer"
        # placeholder line) won the `or` immediately and short-circuited
        # every later candidate -- including a perfectly good, specific
        # title sitting in the subject or elsewhere in the body. The
        # rejection then ran on that already-won "Developer" value, found
        # it generic, and set role = None entirely (-> "UNKNOWN") instead
        # of falling through to try the next candidate, which is exactly
        # what every BUG FIX comment on the individual fallbacks above
        # assumes will happen. Reject and skip forward per-candidate
        # instead, so a generic/employment-term match anywhere in the
        # chain correctly defers to the next one rather than winning by
        # default or blanking the result.
        def _first_real_role(*candidates):
            for candidate in candidates:
                if not candidate:
                    continue
                if candidate.strip().lower().rstrip('.') in _employment_terms:
                    continue
                if _looks_like_generic_role_header(candidate):
                    continue
                if _role_echoes_non_role_label(candidate, full_text):
                    continue
                return candidate
            return None

        role = _first_real_role(
            clean_role(first_valid_role_match(ROLE_PATTERNS, norm_body)),
            clean_role(role_from_bold_lead(norm_body)),
            clean_role(role_from_numbered_label(norm_body)),
            clean_role(first_valid_role_match(ROLE_PATTERNS, full_text)),
            _subject_role,
            clean_role(role_from_body_lead(norm_body)),
            clean_role(role_from_subject(safe_subject)),
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
            # BUG FIX ("client: 'Person'" from "...Interview – In-Person
            # Client Interviw Requirement –..."): the separator dash here had
            # no whitespace requirement on the leading side, so it matched
            # the hyphen glued inside an ordinary compound word ("In-Person")
            # just as readily as a real "Title – Client" field separator --
            # capturing "Person" (with the coincidental " Client" text right
            # after it satisfying the lookahead) as if it were a company
            # name. Require whitespace immediately before the dash too, same
            # as the second fallback right below already does, so it can
            # only fire on an actual separator, not a hyphenated word.
            dash_m = re.search(
                r'(?i)(?:role|position|opening|title)\s*[:\-]\s*[^\n]+?'
                r'[ \t][\-\u2013][ \t]*([A-Z][A-Za-z0-9&\ \t]{2,30}?)(?=[ \t]*(?:Location|Client|Rate|\n|$))',
                norm_body
            )
            if not dash_m:
                # BUG FIX ("client: 'UI Automation Tools'" / "client:
                # 'Playwright and'", both from ordinary bullet-list lines in
                # a JD's skills section): this fallback's \s+ around the
                # dash matches ANY whitespace, including newlines -- so it
                # doesn't actually require the "<text> - <Value>" construct
                # to sit on one line at all. It was matching the END of one
                # bullet, the newline into the NEXT bullet, and that
                # bullet's leading "- Capitalized Words" as if they were one
                # continuous "Title - Client" phrase (bulleted JDs have this
                # shape on nearly every line, so it always finds something).
                # A real inline "Title - Client" convention is always a
                # single visual line -- restrict every whitespace gap here
                # to horizontal whitespace only ([ \t], never \n) so the
                # whole match is forced to stay on one line, same fix
                # applied to the first fallback above.
                dash_m = re.search(
                    r'[A-Za-z ]{4,}[ \t]+[\-\u2013][ \t]+([A-Z][A-Za-z0-9&\ \t]{2,30}?)'
                    r'(?=[ \t]*(?:Location|Client|Rate|\n|$))',
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
                # BUG FIX ("client: 'No H1'" from "...$90-$110/HR Target -
                # No H1" -- a visa-restriction note sitting in a quoted
                # forwarded subject line inside the body, not a company
                # name): this fallback's trigger side ("[A-Za-z ]{4,}") is
                # deliberately loose so it can catch a plain "Role - Client"
                # line with no real label at all -- but that means it also
                # fires on ordinary rate/visa clauses ending in a dash, and
                # a bare negation word ("No H1", "No OPT", "No C2C", "Not
                # Applicable") looks exactly like a short capitalized
                # company name to it. A real client name never starts with
                # a bare negation, so reject on that alone.
                _cand_first_word = cand_lower.split()[0].strip(',.:;') if cand_lower else ''
                is_negation_phrase = _cand_first_word in ('no', 'not', 'none')
                if cand and len(cand) <= 40 and not is_email_body(cand) and not is_role_echo and not is_negation_phrase:
                    client = cand

    # BUG FIX ("client: 'Requisition List Client Name Req'" -- confirmed
    # real case): reject a client value outright when it's just an
    # ATS/recruiter-template column header (or several concatenated) --
    # see _looks_like_generic_client_header()'s comment. Never gated on
    # _CLIENT_LABEL_PRESENT_RE -- a value that's nothing but reproduced
    # table-header text is never a real client name, regardless of
    # whether some other genuine client signal also exists elsewhere in
    # the email.
    if client and _looks_like_generic_client_header(client):
        client = None

    # BUG FIX: reject a client value that's just the role/JD title's own
    # technology or product name/acronym echoed back (e.g. role "Oracle
    # E-Business Suite (EBS) Upgrade" producing client "EBS", or role
    # "Data Engineer with Manufacturing Domain EXP" producing client
    # "Data Engineer"). The AI/regex fallback latches onto the role text
    # whenever the email never actually names an end client (common
    # phrasing: "one of our clients", or no client mention at all).
    # GATED on _CLIENT_LABEL_PRESENT_RE finding no explicit client label
    # anywhere in the email at all -- without that gate, this guard would
    # also wrongly null out a perfectly real "Client: Oracle" sitting
    # next to a role like "Oracle DBA", since "oracle" is a substring
    # either way. Only apply the echo check when there's no genuine
    # client-labeled field to have grounded the value in the first
    # place -- that's when it's actually just a role echo, not a real
    # client whose name happens to overlap with the role text.
    if client and not _CLIENT_LABEL_PRESENT_RE.search(full_text):
        _client_lower = client.strip().lower()
        _role_lower = (role or '').strip().lower()
        if _client_lower and _role_lower and (
            _client_lower == _role_lower
            or _client_lower in _role_lower
            or _role_lower in _client_lower
        ):
            client = None
        # BUG FIX ("client: 'Data Engineer - AI/ML - Louisville'" -- the
        # subject line was "Data Engineer - AI/ML - Louisville, Kentucky
        # (DAY 1 onsite)", and the body never named an end client at all,
        # only the sending recruiter's own agency in passing prose --
        # confirmed real case): same shape of AI-fabrication as the role
        # echo just above, but sourced from the SUBJECT line instead of
        # the role field. Checked the same way -- only when no genuine
        # client label grounds the value -- against the raw subject text.
        _subject_lower = (safe_subject or '').strip().lower()
        if client and _client_lower and _subject_lower and (
            _client_lower in _subject_lower
        ):
            client = None

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
        if not location:
            location = find_city_state(normalize_text(safe_subject), reject_first_words=_SIGNOFF_WORDS)
        # BUG FIX ("location: None" for ~150 real requirement rows out of a
        # ~3,650-row sample — confirmed via batch analysis against real
        # production data): a very common posting shape states ONLY a bare
        # work-mode word ("...Remote", "!!Remote----Offshore", "(Onsite)")
        # with no "Location:" label at all and no city/state anywhere in
        # the email — e.g. "Need - Oracle EBS Technical Architect   Remote"
        # or "UiPath & Selenium Automation Developer (Onsite)". Every tier
        # above requires either a labeled field or a validated city/state
        # pair, so this extremely common case fell all the way through to
        # None even though the posting is completely unambiguous about
        # being Remote/Hybrid/Onsite. Reuses the same earliest-match work-
        # mode detector the dedicated work_mode field already relies on
        # (see extract_work_mode() below) as a last-resort location value
        # — a bare "Remote"/"Hybrid"/"Onsite" is itself a perfectly valid,
        # commonly-used location value (clean_location() already handles
        # it correctly when it comes from an explicit label).
        if not location:
            _bare_work_mode = extract_work_mode(full_text)
            if _bare_work_mode != 'UNKNOWN':
                location = _bare_work_mode.capitalize()

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
    #
    # BUG FIX ("onsite in Dallas/Austin/Bay Area/Irvine or Remote" came
    # back as REMOTE — the body's OWN primary descriptor, "onsite",
    # completely overridden): extract_work_mode() picks whichever mode's
    # keyword appears EARLIEST in the given text — but this called it on
    # full_text (subject + body), and the subject line here ends with
    # "...or Remote." (a short echo of the body's own "onsite ... or
    # Remote" phrase). Since the subject is concatenated BEFORE the body,
    # "Remote" from the subject's tail always sits earlier in the
    # combined string than "onsite" appearing partway through the body's
    # own sentence — winning for a reason unrelated to which mode is
    # actually primary. Try body text alone first (same body-then-
    # full_text fallback shape role/title extraction already use), and
    # only fall through to including the subject when the body itself
    # says nothing about work mode at all.
    work_mode = _ai_field('work_mode', unknown_value='UNKNOWN') or extract_work_mode(norm_body)
    if work_mode == 'UNKNOWN':
        work_mode = extract_work_mode(full_text)

    ai_employment_types = _ai_field('employment_types', unknown_value=['UNKNOWN'])
    employment_types = ai_employment_types or extract_employment_types(full_text)

    experience = _ai_field('experience') or extract_experience(full_text)

    # BUG FIX (ported from rap_python_cron's identical addition): this
    # field was never computed at all here — extract_work_authorization()
    # now exists above (see that section's own comment), this just wires
    # it into the actual field-assembly flow the same way experience/
    # skills already are.
    work_authorization = _ai_field('work_authorization') or extract_work_authorization(full_text)

    ai_skills = _ai_field('skills')
    skills = ai_skills if ai_skills else extract_skills(full_text)

    # BUG FIX: reject a client value that's actually one of the extracted
    # skills/technologies echoed back (e.g. skill "Salesforce" or "Oracle
    # EBS" coming out as the client too, when the email never names a real
    # end client). This is the second half of the client-echo guard above
    # — `skills` isn't known yet at that point, so it's checked here
    # instead. Same gate as above: only applies when there's no explicit
    # client label anywhere in the email, so a genuine "Client: Oracle"
    # next to "Skills: Oracle, PL/SQL" is left alone instead of being
    # wrongly nulled out.
    if client and skills and not _CLIENT_LABEL_PRESENT_RE.search(full_text):
        _client_lower = client.strip().lower()
        for _skill in skills:
            if not isinstance(_skill, str):
                continue
            _skill_lower = _skill.strip().lower()
            if not _skill_lower or _skill_lower == 'unknown':
                continue
            if (_client_lower == _skill_lower
                    or _client_lower in _skill_lower
                    or _skill_lower in _client_lower):
                client = None
                break

    # ── Vendor info ───────────────────────────────────────────────────────
    vendor_name = None
    vendor_email = None
    vendor_name_from_domain_guess = False
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
            vendor_name_from_domain_guess = True

    # BUG FIX: the header 'From' is frequently NOT the actual recruiter --
    # either a mailing-list/broadcast-portal relay address (ProHires
    # Powerhouse and similar), or a personal webmail address with no
    # display name at all (which previously fell back to a useless
    # capitalized-domain guess like "vendor: Gmail"). In both cases the
    # real name/email is usually sitting in the body itself -- an embedded
    # "From:" mini-header (broadcast format), a plain "Email: x@y.com"
    # line, or a "Thanks & Regards / <Name>" sign-off. Only overrides the
    # header result when the header itself looked untrustworthy, so a
    # genuine "Real Name <real@company.com>" header is never touched.
    from_domain = vendor_email.rsplit('@', 1)[-1].lower() if vendor_email and '@' in vendor_email else ''
    header_looks_untrustworthy = (
        vendor_name_from_domain_guess
        or not vendor_name
        or from_domain in _PERSONAL_WEBMAIL_DOMAINS
    )
    if header_looks_untrustworthy:
        body_name, body_company, body_email = extract_vendor_from_body(safe_body)
        if body_email:
            vendor_email = body_email
        if body_name:
            vendor_name = body_name
        if body_company and body_company.strip().lower() != (body_name or '').strip().lower():
            vendor_name = f"{vendor_name} ({body_company})" if vendor_name else body_company

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
        'work_authorization': work_authorization,
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
            anchors.append((m.start(), line_start, value, False))
    anchors.sort(key=lambda a: a[0])
    return anchors


# JD section headers that must NEVER be mistaken for a bare job-title
# anchor, even in the (rare) case one happens to be followed by something
# location-shaped. Not exhaustive by design -- the blank-line + immediate
# location-line requirement in _find_bare_title_anchors() is the real
# safety margin; this is a belt-and-suspenders check for the most common
# recurring header phrases.
_SECTION_HEADER_EXCLUSIONS = {
    'job description', 'job summary', 'role overview', 'key responsibilities',
    'core responsibilities', 'primary responsibilities', 'required skills',
    'required experience', 'preferred qualifications', 'qualifications',
    'requirements', 'responsibilities', 'non-negotiable requirements',
    'program delivery', 'venue readiness', 'cross-functional coordination',
    'change management', 'risk & issue management', 'installation oversight',
    'operational readiness', 'stakeholder management', 'reporting',
    'leadership responsibilities', 'nice to have', 'preferred',
    'must-have skills', 'must have skills', 'key skills', 'soft skills',
    'core skills', 'technical skills', 'key responsibilities:',
}
_TITLE_CONNECTOR_WORDS = {'of', 'and', 'the', 'for', 'or', 'in', 'on', 'to', '&', 'a', 'an'}
# A job title essentially never ends with a company-name suffix -- guards
# against lines like "Agile Enterprise Solutions Inc." (a recruiter's own
# company name in their email signature) being mistaken for a posting
# title just because a company address on the next line happens to
# contain a "City, ST" pattern.
_COMPANY_SUFFIX_WORDS = {
    'inc', 'inc.', 'llc', 'llc.', 'ltd', 'ltd.', 'corp', 'corp.',
    'corporation', 'solutions', 'systems', 'technologies', 'technology',
    'consulting', 'group', 'partners', 'associates', 'staffing', 'services',
}


def _looks_like_bare_job_title(line: str) -> bool:
    line = line.strip()
    if not line or line.endswith(':'):
        return False
    if not (3 <= len(line) <= 60):
        return False
    if line.lower() in _SECTION_HEADER_EXCLUSIONS:
        return False
    # BUG FIX (role captured as "Location"/"Work Model"/"Contract" —
    # confirmed on real, live requirement rows: two separate senders both
    # produced role="Location", another produced role="Work Model", and
    # two different itbtalent.com emails both produced role="Contract"):
    # a template that puts the field LABEL on its own line with the
    # value on the line(s) after it, instead of "Label: value" on one
    # line — e.g.
    #     Location
    #     Onsite/Remote
    # — has no colon on the label's own line at all, so NEXT_FIELD_
    # PATTERN (which requires a trailing ":"/"-") never matches it, and
    # the bare word "Location" then passed every other check here (a
    # single short capitalized word) followed by a location-shaped
    # value line right after it — exactly the "bare title + location"
    # shape this function exists to detect. Same failure mode for a
    # bare employment-type value ("Contract") sitting alone on its own
    # line, immediately followed by a client/location-shaped line.
    # Reject both: any line that IS (not just contains) a recognized
    # field label word, or IS a bare employment-type keyword — reusing
    # NEXT_FIELD_LABELS and EMPLOYMENT_KEYWORDS so this can't drift out
    # of sync with what those already recognize.
    if line.lower() in NEXT_FIELD_LABELS or line.lower() in _EMPLOYMENT_TYPE_BARE_WORDS:
        return False
    if NEXT_FIELD_PATTERN.search(line):
        return False
    # BUG FIX (role captured as "H1B"/"USC"/"H4 EAD" — a "consultant
    # hotlist" table's STATUS column, not a job title at all): a table
    # like "SKILL SET | EXP | STATUS | RELOCATION" converts to plain
    # text as one cell per line, so a status cell ("H1B") sitting alone
    # on its own line, immediately followed by the RELOCATION cell on
    # the next line ("Remote" — a genuine location-shaped string),
    # matched this function's title check (single short capitalized
    # "word") and the location check right after it — exactly the "bare
    # title + location" shape this function looks for — so the
    # segmenter split the table into a fake posting per row, each with
    # the visa/work-auth code as its "role". Reject a line that's just
    # visa/work-authorization shorthand up front: reuses the same
    # _WORK_AUTH_TOKEN_PATTERN vocabulary the work-authorization
    # extractor itself already relies on elsewhere, so this can't drift
    # out of sync with what counts as a visa code, and a genuine job
    # title never looks like this (real titles aren't bare 2-6 char
    # all-caps/digit codes with nothing else on the line).
    if len(line) <= 10 and _WORK_AUTH_TOKEN_PATTERN.search(line):
        return False
    words = line.split()
    if not (1 <= len(words) <= 7):
        return False
    if words[-1].strip('.,()').lower() in _COMPANY_SUFFIX_WORDS:
        return False
    for w in words:
        core = w.strip('()')
        if not core or core.lower() in _TITLE_CONNECTOR_WORDS:
            continue
        if not core[0].isupper():
            return False
    return True


def _looks_like_location_line(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    if find_city_state(line):
        return True
    return bool(re.search(r'(?i)\b(onsite|on-site|remote|hybrid)\b', line))


def _find_bare_title_anchors(text: str) -> List[tuple]:
    """Detect additional distinct-posting anchors for JDs that introduce
    each posting with a bare, UNLABELED title line (no "Job Title:"/
    "Role:" prefix) followed -- after a blank line -- by a location-
    shaped line, e.g.:
        Logistics Lead

        Los Angeles, CA-Onsite

        Rate-$55

    BUG FIX: _find_role_label_anchors() alone only recognizes labeled
    postings ("Job Title:", "Role:", etc.) — a real multi-posting email
    with this bare-title style had two whole job postings (each with
    substantial, hard non-negotiable requirements) silently swallowed
    into whichever labeled posting happened to be open at that point in
    the text, never becoming their own requirement rows at all.
    Deliberately narrow (short title line + blank line + immediate
    location-shaped line) to keep false-positive risk low — ordinary JD
    section headers essentially never have a location-shaped line
    immediately following a blank line.
    """
    anchors = []
    lines = text.split('\n')
    offsets = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1

    # Reject any candidate sitting shortly after a sign-off word ("Thanks",
    # "Regards", ...) -- almost always still inside THAT posting's own
    # signature block (a name/company/address triple, not a new posting),
    # confirmed via a real false-positive ("Agile Enterprise Solutions
    # Inc." followed by an address line) just 16-26 chars after a sign-off.
    # A genuinely new posting after a forwarded-email chain sits much
    # farther out (600+ chars in the confirmed multi-posting case), so a
    # short window here doesn't risk suppressing that.
    signoff_ends = [m.end() for m in SIGNATURE_PATTERN.finditer(text)]

    n = len(lines)
    for i in range(n):
        if any(0 <= offsets[i] - se < 350 for se in signoff_ends):
            continue
        if not _looks_like_bare_job_title(lines[i]):
            continue
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        if j > i + 1 and j < n and _looks_like_location_line(lines[j]):
            anchors.append((offsets[i], offsets[i], lines[i].strip(), True))
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

    raw_anchors = _find_role_label_anchors(body_text) + _find_bare_title_anchors(body_text)
    raw_anchors.sort(key=lambda a: a[0])
    if len(raw_anchors) < 2:
        return [body_text]

    accepted: list = []
    for pos, line_start, value, is_bare in raw_anchors:
        if accepted:
            prev_pos, _prev_line_start, prev_value, _prev_bare = accepted[-1]
            if pos - prev_pos < _ANCHOR_MIN_GAP and value.strip().lower() == prev_value.strip().lower():
                # Same role restated close together — not a second posting.
                continue
        accepted.append((pos, line_start, value, is_bare))

    if len(accepted) < 2:
        return [body_text]

    accepted = accepted[:max_segments]

    segments = []
    for i, (_, line_start, value, is_bare) in enumerate(accepted):
        seg_end = accepted[i + 1][1] if i + 1 < len(accepted) else len(body_text)
        segment = body_text[line_start:seg_end].strip()
        if is_bare:
            # BUG FIX: a bare-title anchor's segment has no "Role:"/"Job
            # Title:" label of its own -- without this, parse_requirement()
            # finds no labeled role INSIDE the segment and falls back to
            # the one shared email subject line for every segment, so a
            # multi-posting email with unlabeled titles (e.g. "Logistics
            # Lead", "Deputy Cluster Telecom Operations Manager") had
            # every such posting come back with the SAME wrong role
            # (whatever the first labeled posting's role happened to be).
            # Prepending a synthetic "Role:" line gives this segment's own
            # extraction the correct title immediately.
            segment = f"Role: {value}\n{segment}"
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