# phase_users_service.py
# ---------------------------------------------------------------------------
# Admin User Management — Service layer
# Business logic + audit logging via your existing Phase 8 audit_logs table.
# ---------------------------------------------------------------------------

import math
from typing import Optional, List

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models import User, Consultant, Application, Resume
from phase_users_repository import (
    UserRepository, ConsultantRepository, RecruiterConsultantRepository,
)
from auth import get_password_hash
from phase8_audit_service import log_action
from phase_users_schema import (
    UserAdminRowDTO, PaginatedUsersDTO,
    CreateUserRequestDTO, EditUserRequestDTO,
    ConsultantAdminRowDTO, RecruiterRefDTO,
)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

def _user_to_dto(u: User) -> UserAdminRowDTO:
    return UserAdminRowDTO(
        id=str(u.id),
        full_name=u.full_name,
        email=u.email,
        role=u.role,
        status="Active" if u.is_active else "Inactive",
        is_active=u.is_active,
        created_at=u.created_at.isoformat() if u.created_at else "",
        updated_at=u.updated_at.isoformat() if u.updated_at else "",
        skills=u.skills if isinstance(u.skills, list) else None,
        needsto_fetch_mail=bool(u.needsto_fetch_mail),
        experience_years=float(u.experience_years) if u.experience_years is not None else None,
        resume_info=u.resume_info,
    )


async def _consultant_to_dto(db: AsyncSession, c: Consultant) -> ConsultantAdminRowDTO:
    recruiters = await ConsultantRepository.get_assigned_recruiters(db, c.id)

    # Profile completeness — mirrors ProfileCompletenessBar.tsx so the admin
    # screen and the consultant's own profile show the same number.
    completeness = 0
    if (c.primary_skills or "").strip() or (c.secondary_skills or "").strip():
        completeness += 30  # Skills
    if c.base_resume_file_path or c.base_resume_text:
        completeness += 30  # Resume
    if c.preferred_employment_types:
        completeness += 20  # Employment type
    if (c.work_authorization or "").strip():
        completeness += 10  # Work auth
    if len((c.current_location or "").strip()) >= 2:
        completeness += 10  # Location

    # BUG FIX: last_login_at / total_applications_sent / total_resumes_generated
    # were previously not returned by this endpoint at all — the frontend
    # hardcoded null/0 for all three with a comment noting the backend gap.
    # Computed here from real data: User.last_login_at (now tracked at
    # login — see main.py), Application rows with status="SENT", and
    # Resume rows for this consultant's linked user account.
    last_login_at = None
    total_applications_sent = 0
    total_resumes_generated = 0

    if c.user_id:
        user_result = await db.execute(select(User.last_login_at).where(User.id == c.user_id))
        last_login_at = user_result.scalar_one_or_none()

        resumes_result = await db.execute(
            select(func.count()).select_from(Resume).where(Resume.user_id == c.user_id)  # pylint: disable=not-callable  # pyright: ignore[reportOptionalCall, reportCallIssue]
        )
        total_resumes_generated = resumes_result.scalar_one() or 0

    apps_result = await db.execute(
        select(func.count()).select_from(Application).where(  # pylint: disable=not-callable  # pyright: ignore[reportOptionalCall, reportCallIssue]
            Application.consultant_id == c.id, Application.status == "SENT"
        )
    )
    total_applications_sent = apps_result.scalar_one() or 0

    return ConsultantAdminRowDTO(
        id=str(c.id),
        user_id=str(c.user_id) if c.user_id else "",
        name=c.full_name or "",
        email=c.email or "",
        status=c.status,
        primary_skills=c.primary_skills,
        work_authorization=c.work_authorization,
        preferred_employment_types=c.preferred_employment_types or [],
        gmail_connected=c.gmail_connected,
        assigned_recruiters=[
            RecruiterRefDTO(id=str(r.id), name=r.full_name, email=r.email) for r in recruiters
        ],
        created_at=c.created_at.isoformat() if c.created_at else "",
        phone=c.phone,
        sales_recruiter_user_id=str(c.sales_recruiter_user_id) if c.sales_recruiter_user_id else None,
        current_location=c.current_location,
        preferred_locations=c.preferred_locations,
        availability_status=c.availability_status,
        total_experience_years=float(c.total_experience_years) if c.total_experience_years is not None else None,
        secondary_skills=c.secondary_skills,
        preferred_roles=c.preferred_roles,
        ats_score=float(c.ats_score) if c.ats_score is not None else None,
        updated_at=c.updated_at.isoformat() if c.updated_at else "",
        has_resume=bool(c.base_resume_file_path or c.base_resume_text),
        last_login_at=last_login_at.isoformat() if last_login_at else None,
        total_applications_sent=total_applications_sent,
        total_resumes_generated=total_resumes_generated,
        completeness_pct=completeness,
    )


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

