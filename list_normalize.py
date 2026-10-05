"""
list_normalize.py
-----------------
Server-side safety net for list-like profile fields (Skills, Preferred Roles).

The Add / Edit User screens stop a user from adding the same chip twice, but
the API can also be reached by other screens, imports and direct calls, so the
backend collapses duplicates itself. Rules (kept deliberately conservative):

  * Comparison is case-insensitive and ignores surrounding whitespace
    ("React", " react " and "REACT" are the same item).
  * The FIRST spelling and the original order are kept.
  * A value with no real duplicates is returned EXACTLY as received (no
    re-spacing, no re-joining), so this can never alter data that was fine.
  * Items are split on "," only - the same separator every other reader of
    these columns already uses (e.g. ``primary_skills.split(",")``).
  * Never raises; if collapsing would leave nothing, the input is returned
    untouched so the existing "required" validators stay in charge.
"""

from __future__ import annotations

from typing import List, Optional


def dedupe_list(items: Optional[List[str]]) -> Optional[List[str]]:
    """Case-insensitive de-dupe of a list of strings, first spelling wins."""
    if not items:
        return items
    seen = set()
    out: List[str] = []
    for item in items:
        if not isinstance(item, str):
            return items  # unexpected shape - leave it to other validators
        key = item.strip().lower()
        if not key:
            out.append(item)  # blanks are handled by existing validators
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out if len(out) != len(items) else items


def dedupe_csv(value: Optional[str]) -> Optional[str]:
    """Case-insensitive de-dupe of a comma-separated string."""
    if not value or not isinstance(value, str) or "," not in value:
        return value
    parts = [p.strip() for p in value.split(",")]
    seen = set()
    kept: List[str] = []
    for p in parts:
        if not p:
            continue
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        kept.append(p)
    original_count = sum(1 for p in parts if p)
    if not kept or len(kept) == original_count:
        return value  # nothing to collapse -> byte-for-byte unchanged
    return ", ".join(kept)
