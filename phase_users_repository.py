# phase_users_repository.py
# ---------------------------------------------------------------------------
# Admin User Management — Repository layer
# Queries your REAL models (User, Consultant, RecruiterConsultant) from
# models.py — not the standalone user_mgmt_backend's placeholder schema.
# ---------------------------------------------------------------------------

from typing import Optional, List, Tuple

from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from models import User, Consultant, RecruiterConsultant, ConsultantEmailToken


GMAIL_STATUSES = {"CONNECTED", "PAUSED", "RECONNECT", "NOT_CONNECTED"}


def gmail_status_filter(gmail_status: Optional[List[str]]):
    """SQL filter for the User Management "Gmail" dropdown (multi-select, OR).
    Same rules as phase_users_service._gmail_status_of:
      CONNECTED     token active + send permission + refresh token
      PAUSED        token kept but is_active = FALSE (logged out / inactive)
      RECONNECT     token active but send permission or refresh token missing
      NOT_CONNECTED no token row (Google rejected refresh -> row deleted)
    Only consultants have Gmail, so any Gmail filter limits to CONSULTANT."""
    wanted = [s for s in (gmail_status or []) if s in GMAIL_STATUSES]
    if not wanted:
        return None
    T = ConsultantEmailToken
    def has_token(*conds):
        return (
            select(T.id)
            .join(Consultant, Consultant.id == T.consultant_id)
            .where(Consultant.user_id == User.id, *conds)
            .exists()
        )
    parts = []
    if "CONNECTED" in wanted:
        parts.append(has_token(T.is_active == True, T.send_permission_granted == True, T.refresh_token_encrypted.isnot(None)))
    if "PAUSED" in wanted:
        parts.append(has_token(T.is_active == False))
    if "RECONNECT" in wanted:
        parts.append(has_token(T.is_active == True, or_(T.send_permission_granted == False, T.refresh_token_encrypted.is_(None))))
    if "NOT_CONNECTED" in wanted:
        parts.append(~has_token())
    return and_(User.role == "CONSULTANT", or_(*parts))


class UserRepository:

    @staticmethod
    async def get_by_id(db: AsyncSession, user_id: int) -> Optional[User]:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalars().first()

    @staticmethod
    async def get_by_email(db: AsyncSession, email: str) -> Optional[User]:
        result = await db.execute(select(User).where(User.email == email.lower().strip()))
        return result.scalars().first()

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        *,
        page: int,
        page_size: int,
        sort_by: str,
        sort_dir: str,
        search: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        gmail_status: Optional[List[str]] = None,
    ) -> Tuple[List[User], int]:
        filters = []
        gmail_filter = gmail_status_filter(gmail_status)
        if gmail_filter is not None:
            filters.append(gmail_filter)
        if role:
            filters.append(User.role == role)
        if status:
            # BUG FIX: this referenced User.is_active, a column that does
            # not exist on the User model at all — is_active only exists
            # on RecruiterConsultant, a completely different table. Every
            # request with a status filter applied raised a SQLAlchemy
            # AttributeError, which the users list endpoint had no special
            # handling for, so the frontend just showed "Failed to load
            # users" with no indication why. The row's actual displayed
            # status ("Authorized"/"Unauthorized", built in
            # phase_users_service.py from is_authorized) already comes
            # from User.is_authorized — filtering needs to match that same
            # field, not a column from an unrelated table.
            filters.append(User.is_authorized == (status == "Active"))
        if search:
            kw = f"%{search.lower()}%"
            filters.append(
                or_(func.lower(User.full_name).like(kw), func.lower(User.email).like(kw))
            )

        base_filter = and_(*filters) if filters else True

        count_q = select(func.count()).select_from(User).where(base_filter)
        total = (await db.execute(count_q)).scalar_one()

        allowed_sort = {"full_name", "email", "role", "created_at"}
        sort_col_name = sort_by if sort_by in allowed_sort else "full_name"
        # Same bug, same fix: sorting by "status" referenced the
        # nonexistent User.is_active instead of User.is_authorized.
        sort_col = User.is_authorized if sort_by == "status" else getattr(User, sort_col_name)

        query = select(User).where(base_filter)
        query = query.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        rows = (await db.execute(query)).scalars().all()
        return list(rows), total

    @staticmethod
    async def create(db: AsyncSession, user: User) -> User:
        db.add(user)
        await db.flush()
        await db.refresh(user)
        return user

    @staticmethod
    async def update(db: AsyncSession, user: User) -> User:
        await db.flush()
        await db.refresh(user)
        return user

    @staticmethod
    async def soft_delete(db: AsyncSession, user: User) -> User:
        user.is_active = False
        await db.flush()
        await db.refresh(user)
        return user


