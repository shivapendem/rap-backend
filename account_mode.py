# account_mode.py
# ---------------------------------------------------------------------------
# Single source of truth for Public/Private visibility.
#
# WHY THIS EXISTS
# Previously every gmail_emails / requirements row carried its own copy of the
# account's mode, and a DB trigger rewrote ALL of an account's rows whenever an
# admin flipped the mode.  For a 400k-email account that is a multi-hour write
# across 20-40 indexes: the request timed out, held a pooled DB connection and
# could take the whole app down.
#
# NOW: the mode lives only on gmail_accounts.  Rows are linked to their account
# by the (normalised) account_email, and visibility is decided at read time.
#
# RULES (kept identical to the old behaviour for rows with no matching account):
#   * row linked to a gmail account  -> the ACCOUNT's mode decides
#   * row with NO matching account    -> the row's own stored mode decides
#                                        (old rule: public when 'public'/NULL)
#
# gmail_accounts is tiny (a handful of rows), so we read it once per request
# and inline the result as constants -- no join / correlated subquery on the
# 400k-row tables.
# ---------------------------------------------------------------------------
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import and_, func, or_, text


class AccountModes:
    """Normalised (lower/trim) account emails split by visibility."""

    __slots__ = ("private", "known")

    def __init__(self, private: Iterable[str] = (), known: Iterable[str] = ()):
        self.private = frozenset(private)
        self.known = frozenset(known) | self.private


def _norm(value: Optional[str]) -> str:
    return (value or "").lower().strip()


async def load_account_modes(db) -> AccountModes:
    """One tiny query (gmail_accounts has only a few rows)."""
    rows = (await db.execute(
        text("SELECT email, mode FROM gmail_accounts WHERE email IS NOT NULL")
    )).all()
    private, known = set(), set()
    for email, mode in rows:
        e = _norm(email)
        if not e:
            continue
        known.add(e)
        # Same complement as the old rule: only 'public' (or NULL) is public.
        if (mode or "public").lower().strip() != "public":
            private.add(e)
    return AccountModes(private, known)


def effective_mode(row_mode: Optional[str], account_email: Optional[str], modes: AccountModes) -> str:
    """Mode to show / enforce for a single row (python side)."""
    e = _norm(account_email)
    if e in modes.private:
        return "private"
    if e in modes.known:
        return "public"
    return (row_mode or "public").lower()


# ---------------------------------------------------------------------------
# SQLAlchemy (ORM) clause builders
# ---------------------------------------------------------------------------
def private_clause(mode_col, account_email_col, modes: AccountModes):
    """True when the row is effectively PRIVATE.  NULL-safe on both columns."""
    norm = func.lower(func.trim(func.coalesce(account_email_col, "")))
    parts = []
    if modes.private:
        parts.append(norm.in_(sorted(modes.private)))
    unlinked_private = func.coalesce(mode_col, "public") != "public"
    if modes.known:
        unlinked_private = and_(unlinked_private, norm.not_in(sorted(modes.known)))
    parts.append(unlinked_private)
    return or_(*parts) if len(parts) > 1 else parts[0]


def public_clause(mode_col, account_email_col, modes: AccountModes):
    """True when the row is effectively PUBLIC (old: mode == 'public' OR NULL)."""
    return ~private_clause(mode_col, account_email_col, modes)


def public_or_own_clause(mode_col, account_email_col, modes: AccountModes, own_emails):
    """Public rows, plus rows received on one of `own_emails`.

    own_emails: a single email (str) or an iterable of emails (already
    lower/trim'd by callers, re-normalised here defensively).
    """
    if isinstance(own_emails, str):
        own_emails = [own_emails]
    own = sorted({_norm(e) for e in (own_emails or []) if _norm(e)})
    pub = public_clause(mode_col, account_email_col, modes)
    if not own:
        return pub
    return or_(pub, func.lower(func.trim(account_email_col)).in_(own))


# ---------------------------------------------------------------------------
# Raw-SQL fragment builder (for text() queries on gmail_emails)
# ---------------------------------------------------------------------------
def public_or_own_sql(alias: str, modes: AccountModes, params: dict, own_param: str = "recruiter_email", prefix: str = "am") -> str:
    """Return a SQL boolean fragment (and fill `params`) equivalent to
    public_or_own_clause for `<alias>.mode` / `<alias>.account_email`.

    The IN-lists are tiny and bound as individual named parameters so no
    driver-specific array handling is required.
    """
    norm = f"LOWER(TRIM(COALESCE({alias}.account_email, '')))"

    def _in(names, tag):
        keys = []
        for i, v in enumerate(sorted(names)):
            k = f"{prefix}_{tag}_{i}"
            params[k] = v
            keys.append(f":{k}")
        return ", ".join(keys)

    parts = []
    if modes.private:
        parts.append(f"{norm} IN ({_in(modes.private, 'p')})")
    unlinked = f"COALESCE({alias}.mode, 'public') <> 'public'"
    if modes.known:
        unlinked = f"({unlinked} AND {norm} NOT IN ({_in(modes.known, 'k')}))"
    parts.append(unlinked)
    private_sql = "(" + " OR ".join(parts) + ")"
    return f"(NOT {private_sql} OR LOWER(TRIM({alias}.account_email)) = :{own_param})"