class UserService:

    @staticmethod
    async def list_users(
        db: AsyncSession,
        *, page: int, page_size: int, sort_by: str, sort_dir: str,
        search: Optional[str], role: Optional[str], status: Optional[str],
    ) -> PaginatedUsersDTO:
        rows, total = await UserRepository.list_paginated(
            db, page=page, page_size=page_size, sort_by=sort_by, sort_dir=sort_dir,
            search=search, role=role, status=status,
        )
        return PaginatedUsersDTO(
            data=[_user_to_dto(u) for u in rows],
            total=total, page=page, page_size=page_size,
            total_pages=math.ceil(total / page_size) if total else 1,
        )

    @staticmethod
    async def get_user(db: AsyncSession, user_id: int) -> UserAdminRowDTO:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return _user_to_dto(user)

    @staticmethod
    async def create_user(
        db: AsyncSession, req: CreateUserRequestDTO, *, admin_id: str,
    ) -> UserAdminRowDTO:
        existing = await UserRepository.get_by_email(db, req.email)
        if existing:
            raise HTTPException(status_code=409, detail="A user with this email already exists.")

        user = User(
            full_name=req.full_name.strip(),
            email=req.email,
            role=req.role,
            password_hash=get_password_hash(req.password),
            is_active=True,
            resume_info=req.resume_info,
        )
        user = await UserRepository.create(db, user)

        # AUTO CREATE consultant profile when role is CONSULTANT
        if req.role == "CONSULTANT":
            from models import Consultant
            consultant = Consultant(
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                status="ACTIVE",
                gmail_connected=False,
                ats_score=0,
                preferred_employment_types=[],
            )
            db.add(consultant)
            await db.flush()

        await log_action(
            db, "USER_CREATED",
            actor_user_id=admin_id, actor_name=admin_id, actor_role="ADMIN",
            entity_type="User", entity_id=str(user.id),
            metadata={"email": user.email, "role": user.role},
        )
        await db.commit()
        return _user_to_dto(user)

    @staticmethod
    async def update_user(
        db: AsyncSession, user_id: int, req: EditUserRequestDTO, *, admin_id: str,
    ) -> UserAdminRowDTO:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if req.email.lower() != user.email.lower():
            existing = await UserRepository.get_by_email(db, req.email)
            if existing and existing.id != user.id:
                raise HTTPException(status_code=409, detail="A user with this email already exists.")

        before = {"full_name": user.full_name, "email": user.email, "role": user.role, "is_active": user.is_active}

        user.full_name = req.full_name.strip()
        user.email = req.email
        user.role = req.role
        user.is_active = req.is_active
        if req.skills is not None:
            user.skills = req.skills
        if req.needsto_fetch_mail is not None:
            user.needsto_fetch_mail = req.needsto_fetch_mail
        if req.experience_years is not None:
            user.experience_years = req.experience_years
        if req.resume_info is not None:
            user.resume_info = req.resume_info
        user = await UserRepository.update(db, user)

        # Apply consultant-only fields if this user has a linked consultant profile
        if req.role == "CONSULTANT":
            consultant = await ConsultantRepository.get_by_user_id(db, user.id)
            if consultant:
                if req.work_authorization is not None:
                    consultant.work_authorization = req.work_authorization
                if req.preferred_employment_types is not None:
                    consultant.preferred_employment_types = req.preferred_employment_types
                if req.primary_skills is not None:
                    consultant.primary_skills = req.primary_skills
                consultant.full_name = user.full_name
                consultant.email = user.email
                await ConsultantRepository.update(db, consultant)

                if req.recruiter_id:
                    rid = int(req.recruiter_id)
                    already = await RecruiterConsultantRepository.exists(db, rid, consultant.id)
                    if not already:
                        await RecruiterConsultantRepository.assign(db, rid, consultant.id)

        await log_action(
            db, "USER_UPDATED",
            actor_user_id=admin_id, actor_name=admin_id, actor_role="ADMIN",
            entity_type="User", entity_id=str(user.id),
            metadata={"before": before, "after": {
                "full_name": user.full_name, "email": user.email,
                "role": user.role, "is_active": user.is_active,
            }},
        )
        await db.commit()
        return _user_to_dto(user)

    @staticmethod
    async def delete_user(db: AsyncSession, user_id: int, *, admin_id: str) -> None:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        await UserRepository.soft_delete(db, user)

        await log_action(
            db, "USER_DELETED",
            actor_user_id=admin_id, actor_name=admin_id, actor_role="ADMIN",
            entity_type="User", entity_id=str(user.id),
            metadata={"email": user.email, "role": user.role},
        )
        await db.commit()

    @staticmethod
    async def update_status(
        db: AsyncSession, user_id: int, status_value: str, *, admin_id: str,
    ) -> tuple[str, str]:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        old_status = "Active" if user.is_active else "Inactive"
        user.is_active = (status_value == "ACTIVE")
        user = await UserRepository.update(db, user)
        new_status = "Active" if user.is_active else "Inactive"

        await log_action(
            db, "USER_STATUS_CHANGED",
            actor_user_id=admin_id, actor_name=admin_id, actor_role="ADMIN",
            entity_type="User", entity_id=str(user.id),
            metadata={"old_status": old_status, "new_status": status_value},
        )
        await db.commit()
        return str(user.id), new_status

    @staticmethod
    async def deactivate(db: AsyncSession, user_id: int, *, admin_id: str) -> tuple[str, str]:
        return await UserService.update_status(db, user_id, "INACTIVE", admin_id=admin_id)

    @staticmethod
    async def activate(db: AsyncSession, user_id: int, *, admin_id: str) -> tuple[str, str]:
        return await UserService.update_status(db, user_id, "ACTIVE", admin_id=admin_id)


