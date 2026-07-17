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
    r'(?is)remove/unsubscribe.*',
    r'(?is)to unsubscribe from this group.*',
    r'(?is)you received this message because.*',
    r'(?is)-----original message-----.*',
    r'(?is)---+\s*forwarded message\s*---+.*',
    r'(?is)on .+ wrote:.*',
    r'(?is)from:.*sent:.*to:.*subject:.*',
    r'(?is)click here to unsubscribe.*',
    r'(?is)this email was sent to.*',
    r'(?is)copyright.*all rights reserved.*',
    r'(?is)confidentiality notice.*',
    r'(?is)this message.*intended only for.*',
    r'(?is)if you have received this.*in error.*',
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

    # Convert HTML to plain text if needed
    if "<html" in text.lower() or "<body" in text.lower():
        text = html_to_text(text)

    # Remove noise patterns
    for pattern in NOISE_PATTERNS:
        text = re.sub(pattern, "", text)

    # Normalize whitespace only (do NOT rewrite content)
    text = re.sub(r'\n{3,}', '\n\n', text)  # max 2 newlines
    text = re.sub(r' {2,}', ' ', text)       # max 1 space
    text = text.strip()

    return text
