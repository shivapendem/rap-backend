# =============================================================
# Phase 2 - Task 4: Footer, Unsubscribe and Thread Cleaner
# Removes email noise before JD hash creation
# =============================================================

import re
from html.parser import HTMLParser


# ---------------------------------------------------------------------------
# Noise patterns to remove
# ---------------------------------------------------------------------------
NOISE_PATTERNS = [
    r'(?im)^.*remove/unsubscribe.*$',
    r'(?is)to unsubscribe from this group.*',
    r'(?is)you received this message because.*',
    r'(?is)-----original message-----.*',
    r'(?is)---+\s*forwarded message\s*---+.*',
    r'(?is)on .+ wrote:.*',
    # BUG FIX: the trailing ".*" here was unbounded AND (?s) makes "."
    # match newlines, so once a forwarded-message header block was found,
    # this deleted EVERYTHING from that point to the end of the string --
    # not just the header itself. A real email forwarding a batch of
    # positions ("FW: Open Positions") had every posting AFTER the
    # forwarded header silently wiped from the stored job_description.
    # Bounded to the header block itself: non-greedy up through "Subject:"
    # and its own line only (no (?s) on that final segment), so it stops
    # at the end of the Subject line instead of consuming the rest of the
    # document.
    r'(?is)from\s*:.*?\bsent\s*:.*?\bto\s*:.*?\bsubject\s*:[^\n]*\n?',
    r'(?is)click here to unsubscribe.*',
    r'(?is)this email was sent to.*',
    r'(?is)copyright.*all rights reserved.*',
    r'(?is)confidentiality notice.*',
    r'(?is)this message.*intended only for.*',
    r'(?is)if you have received this.*in error.*',
    # BUG FIX: nothing here ever stripped recruiter sign-offs / signature
    # blocks ("Thanks & Regards, <name>, <company>, <phone>, <disclaimer>").
    # That whole block flowed straight through into job_description, which
    # is what jd_hash / dedup_key are built from AND what phase4.score_match
    # scans (first 1500 chars) for skills — so short JDs regularly had a
    # sender's name/company/phone number counted as part of the "job
    # description" for hashing and matching purposes, exactly the "clean
    # footer/thread text" step the parser pipeline is supposed to do before
    # jd_hash creation. Matches a sign-off line (start of line, only the
    # sign-off phrase + optional punctuation, nothing else) and removes it
    # plus everything after — mirrors parser.py's own FIELD_BOUNDARIES
    # sign-off words, but anchored to line boundaries so it doesn't eat "in
    # regards to ..." or "thanks for the update" appearing mid-sentence.
    r'(?ism)^[ \t]*(?:thanks\s*(?:&|and)?\s*(?:regards|best)|warm(?:est)?\s*regards|'
    r'kind\s*regards|best\s*regards|regards|many\s+thanks|sincerely\s+yours|'
    r'sincerely|yours\s+(?:truly|sincerely|faithfully)?|thank\s+you|thanks|best)'
    r'\s*[,.:]*[ \t]*$\n?.*',
]


