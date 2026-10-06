# notification_helper.py
from models import User, Notification
from sqlalchemy.future import select


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


# ---------------------------------------------------------------------------
# Notification type / deep-link resolution
#
# The notifications table has no `type` / `link` columns (and adding them
# would need a DB migration). Instead the API derives both from the title,
# which is always one of a small set of fixed strings written by this app.
# Unknown titles resolve to ("general", None), so existing rows are untouched.
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


def resolve_notification_meta(title: str, role: str):
    """Return (type, link) for a notification title as seen by `role`."""
    t = (title or "").strip().lower()
    for prefix, ntype, links in _NOTIFICATION_RULES:
        if t.startswith(prefix):
            return ntype, links.get((role or "").upper())
    return "general", None


async def notify_users(db, user_ids, title: str, body: str):
    """
    Create a Notification for each user id (de-duplicated). Skips a user who
    already has an unread notification with the same title and body, so a
    retried failure never piles up identical alerts.
    Same safety contract as notify_by_role: never raises.
    """
    try:
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
                db.add(Notification(user_id=uid, title=title, body=body))
        await db.commit()
    except Exception as e:
        print(f"[notification_helper] FAILED to notify users: {e}")
        try:
            await db.rollback()
        except Exception:
            pass