import re


def is_email_body(text: str) -> bool:
    """Return True if text looks like a full email body rather than a field value."""
    if not text:
        return False
    sentences = re.split(r'[.!?]\s+', text)
    if len(sentences) > 2 and len(text) > 100:
        return True
    # BUG FIX ("Job Description- Senior GIS Programmer" -- confirmed real
    # case, role came out 'UNKNOWN' for a genuine, fully-detailed real
    # posting; ported from the identical fix in the cron copy of this
    # file): this phrase-based check fired regardless of text LENGTH -- a
    # short, clearly title-shaped 39-character string merely starting
    # with a "Job Description-" label prefix tripped the same heuristic
    # meant to catch an entire multi-paragraph email body. Add the same
    # length guard the sentence-count check above already uses.
    if len(text) <= 100:
        return False
    email_patterns = [
        r'job\s+description', r'responsibilities', r'qualifications',
        r'benefits', r'about\s+company', r'thank\s+you', r'best\s+regards'
    ]
    text_lower = text.lower()
    for pattern in email_patterns:
        if re.search(pattern, text_lower):
            return True
    return False