class HTMLToTextParser(HTMLParser):
    """Simple HTML to plain text converter."""

    # Tags whose START should force a real line break — paragraph/row/list
    # level structure. NOTE: td/th are deliberately NOT here — two cells in
    # the same row (e.g. "Role:" | "Java Developer") usually belong on one
    # logical line, so they get a space (below), not a hard newline.
    _BLOCK_TAGS = {
        "br", "p", "div", "li", "tr", "table", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self._skip_tags = {"script", "style", "head"}
        self._current_skip = False

    def _last_char(self):
        for part in reversed(self.text_parts):
            if part:
                return part[-1]
        return ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._skip_tags:
            self._current_skip = True
            return
        if tag in self._BLOCK_TAGS:
            self.text_parts.append("\n")
        elif self._last_char() not in ("", "\n", " ", "\t"):
            # BUG FIX: previously ONLY br/p/div/li/tr inserted any separator.
            # Real recruiter HTML (Outlook/Word-pasted tables, <span>-only
            # markup) puts each field in its own <td>/<span> with zero
            # whitespace between tags in the source, e.g.
            #   ...Trintech</td><td>Location: Drive, Plano...
            # With no separator inserted here, that becomes the literal
            # string "TrintechLocation: Drive, Plano..." — which is exactly
            # the "TrintechLocation:" garbage seen in the Requirements table.
            # A plain space (not a newline) is enough to stop labels/values
            # fusing into one token, without breaking same-row "Label: Value"
            # pairs onto separate lines.
            self.text_parts.append(" ")

    def handle_endtag(self, tag):
        if tag.lower() in self._skip_tags:
            self._current_skip = False

    def handle_data(self, data):
        if not self._current_skip:
            self.text_parts.append(data)

    def get_text(self):
        return "".join(self.text_parts)


def is_junk_plain_text(text: str) -> bool:
    """
    Detect a broken vendor "plain text alternative" that is actually raw,
    untagged CSS (e.g. some ATS mailers dump `@import url(...);` and
    `.class{...}` rules straight into text/plain with no wrapping tags at
    all). Real JD text sometimes contains stray braces/semicolons further
    down (e.g. skill matrices), so this is scoped to the first 2000 chars
    to avoid false positives on legitimate long postings.

    BUG FIX: this function didn't exist in this copy of cleaner.py at
    all — pipeline.py (the manual "Reparse" path) used body_text
    unconditionally whenever it was non-empty, with no way to detect
    this specific failure mode. The cron project's copy of this file
    already had this check (plus strip_css_junk below); this backend
    copy had drifted behind it.
    """
    if not text:
        return False
    head = text[:2000]
    if "@import url(" in head or "-webkit-text-size-adjust" in head:
        return True
    return len(re.findall(r'\.[a-zA-Z][\w-]*\s*\{', head)) >= 3


def strip_css_junk(text: str) -> str:
    """
    Remove CSS @import/@media/rule-block junk from text that
    is_junk_plain_text() has flagged as containing it.

    BUG FIX ("Failed" status / garbage role like "1{color:#333;...}"
    extracted instead of the real job title): some ATS mailers dump raw,
    untagged CSS directly into the plain-text body, with the REAL
    message sandwiched in the middle or after it. Detecting the junk
    isn't enough on its own — when there's no body_html to fall back to,
    the raw CSS-plus-content was passed straight through to the parser
    as-is. normalize_text()'s own "un-glue a field label HTML-collapse
    fused onto a preceding word" step then actively made this worse: it
    saw ".mTitle-1{color:#333;...}", recognized "Title-" as a real
    field-label pattern, and inserted a space right before it — creating
    a brand-new, fake word boundary that let the role-extraction regex
    match "Title:" inside a CSS class name and capture the CSS body
    itself as the job title.

    Strips just the CSS-shaped chunks (import statements, @media blocks,
    and flat "selector{prop:val;...}" rule blocks) and keeps everything
    else, so the real content — which real-world testing confirms is
    often still fully present, just sandwiched between CSS blocks —
    survives instead of the row failing or saving garbage.
    """
    if not text:
        return text
    text = re.sub(r'@import\s+url\([^)]*\)\s*;?', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'@media[^{]*\{(?:[^{}]|\{[^{}]*\})*\}', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'[.\#]?[\w][\w\-]*(?:\s*,\s*[.\#]?[\w][\w\-]*)*\s*\{[^{}]*\}', ' ', text)
    return re.sub(r'[ \t]+', ' ', text).strip()


def html_to_text(html: str) -> str:
    """Convert HTML to plain text."""
    if not html:
        return ""
    try:
        parser = HTMLToTextParser()
        parser.feed(html)
        return parser.get_text()
    except Exception:
        # Fallback: strip all HTML tags with regex
        return re.sub(r'<[^>]+>', ' ', html)


def clean_requirement_text(text: str) -> str:
    """
    Task 4: Main cleaner function.
    Removes noise while preserving exact JD content.
    """
    if not text:
        return ""

    text = text.replace("\x00", "").replace("\u0000", "")

    # Convert HTML to plain text if needed
    if "<html" in text.lower() or "<body" in text.lower():
        text = html_to_text(text)

    # Remove noise patterns
    for pattern in NOISE_PATTERNS:
        text = re.sub(pattern, "", text)

    # Normalize whitespace robustly
    text = text.replace('\xa0', ' ').replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t]+', ' ', text)      # Collapse horizontal space
    text = re.sub(r' \n', '\n', text)        # Remove trailing spaces
    text = re.sub(r'\n ', '\n', text)        # Remove leading spaces
    text = re.sub(r'\n+', '\n', text)        # Collapse multiple newlines into a single newline
    text = text.strip()

    return text.replace("\x00", "").replace("\u0000", "")