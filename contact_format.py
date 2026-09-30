"""Consultant contact fields: US phone number + LinkedIn URL.

Backend mirror of the frontend's src/lib/contactFormat.ts. Used ONLY for a
CONSULTANT's phone / LinkedIn URL (profile save, admin/recruiter consultant
edits, admin consultant create, Base Resume save). Admin/Recruiter mobile
number, extension and LinkedIn (email signature fields) are NOT touched.

Regression rule: these helpers only ever ADD formatting. A value that is a
valid US number / LinkedIn URL is stored in the standard form; anything
else is returned unchanged so each request model's EXISTING checks decide
exactly as they did before. The frontend's own profile saves re-send the
consultant's stored phone/LinkedIn with unrelated edits (Skills, Work
Auth, Employment Type), so a stricter backend rejection here would block
every save for a consultant whose stored value predates this rule.
"""
import re
from typing import Optional
from urllib.parse import urlsplit

US_PHONE_EXAMPLE = "+1 (469) 392-4030"

_PHONE_CHARS = re.compile(r"^\+?[\d\s\-().]+$")
_US_TEN_DIGITS = re.compile(r"^[2-9]\d{2}[2-9]\d{6}$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.I)
_LINKEDIN_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")


def format_us_phone(raw: Optional[str]) -> Optional[str]:
    """'+1 (469) 392-4030' for a valid US number in any common typing
    style (4693924030, 469-392-4030, (469) 392 4030, +1 469.392.4030,
    1-469-392-4030), else None. Area code and exchange must start 2-9."""
    s = (raw or "").strip()
    if not s or not _PHONE_CHARS.match(s):
        return None
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        if not (len(digits) == 11 and digits[0] == "1"):
            return None
        digits = digits[1:]
    elif len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if not _US_TEN_DIGITS.match(digits):
        return None
    return f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def normalize_linkedin_url(raw: Optional[str]) -> Optional[str]:
    """'https://<host>/<path>' when the input is a LinkedIn profile URL
    (with or without http(s):// and www.), else None. The real host must
    be linkedin.com or a subdomain of it, and a profile path is required."""
    s = (raw or "").strip()
    if not s or re.search(r"\s", s):
        return None
    if not re.match(r"^https?://", s, re.I):
        if _SCHEME.match(s) or s.startswith("//"):
            return None
        s = "https://" + s
    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if parts.username or parts.password or port:
        return None
    host = (parts.hostname or "").lower()
    # linkedin.com itself or a real subdomain of it (www., in., uk. ...)
    if not _LINKEDIN_HOST.match(host):
        return None
    if not parts.path.replace("/", ""):
        return None
    out = f"https://{host}{parts.path}"
    if parts.query:
        out += "?" + parts.query
    if parts.fragment:
        out += "#" + parts.fragment
    return out


def phone_for_storage(value: Optional[str]) -> Optional[str]:
    """US-formatted when valid; otherwise the value unchanged (None stays None)."""
    if value is None:
        return None
    return format_us_phone(value) or value


def linkedin_for_storage(value: Optional[str]) -> Optional[str]:
    """Normalized https:// URL when valid; otherwise the value unchanged."""
    if value is None:
        return None
    return normalize_linkedin_url(value) or value