# ---------------------------------------------------------------------------
# Consultant assignment
# ---------------------------------------------------------------------------

class ConsultantAssignmentService:

    @staticmethod
    async def list_consultants(db: AsyncSession) -> List[ConsultantAdminRowDTO]:
        consultants = await ConsultantRepository.list_all(db)
        return [await _consultant_to_dto(db, c) for c in consultants]

    @staticmethod
    async def get_consultant(db: AsyncSession, consultant_id: int) -> ConsultantAdminRowDTO:
        consultant = await ConsultantRepository.get_by_id(db, consultant_id)
        if not consultant:
            raise HTTPException(status_code=404, detail="Consultant not found")
        return await _consultant_to_dto(db, consultant)

    @staticmethod
    async def update_consultant(
        db: AsyncSession, consultant_id: int,
        primary_skills: Optional[str], availability_status: Optional[str],
        status: Optional[str], *, admin_id: str,
        work_authorization: Optional[str] = None,
        preferred_employment_types: Optional[list] = None,
        phone: Optional[str] = None,
        current_location: Optional[str] = None,
        preferred_locations: Optional[str] = None,
        total_experience_years: Optional[float] = None,
        secondary_skills: Optional[str] = None,
        preferred_roles: Optional[str] = None,
    ) -> ConsultantAdminRowDTO:
        consultant = await ConsultantRepository.get_by_id(db, consultant_id)
        if not consultant:
            raise HTTPException(status_code=404, detail="Consultant not found")

        if primary_skills is not None:
            consultant.primary_skills = primary_skills
        if availability_status is not None:
            consultant.availability_status = availability_status
        if status is not None:
            consultant.status = status
        if work_authorization is not None:
            consultant.work_authorization = work_authorization
        if preferred_employment_types is not None:
            consultant.preferred_employment_types = preferred_employment_types
        if phone is not None:
            consultant.phone = phone
        if current_location is not None:
            consultant.current_location = current_location
        if preferred_locations is not None:
            consultant.preferred_locations = preferred_locations
        if total_experience_years is not None:
            consultant.total_experience_years = total_experience_years
        if secondary_skills is not None:
            consultant.secondary_skills = secondary_skills
        if preferred_roles is not None:
            consultant.preferred_roles = preferred_roles

        consultant = await ConsultantRepository.update(db, consultant)

        await log_action(
            db, "USER_UPDATED",
            actor_user_id=admin_id, actor_name=admin_id, actor_role="ADMIN",
            entity_type="Consultant", entity_id=str(consultant.id),
            metadata={"type": "consultant_profile_update"},
        )
        await db.commit()
        return await _consultant_to_dto(db, consultant)

    @staticmethod
    async def assign_consultant(
        db: AsyncSession, recruiter_user_id: int, consultant_id: int, *, admin_id: str,
    ) -> None:
        recruiter = await UserRepository.get_by_id(db, recruiter_user_id)
        if not recruiter or recruiter.role != "RECRUITER":
            raise HTTPException(status_code=404, detail="Recruiter not found")

        consultant = await ConsultantRepository.get_by_id(db, consultant_id)
        if not consultant:
            raise HTTPException(status_code=404, detail="Consultant not found")

        already = await RecruiterConsultantRepository.exists(db, recruiter_user_id, consultant_id)
        if already:
            raise HTTPException(status_code=409, detail="Consultant already assigned to this recruiter.")

        await RecruiterConsultantRepository.assign(db, recruiter_user_id, consultant_id)

        await log_action(
            db, "CONSULTANT_ASSIGNED",
            actor_user_id=admin_id, actor_name=admin_id, actor_role="ADMIN",
            entity_type="Consultant", entity_id=str(consultant_id),
            metadata={"recruiter_id": str(recruiter_user_id)},
        )
        await db.commit()

    @staticmethod
    async def unassign_consultant(
        db: AsyncSession, recruiter_user_id: int, consultant_id: int, *, admin_id: str,
    ) -> None:
        await RecruiterConsultantRepository.unassign(db, recruiter_user_id, consultant_id)
        await log_action(
            db, "CONSULTANT_ASSIGNED",
            actor_user_id=admin_id, actor_name=admin_id, actor_role="ADMIN",
            entity_type="Consultant", entity_id=str(consultant_id),
            metadata={"recruiter_id": str(recruiter_user_id), "action": "unassigned"},
        )
        await db.commit()

    @staticmethod
    async def replace_assignments(
        db: AsyncSession, recruiter_user_id: int, consultant_ids: List[int], *, admin_id: str,
    ) -> None:
        recruiter = await UserRepository.get_by_id(db, recruiter_user_id)
        if not recruiter or recruiter.role != "RECRUITER":
            raise HTTPException(status_code=404, detail="Recruiter not found")

        await RecruiterConsultantRepository.replace_for_recruiter(db, recruiter_user_id, consultant_ids)

        await log_action(
            db, "CONSULTANT_ASSIGNED",
            actor_user_id=admin_id, actor_name=admin_id, actor_role="ADMIN",
            entity_type="User", entity_id=str(recruiter_user_id),
            metadata={"consultant_ids": [str(c) for c in consultant_ids]},
        )
        await db.commit()