class ConsultantRepository:

    @staticmethod
    async def get_by_id(db: AsyncSession, consultant_id: int) -> Optional[Consultant]:
        result = await db.execute(select(Consultant).where(Consultant.id == consultant_id))
        return result.scalars().first()

    @staticmethod
    async def get_by_user_id(db: AsyncSession, user_id: int) -> Optional[Consultant]:
        result = await db.execute(select(Consultant).where(Consultant.user_id == user_id))
        return result.scalars().first()

    @staticmethod
    async def list_all(db: AsyncSession, limit: int = 200) -> List[Consultant]:
        from sqlalchemy.orm import defer
        result = await db.execute(
            select(Consultant)
            .options(
                defer(Consultant.base_resume_text),
                defer(Consultant.resume_rich_text),
                defer(Consultant.base_resume_content),
                defer(Consultant.base_resume_file_path),
            )
            .order_by(Consultant.full_name.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def update(db: AsyncSession, consultant: Consultant) -> Consultant:
        await db.flush()
        await db.refresh(consultant)
        return consultant

    @staticmethod
    async def get_assigned_recruiters(db: AsyncSession, consultant_id: int) -> List[User]:
        result = await db.execute(
            select(User)
            .join(RecruiterConsultant, RecruiterConsultant.recruiter_id == User.id)
            .where(
                RecruiterConsultant.consultant_id == consultant_id,
                RecruiterConsultant.is_active == True,
            )
        )
        return list(result.scalars().all())


class RecruiterConsultantRepository:

    @staticmethod
    async def get_assigned_consultant_ids(db: AsyncSession, recruiter_id: int) -> List[int]:
        result = await db.execute(
            select(RecruiterConsultant.consultant_id).where(
                RecruiterConsultant.recruiter_id == recruiter_id,
                RecruiterConsultant.is_active == True,
            )
        )
        return [r[0] for r in result.all()]

    @staticmethod
    async def assign(db: AsyncSession, recruiter_id: int, consultant_id: int) -> RecruiterConsultant:
        # Reactivate if a soft-deleted mapping already exists, else create new
        existing = await db.execute(
            select(RecruiterConsultant).where(
                RecruiterConsultant.recruiter_id == recruiter_id,
                RecruiterConsultant.consultant_id == consultant_id,
            )
        )
        mapping = existing.scalars().first()
        if mapping:
            mapping.is_active = True
        else:
            mapping = RecruiterConsultant(
                recruiter_id=recruiter_id, consultant_id=consultant_id, is_active=True,
            )
            db.add(mapping)
        await db.flush()
        return mapping

    @staticmethod
    async def unassign(db: AsyncSession, recruiter_id: int, consultant_id: int) -> None:
        result = await db.execute(
            select(RecruiterConsultant).where(
                RecruiterConsultant.recruiter_id == recruiter_id,
                RecruiterConsultant.consultant_id == consultant_id,
            )
        )
        mapping = result.scalars().first()
        if mapping:
            mapping.is_active = False
            await db.flush()

    @staticmethod
    async def exists(db: AsyncSession, recruiter_id: int, consultant_id: int) -> bool:
        result = await db.execute(
            select(RecruiterConsultant).where(
                RecruiterConsultant.recruiter_id == recruiter_id,
                RecruiterConsultant.consultant_id == consultant_id,
                RecruiterConsultant.is_active == True,
            )
        )
        return result.scalars().first() is not None

    @staticmethod
    async def replace_for_recruiter(db: AsyncSession, recruiter_id: int, consultant_ids: List[int]) -> None:
        """Used by PUT /admin/recruiters/{id}/consultants — sets exact assignment list."""
        from models import Consultant
        real_consultant_ids = []
        for cid in consultant_ids:
            result = await db.execute(select(Consultant).where(Consultant.id == cid))
            c = result.scalars().first()
            if c:
                real_consultant_ids.append(c.id)
            else:
                result = await db.execute(select(Consultant).where(Consultant.user_id == cid))
                c = result.scalars().first()
                if c:
                    real_consultant_ids.append(c.id)

        existing = await RecruiterConsultantRepository.get_assigned_consultant_ids(db, recruiter_id)
        existing_set = set(existing)
        target_set = set(real_consultant_ids)

        to_remove = existing_set - target_set
        to_add = target_set - existing_set

        for cid in to_remove:
            await RecruiterConsultantRepository.unassign(db, recruiter_id, cid)
        for cid in to_add:
            await RecruiterConsultantRepository.assign(db, recruiter_id, cid)