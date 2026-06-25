+
 Z# phase_users_repository.py
# ---------------------------------------------------------------------------
# Admin User Management — Repository layer
# Queries your REAL models (User, Consultant, RecruiterConsultant) from
# models.py — not the standalone user_mgmt_backend's placeholder schema.
# ---------------------------------------------------------------------------

from typing import Optional, List, Tuple

from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from models import User, Consultant, RecruiterConsultant


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
    ) -> Tuple[List[User], int]:
        filters = []
        if role:
            filters.append(User.role == role)
        if status:
            filters.append(User.is_active == (status == "Active"))
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
        sort_col = User.is_active if sort_by == "status" else getattr(User, sort_col_name)

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
        result = await db.execute(select(Consultant).order_by(Consultant.full_name.asc()).limit(limit))
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