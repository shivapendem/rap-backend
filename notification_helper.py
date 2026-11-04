# notification_helper.py
#
# NOTE: this file is identical in the backend and the cron service. Keep the
# two copies in sync.
import os
import re
import time
from datetime import datetime, timedelta, timezone

from models import (
    User,
    Notification,
    Consultant,
    RecruiterConsultant,
    Requirement,
    RequirementConsultantMatch,
)
from sqlalchemy import and_, delete, func, text, update
from sqlalchemy.future import select


# ---------------------------------------------------------------------------
# Schema: notifications.consultant_id (nullable)
#
# Base.metadata.create_all() never adds a column to an existing table, so the
# column is added here, idempotently. It is called from the backend startup
# and lazily (once per process) from every writer below, so the cron service
# works whichever process boots first. Never raises.
# ---------------------------------------------------------------------------
_schema_ready = False


async def ensure_notification_schema(engine=None) -> bool:
    global _schema_ready
    if _schema_ready:
        return True
    try:
        if engine is None:
            from database import engine as _engine
            engine = _engine
        async with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                have = (await conn.execute(text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'notifications' AND column_name = 'consultant_id'"
                ))).first()
                if have is None:
                    # Fail fast instead of queueing behind a long transaction.
                    await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
                    await conn.execute(text(
                        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS consultant_id BIGINT "
                        "REFERENCES consultants(id) ON DELETE CASCADE"
                    ))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_notifications_consultant_id "
                        "ON notifications (consultant_id)"
                    ))
            else:
                cols = (await conn.execute(text("PRAGMA table_info(notifications)"))).fetchall()
                if not cols:
                    return False  # table not created yet; retry on the next call
                if "consultant_id" not in {c[1] for c in cols}:
                    await conn.execute(text("ALTER TABLE notifications ADD COLUMN consultant_id INTEGER"))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_notifications_consultant_id "
                        "ON notifications (consultant_id)"
                    ))
        _schema_ready = True
        return True
    except Exception as exc:
        print(f"[notification_helper] Could not ensure notifications.consultant_id: {exc!r}")
        return False


# ---------------------------------------------------------------------------
# Day windows (one match card per consultant per day)
# ---------------------------------------------------------------------------
def _tz():
    name = os.getenv("NOTIFICATION_TZ", "Asia/Kolkata")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_bounds(dt: datetime):
    """(start, end) of the local day containing `dt`, as UTC-aware datetimes."""
    local = _as_utc(dt).astimezone(_tz())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return _as_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
async def notify_by_role(db, roles: list[str], title: str, body: str):
    """
    Create a Notification row for every active user whose role is in `roles`.
    Safe by design: any failure here is caught and printed, never raised,
    so it can never break the calling code's real operation (login, sync, etc.)
    """
    try:
        result = await db.execute(
            select(User).where(User.role.in_(roles), User.is_authorized == True)
        )
        users = result.scalars().all()
        for u in users:
            db.add(Notification(user_id=u.id, title=title, body=body))
        await db.commit()
    except Exception as e:
        print(f"[notification_helper] FAILED to send notifications: {e}")


