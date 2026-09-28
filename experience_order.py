# experience_order.py
# ---------------------------------------------------------------------------
# Single source of truth for how a consultant's work experience is ordered:
# current role first, then the most recent previous role, then older ones.
#
# Used everywhere experience rows are written (add / edit / delete / the
# legacy reorder endpoint / the Base Resume editor save) to renumber
# sort_order, AND everywhere they are read for display or resume generation,
# so the order is correct even for rows saved before this rule existed
# (no data migration needed). Manual drag-to-reorder was removed in favour
# of this automatic chronological order.
# ---------------------------------------------------------------------------

from __future__ import annotations

from typing import Iterable, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ConsultantExperience


def _ordinal(d) -> int:
    return d.toordinal() if d else 0


def experience_sort_key(exp):
    """Sort key: current role(s) first (latest start first), then past roles
    by end date newest-first (start date when no end date), then start date,
    then roles with no dates at all. Exact ties keep their previous relative
    order (sort_order, then id) so the result is stable and deterministic."""
    is_present = bool(getattr(exp, "is_present", False))
    start = getattr(exp, "start_date", None)
    end = getattr(exp, "end_date", None)
    if is_present:
        group, primary = 0, start
    elif end or start:
        group, primary = 1, (end or start)
    else:
        group, primary = 2, None
    return (
        group,
        -_ordinal(primary),
        -_ordinal(start),
        getattr(exp, "sort_order", 0) or 0,
        getattr(exp, "id", 0) or 0,
    )


def sort_experiences_chronologically(experiences: Iterable) -> List:
    """Return a new list ordered most-recent-first (see experience_sort_key)."""
    return sorted(experiences, key=experience_sort_key)


async def renumber_experiences_chronologically(
    db: AsyncSession, consultant_id: int
) -> List[ConsultantExperience]:
    """Re-assign sort_order 0..N for one consultant's rows in chronological
    order. Call after adding/editing/deleting rows and BEFORE anything that
    re-reads them (resume text sync, resume_info JSON sync). Only touches
    rows whose position actually changed. Returns the rows in the new order."""
    result = await db.execute(
        select(ConsultantExperience).where(ConsultantExperience.consultant_id == consultant_id)
    )
    ordered = sort_experiences_chronologically(result.scalars().all())
    for idx, exp in enumerate(ordered):
        if exp.sort_order != idx:
            exp.sort_order = idx
    return ordered
