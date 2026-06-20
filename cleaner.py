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

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self._skip_tags = {"script", "style", "head"}
        self._current_skip = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._skip_tags:
            self._current_skip = True
        if tag.lower() in ("br", "p", "div", "li", "tr"):
            self.text_parts.append("\n")

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