async def notify_users(db, user_ids, title: str, body: str, consultant_id=None):
    """
    Create a Notification for each user id (de-duplicated). Skips a user who
    already has an unread notification with the same title and body, so a
    retried failure never piles up identical alerts. `consultant_id` (optional)
    ties the notification to a consultant so it can be retracted exactly when
    that consultant is removed from a recruiter.
    Same safety contract as notify_by_role: never raises.
    """
    try:
        if consultant_id is not None:
            await ensure_notification_schema()
        for uid in {u for u in user_ids if u}:
            exists = await db.execute(
                select(Notification.id).where(
                    Notification.user_id == uid,
                    Notification.title == title,
                    Notification.body == body,
                    Notification.is_read == False,
                ).limit(1)
            )
            if exists.first() is None:
                extra = {"consultant_id": consultant_id} if consultant_id is not None else {}
                db.add(Notification(user_id=uid, title=title, body=body, **extra))
        await db.commit()
    except Exception as e:
        print(f"[notification_helper] FAILED to notify users: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


# --- new-match cards --------------------------------------------------------
# One notifications row per (user, consultant, local day). Later syncs on the
# same day update that row (moved to the top, unread again) instead of adding
# another one. The text and counts are NOT stored: they are recomputed from
# requirement_consultant_matches whenever the list is read. `body` only holds
# "new:<n>" = matches added since the user last read the card.
MATCH_TITLE = "New matches"


def is_match_title(title: str) -> bool:
    return (title or "").strip().lower().startswith("new match")


def _parse_new(body: str) -> int:
    m = re.match(r"^new:(\d+)$", (body or "").strip())
    return int(m.group(1)) if m else 0


async def notify_new_matches(db, consultant_ids) -> None:
    """Call after the matches were committed. `consultant_ids` = one entry per
    NEW match (an id repeats once per match it just got). Never raises."""
    try:
        counts: dict = {}
        for c in consultant_ids:
            if c:
                counts[int(c)] = counts.get(int(c), 0) + 1
        ids = sorted(counts)
        if not ids:
            return
        if not await ensure_notification_schema():
            return
        now = datetime.now(timezone.utc)
        day_start, _ = day_bounds(now)

        cons_rows = (await db.execute(
            select(Consultant.id, Consultant.user_id).where(Consultant.id.in_(ids))
        )).all()
        targets: list[tuple[int, int]] = [(uid, cid) for cid, uid in cons_rows if uid]
        rec_rows = (await db.execute(
            select(RecruiterConsultant.recruiter_id, RecruiterConsultant.consultant_id).where(
                RecruiterConsultant.consultant_id.in_(ids),
                RecruiterConsultant.is_active == True,
            )
        )).all()
        targets += [(rid, cid) for rid, cid in rec_rows]
        if not targets:
            return
        targets = sorted(set(targets))
        uids = {u for u, _ in targets}
        ok_users = {
            r[0] for r in (await db.execute(
                select(User.id).where(User.id.in_(uids), User.is_authorized == True)
            )).all()
        }
        # One query for today's existing rows of every target (not one per target).
        todays = (await db.execute(
            select(Notification).where(
                Notification.user_id.in_(uids),
                Notification.consultant_id.in_(ids),
                func.lower(Notification.title).like("new match%"),
                Notification.created_at >= day_start,
            )
        )).scalars().all()
        latest: dict = {}
        for n in sorted(todays, key=lambda n: (_as_utc(n.created_at), n.id)):
            latest[(n.user_id, n.consultant_id)] = n
        for uid, cid in targets:
            if uid not in ok_users:
                continue
            added = counts[cid]
            existing = latest.get((uid, cid))
            if existing is not None:
                existing.body = f"new:{added if existing.is_read else _parse_new(existing.body) + added}"
                existing.is_read = False
                existing.created_at = now
            else:
                db.add(Notification(
                    user_id=uid, consultant_id=cid, title=MATCH_TITLE, body=f"new:{added}",
                ))
        await db.commit()
        await _purge_old_match_rows(db)
    except Exception as e:
        print(f"[notification_helper] FAILED to notify new matches: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


# Old match rows (one per consultant per day) are dropped after 30 days; checked
# at most every 6 hours per process so the sync never pays for it per match.
_last_purge = 0.0


async def _purge_old_match_rows(db) -> None:
    global _last_purge
    if time.monotonic() - _last_purge < 6 * 3600:
        return
    _last_purge = time.monotonic()
    try:
        await db.execute(
            delete(Notification).where(
                Notification.consultant_id.is_not(None),
                func.lower(Notification.title).like("new match%"),
                Notification.created_at < datetime.now(timezone.utc) - timedelta(days=30),
            )
        )
        await db.commit()
    except Exception as e:
        print(f"[notification_helper] purge of old match rows failed: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


# --- consultant removed from a recruiter ------------------------------------
REMOVED_SUFFIX = "was removed from your consultants"
ASSIGNED_SUFFIX = "was assigned to you"


async def _clear_removal_notices(db, recruiter_id: int, consultant_id: int, name: str) -> None:
    """Delete this recruiter's 'X was removed from your consultants' notices
    for the consultant, read or unread: exact by consultant_id, plus the older
    ones saved before the id was stored (matched by the exact title)."""
    await db.execute(
        delete(Notification).where(
            Notification.user_id == recruiter_id,
            func.lower(Notification.title).like("%" + REMOVED_SUFFIX),
            (Notification.consultant_id == consultant_id)
            | (Notification.title == f"{name} {REMOVED_SUFFIX}"),
        )
    )


async def retract_for_removed_consultant(db, recruiter_id: int, consultant_id: int, notice: bool = True) -> None:
    """Called when a consultant is un-assigned from a recruiter. Deletes that
    recruiter's UNREAD alerts about this consultant (exact, by consultant_id)
    and, when `notice` is True, leaves a short notice (replacing any older
    removal notice for the same consultant). Read ones stay as history; match
    cards of a removed consultant are hidden when the list is read. Does not
    commit (runs inside the caller's transaction) and never raises."""
    try:
        if not await ensure_notification_schema():
            return
        # SAVEPOINT: if anything here fails, only this block is rolled back —
        # the caller's un-assign itself is never affected.
        async with db.begin_nested():
            name = (await db.execute(
                select(Consultant.full_name).where(Consultant.id == consultant_id)
            )).scalar_one_or_none() or "A consultant"
            await db.execute(
                delete(Notification).where(
                    Notification.user_id == recruiter_id,
                    Notification.consultant_id == consultant_id,
                    Notification.is_read == False,
                )
            )
            if notice:
                await _clear_removal_notices(db, recruiter_id, consultant_id, name)
                db.add(Notification(
                    user_id=recruiter_id, consultant_id=consultant_id,
                    title=f"{name} {REMOVED_SUFFIX}",
                    body="You will no longer get matches or alerts for this consultant.",
                ))
    except Exception as e:
        print(f"[notification_helper] FAILED to retract notifications: {e}")


# --- consultant assigned to a recruiter -------------------------------------
async def notify_consultant_assigned(db, recruiter_id: int, consultant_id: int, notice: bool = True) -> None:
    """Called when a consultant is assigned (or re-assigned) to a recruiter.
    Always clears the recruiter's stale 'was removed' notice for this
    consultant; when `notice` is True also tells the recruiter about the new
    assignment (replacing an older unread one). Pass notice=False when the
    recruiter made the assignment themselves. Does not commit (runs inside the
    caller's transaction) and never raises."""
    try:
        if not await ensure_notification_schema():
            return
        async with db.begin_nested():
            name = (await db.execute(
                select(Consultant.full_name).where(Consultant.id == consultant_id)
            )).scalar_one_or_none() or "A consultant"
            await _clear_removal_notices(db, recruiter_id, consultant_id, name)
            if notice:
                await db.execute(
                    delete(Notification).where(
                        Notification.user_id == recruiter_id,
                        Notification.consultant_id == consultant_id,
                        Notification.is_read == False,
                        func.lower(Notification.title).like("%" + ASSIGNED_SUFFIX),
                    )
                )
                db.add(Notification(
                    user_id=recruiter_id, consultant_id=consultant_id,
                    title=f"{name} {ASSIGNED_SUFFIX}",
                    body="You will now get matches and alerts for this consultant.",
                ))
    except Exception as e:
        print(f"[notification_helper] FAILED to notify assignment: {e}")


# ---------------------------------------------------------------------------
# Notification type / deep-link resolution
#
# `type` / `link` are derived from the title (always one of a small set of
# strings written by this app). Unknown titles resolve to ("general", None),
# so existing rows are untouched.
# ---------------------------------------------------------------------------
_QUEUE_LINK = {
    "ADMIN": "/admin/email-queue",
    "RECRUITER": "/recruiter/email-queue",
    "CONSULTANT": "/consultant/email-queue",
}

_NOTIFICATION_RULES = [
    # (lower-cased title prefix, type, {ROLE: link})
    ("new login accessed", "login", {}),
    ("failed login attempt", "security", {"ADMIN": "/admin/audit-logs"}),
    ("email failed", "email_failed", _QUEUE_LINK),
    ("email queue sync failed", "system", {"ADMIN": "/admin/error-queue"}),
]


def resolve_notification_meta(title: str, role: str, body: str = None):
    """Return (type, link) for a notification as seen by `role`."""
    t = (title or "").strip().lower()
    r = (role or "").upper()
    if t.endswith(REMOVED_SUFFIX):
        return "consultant_removed", ("/recruiter/consultants" if r == "RECRUITER" else None)
    if t.endswith(ASSIGNED_SUFFIX):
        return "consultant_assigned", ("/recruiter/consultants" if r == "RECRUITER" else None)
    for prefix, ntype, links in _NOTIFICATION_RULES:
        if t.startswith(prefix):
            link = links.get(r)
            if ntype == "email_failed" and link:
                # Open the Failed tab and highlight the exact queue row.
                link += "?status=FAILED"
                m = re.search(r"queue #(\d+)", body or "")
                if m:
                    link += f"&highlight={m.group(1)}"
            return ntype, link
    return "general", None


# ---------------------------------------------------------------------------
# Reader: build the match cards for one user's list
# ---------------------------------------------------------------------------
def _pct(score) -> int:
    try:
        return int(round(float(score or 0)))
    except Exception:
        return 0


async def build_match_cards(db, user, match_rows) -> list[dict]:
    """Collapse a user's per-consultant match rows into one card per local
    day, with title/body/counts recomputed from the matches table. Rows whose
    matches are gone (consultant removed from the recruiter, matches no longer
    active) produce no card."""
    if not match_rows:
        return []
    role = (user.role or "").upper()

    roster = None
    if role == "RECRUITER":
        roster = {
            r[0] for r in (await db.execute(
                select(RecruiterConsultant.consultant_id).where(
                    RecruiterConsultant.recruiter_id == user.id,
                    RecruiterConsultant.is_active == True,
                )
            )).all()
        }

    days: dict = {}
    for n in match_rows:
        if n.consultant_id is None:
            continue
        if roster is not None and n.consultant_id not in roster:
            continue
        start, end = day_bounds(n.created_at)
        days.setdefault(start, {"end": end, "rows": []})["rows"].append(n)

    cards = []
    for start, grp in days.items():
        rows = grp["rows"]
        cids = sorted({n.consultant_id for n in rows})
        q = (
            select(
                RequirementConsultantMatch.consultant_id,
                RequirementConsultantMatch.match_score,
                Requirement.role,
                Requirement.client,
                Consultant.full_name,
            )
            .join(Requirement, Requirement.id == RequirementConsultantMatch.requirement_id)
            .join(Consultant, Consultant.id == RequirementConsultantMatch.consultant_id)
            .where(
                RequirementConsultantMatch.consultant_id.in_(cids),
                RequirementConsultantMatch.status.in_(("MATCHING", "APPLIED")),
                RequirementConsultantMatch.created_at >= start,
                RequirementConsultantMatch.created_at < grp["end"],
            )
            .order_by(RequirementConsultantMatch.match_score.desc())
        )
        found = (await db.execute(q)).all()
        if not found:
            continue
        per: dict = {}
        for cid, score, rrole, client, cname in found:
            e = per.setdefault(cid, {"id": cid, "name": cname or "Consultant", "count": 0})
            e["count"] += 1
        people = sorted(per.values(), key=lambda p: (-p["count"], p["name"]))
        total = len(found)
        top = found[0]
        top_txt = f"{top[2] or 'Requirement'}" + (f" · {top[3]}" if top[3] and top[3] != "UNKNOWN" else "") + f" · {_pct(top[1])}%"

        if role == "RECRUITER":
            if len(people) == 1:
                nm = people[0]["name"]
                title = f"New match for {nm}" if total == 1 else f"{total} new matches for {nm}"
            else:
                title = f"{total} new matches across {len(people)} consultants"
            if total == 1:
                body = f"{top_txt} match"
            elif len(people) == 1:
                body = f"Best: {top_txt}"
            else:
                body = f"Top: {top_txt} for {(top[4] or 'Consultant').split(' ')[0]}"
            link = f"/recruiter/requirements?since={iso_z(start)}"
            if len(people) == 1:
                link += f"&consultant_id={people[0]['id']}"
        else:
            title = "New match" if total == 1 else f"{total} new matches"
            body = f"{top_txt} match" if total == 1 else f"Best: {top_txt}"
            link = f"/consultant/requirements?since={iso_z(start)}"

        unread = [n for n in rows if not n.is_read]
        newest = max(rows, key=lambda n: (_as_utc(n.created_at), n.id))
        cards.append({
            "id": newest.id,
            "user_id": newest.user_id,
            "title": title,
            "body": body,
            "is_read": not unread,
            "created_at": newest.created_at,
            "type": "matches",
            "link": link,
            "meta": {
                "kind": "matches",
                "total": total,
                "new": min(total, sum(_parse_new(n.body) for n in unread)),
                "since": iso_z(start),
                "consultants": people,
            },
        })
    return cards


def _is_match_row():
    return and_(Notification.consultant_id.is_not(None), func.lower(Notification.title).like("new match%"))


# Match rows are fetched separately (and only the last 14 days) so a recruiter's
# one-row-per-consultant-per-day never crowds logins/alerts out of the list.
_MATCH_WINDOW_DAYS = 14
_MATCH_ROW_CAP = 500


async def list_for_user(db, user, limit: int = 50) -> list[dict]:
    """The user's notification list as the API returns it: ordinary rows as
    they are, new-match rows collapsed into one recomputed card per day."""
    plain = (await db.execute(
        select(Notification)
        .where(Notification.user_id == user.id, ~_is_match_row())
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
    )).scalars().all()
    match_rows = (await db.execute(
        select(Notification)
        .where(
            Notification.user_id == user.id,
            _is_match_row(),
            Notification.created_at >= datetime.now(timezone.utc) - timedelta(days=_MATCH_WINDOW_DAYS),
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(_MATCH_ROW_CAP)
    )).scalars().all()
    out = []
    for n in plain:
        ntype, link = resolve_notification_meta(n.title, user.role, n.body)
        out.append({
            "id": n.id, "user_id": n.user_id, "title": n.title, "body": n.body,
            "is_read": n.is_read, "created_at": n.created_at,
            "type": ntype, "link": link, "meta": None,
        })
    out += await build_match_cards(db, user, match_rows)
    out.sort(key=lambda d: (_as_utc(d["created_at"]), d["id"]), reverse=True)
    return out[:limit]


async def count_unread_for_user(db, user):
    """(unread count, fresh 'consultant removed' notice) for the bell badge.
    Cheap: one COUNT for ordinary rows, and only the user's UNREAD match rows
    (usually one day's worth) go through the card builder so the badge always
    equals what the list shows."""
    plain = (await db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user.id, Notification.is_read == False, ~_is_match_row()
        )
    )).scalar_one()
    match_rows = (await db.execute(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.is_read == False,
            _is_match_row(),
            Notification.created_at >= datetime.now(timezone.utc) - timedelta(days=_MATCH_WINDOW_DAYS),
        ).limit(_MATCH_ROW_CAP)
    )).scalars().all()
    cards = await build_match_cards(db, user, match_rows) if match_rows else []
    notice = None
    row = (await db.execute(
        select(Notification.id, Notification.title, Notification.body).where(
            Notification.user_id == user.id,
            Notification.is_read == False,
            func.lower(Notification.title).like("%was removed from your consultants"),
            Notification.created_at >= datetime.now(timezone.utc) - timedelta(minutes=15),
        ).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(1)
    )).first()
    if row is not None:
        notice = {"id": row[0], "title": row[1], "body": row[2]}
    return int(plain or 0) + len(cards), notice


async def mark_read(db, user, notification_id: int) -> bool:
    """Mark one notification read. A match row stands for a whole day's card
    (one row per consultant), so every match row of that day is marked."""
    n = (await db.execute(
        select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id)
    )).scalars().first()
    if n is None:
        return False
    if n.consultant_id is not None and is_match_title(n.title):
        start, end = day_bounds(n.created_at)
        await db.execute(
            update(Notification).where(
                Notification.user_id == user.id,
                Notification.consultant_id.is_not(None),
                func.lower(Notification.title).like("new match%"),
                Notification.created_at >= start,
                Notification.created_at < end,
            ).values(is_read=True)
        )
    else:
        n.is_read = True
    await db.commit()
    return True