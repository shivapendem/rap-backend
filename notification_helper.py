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
            select(User).where(User.role.in_(roles), User.is_active == True)
        )
        users = result.scalars().all()
        for u in users:
            db.add(Notification(user_id=u.id, title=title, body=body))
        await db.commit()
    except Exception as e:
        print(f"[notification_helper] FAILED to send notifications: {e}")