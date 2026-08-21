# phase_users_service.py
# ---------------------------------------------------------------------------
# Admin User Management — Service layer
# Business logic + audit logging via your existing Phase 8 audit_logs table.
# ---------------------------------------------------------------------------

import math
import re
from typing import Optional, List, Any

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models import User, Consultant, Application, Resume, ConsultantExperience, RecruiterConsultant
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
        status="Authorized" if u.is_authorized else "Unauthorized",
        is_authorized=u.is_authorized,
        created_at=u.created_at.isoformat() if u.created_at else "",
        updated_at=u.updated_at.isoformat() if u.updated_at else "",
        skills=u.skills if isinstance(u.skills, list) else None,
        needsto_fetch_mail=bool(u.needsto_fetch_mail),
        experience_years=float(u.experience_years) if u.experience_years is not None else None,
        resume_info=u.resume_info,
        mobile_number=u.mobile_number,
        extension=u.extension,
        linkedin_url=u.linkedin_url,
        designation=u.designation,
    )


async def _consultant_to_dto(db: AsyncSession, c: Consultant) -> ConsultantAdminRowDTO:
    recruiters = await ConsultantRepository.get_assigned_recruiters(db, c.id)

    exp_count_result = await db.execute(
        select(func.count()).where(ConsultantExperience.consultant_id == c.id)
    )
    experience_count = exp_count_result.scalar_one()

    completeness = 0
    if (c.primary_skills or "").strip() or (c.secondary_skills or "").strip():
        completeness += 30  # Skills
    if experience_count > 0:
        completeness += 25  # Experience
    if c.preferred_employment_types:
        completeness += 20  # Employment type
    if (c.work_authorization or "").strip():
        completeness += 15  # Work auth
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
    # Feeds the admin "Resume Info (JSON)" editor — same underlying field
    # AddEditUserDrawer.tsx already edits from the Users page, now also
    # editable directly from the consultant-specific admin screens.
    resume_info = None

    if c.user_id:
        user_result = await db.execute(
            select(User.last_login_at, User.resume_info).where(User.id == c.user_id)
        )
        user_row = user_result.first()
        if user_row:
            last_login_at, resume_info = user_row

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

    # BUG FIX: c.ats_score is a Consultant column that is never written
    # anywhere in the codebase, so it was permanently stuck at its default
    # of 0 — every "ATS Score" shown from this endpoint (admin consultants
    # table / user detail page) was 0. Same fix as phase3.py's
    # _consultant_to_profile_response: use the most recent generated
    # resume's real ATS score instead.
    latest_ats_score = None
    if c.user_id:
        latest_resume_result = await db.execute(
            select(Resume.ats_score)
            .where(Resume.user_id == c.user_id, Resume.ats_score.isnot(None))
            .order_by(Resume.created_at.desc())
            .limit(1)
        )
        latest_ats_score = latest_resume_result.scalar_one_or_none()

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
        ats_score=float(latest_ats_score) if latest_ats_score is not None else None,
        # BUG FIX ("admin shows no education / different LinkedIn than the
        # consultant's own profile"): this used to read ONLY the Consultant
        # columns, with no fallback — but consultants who saved their
        # LinkedIn/education before those columns existed (or whose data
        # otherwise never got backfilled onto them) still have their real
        # data sitting in User.resume_info, which is exactly what the
        # consultant-facing profile endpoint (phase3.py
        # _consultant_to_profile_response) already falls back to. Admin
        # was missing that same fallback, so the two screens disagreed for
        # any consultant whose data predates the column. Both screens now
        # use identical fallback logic; both write paths still write the
        # real column going forward, so this only matters for old rows.
        linkedin_url=c.linkedin_url if c.linkedin_url is not None else (resume_info or {}).get("linkedin"),
        education=c.education or (resume_info or {}).get("education") or [],
        resume_info=resume_info,
        resume_rich_text=c.resume_rich_text,
        updated_at=c.updated_at.isoformat() if c.updated_at else "",
        has_resume=bool(c.base_resume_file_path or c.base_resume_text),
        last_login_at=last_login_at.isoformat() if last_login_at else None,
        total_applications_sent=total_applications_sent,
        total_resumes_generated=total_resumes_generated,
        completeness_pct=completeness,
    )


async def _consultants_to_dtos_bulk(db: AsyncSession, consultants: List[Consultant]) -> List[ConsultantAdminRowDTO]:
    """
    Batched version of _consultant_to_dto() for list endpoints.

    BUG FIX ("consultant profile not loading" / GET /api/v1/admin/consultants
    request stuck with zero response headers — never a 4xx/5xx, the browser
    just never got a response at all): _consultant_to_dto() does up to 6
    sequential DB queries per consultant (assigned recruiters, experience
    count, linked-user lookup, resume count, applications-sent count,
    latest ATS score). That's fine for looking up ONE consultant, but the
    list endpoint ran it in a plain per-item loop — with the repository's
    200-consultant cap, that's up to 1,200 sequential round trips for a
    single page load. As the consultant roster grew over the course of
    normal use, this endpoint got slower and slower until it finally
    exceeded the request timeout entirely, which looks exactly like a
    missing/deleted consultant profile from the frontend's point of view
    (the request just never completes, so the UI's "not found" fallback
    fires) even though nothing was actually deleted.

    Every one of those lookups is batched here into ONE query total,
    regardless of how many consultants are being rendered — the query
    count for this function is now fixed (6 queries), not O(N).
    """
    if not consultants:
        return []

    cons_ids = [c.id for c in consultants]
    user_ids = [c.user_id for c in consultants if c.user_id]

    # 1. Assigned recruiters for ALL consultants in one query.
    recruiters_by_cons: dict[int, list[User]] = {cid: [] for cid in cons_ids}
    rec_rows = (await db.execute(
        select(RecruiterConsultant.consultant_id, User)
        .join(User, User.id == RecruiterConsultant.recruiter_id)
        .where(
            RecruiterConsultant.consultant_id.in_(cons_ids),
            RecruiterConsultant.is_active == True,
        )
    )).all()
    for cid, user in rec_rows:
        recruiters_by_cons.setdefault(cid, []).append(user)

    # 2. Experience row counts for ALL consultants in one grouped query.
    exp_rows = (await db.execute(
        select(ConsultantExperience.consultant_id, func.count())
        .where(ConsultantExperience.consultant_id.in_(cons_ids))
        .group_by(ConsultantExperience.consultant_id)
    )).all()
    exp_counts: dict[int, int] = {cid: cnt for cid, cnt in exp_rows}

    # 3. Linked user's last_login_at + resume_info for ALL consultants in one query.
    user_info: dict[int, tuple] = {}
    if user_ids:
        user_rows = (await db.execute(
            select(User.id, User.last_login_at, User.resume_info).where(User.id.in_(user_ids))
        )).all()
        user_info = {uid: (last_login, resume_info) for uid, last_login, resume_info in user_rows}

    # 4. Resume counts per user_id in one grouped query.
    resume_counts: dict[int, int] = {}
    if user_ids:
        resume_rows = (await db.execute(
            select(Resume.user_id, func.count())
            .where(Resume.user_id.in_(user_ids))
            .group_by(Resume.user_id)
        )).all()
        resume_counts = {uid: cnt for uid, cnt in resume_rows}

    # 5. Applications-sent counts per consultant_id in one grouped query.
    apps_rows = (await db.execute(
        select(Application.consultant_id, func.count())
        .where(Application.consultant_id.in_(cons_ids), Application.status == "SENT")
        .group_by(Application.consultant_id)
    )).all()
    apps_counts: dict[int, int] = {cid: cnt for cid, cnt in apps_rows}

    # 6. Latest non-null ATS score per user_id — fetch every scored resume
    # for these users ordered newest-first, then keep only the first
    # (most recent) one per user_id in Python. Bounded by however many
    # scored resumes these consultants actually have, not by N x M.
    latest_ats: dict[int, float] = {}
    if user_ids:
        ats_rows = (await db.execute(
            select(Resume.user_id, Resume.ats_score, Resume.created_at)
            .where(Resume.user_id.in_(user_ids), Resume.ats_score.isnot(None))
            .order_by(Resume.created_at.desc())
        )).all()
        for uid, score, _created in ats_rows:
            if uid not in latest_ats:
                latest_ats[uid] = score

    results: List[ConsultantAdminRowDTO] = []
    for c in consultants:
        recruiters = recruiters_by_cons.get(c.id, [])
        experience_count = exp_counts.get(c.id, 0)
        last_login_at, resume_info = user_info.get(c.user_id, (None, None)) if c.user_id else (None, None)
        total_resumes_generated = resume_counts.get(c.user_id, 0) if c.user_id else 0
        total_applications_sent = apps_counts.get(c.id, 0)
        latest_ats_score = latest_ats.get(c.user_id) if c.user_id else None

        completeness = 0
        if (c.primary_skills or "").strip() or (c.secondary_skills or "").strip():
            completeness += 30
        if experience_count > 0:
            completeness += 25
        if c.preferred_employment_types:
            completeness += 20
        if (c.work_authorization or "").strip():
            completeness += 15
        if len((c.current_location or "").strip()) >= 2:
            completeness += 10

        results.append(ConsultantAdminRowDTO(
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
            ats_score=float(latest_ats_score) if latest_ats_score is not None else None,
            linkedin_url=c.linkedin_url if c.linkedin_url is not None else (resume_info or {}).get("linkedin"),
            education=c.education or (resume_info or {}).get("education") or [],
            resume_info=resume_info,
            resume_rich_text=c.resume_rich_text,
            updated_at=c.updated_at.isoformat() if c.updated_at else "",
            has_resume=bool(c.base_resume_file_path or c.base_resume_text),
            last_login_at=last_login_at.isoformat() if last_login_at else None,
            total_applications_sent=total_applications_sent,
            total_resumes_generated=total_resumes_generated,
            completeness_pct=completeness,
        ))
    return results


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
            is_authorized=True,
            experience_years=req.experience_years,
            resume_info=req.resume_info,
            mobile_number=req.mobile_number,
            extension=req.extension,
            linkedin_url=req.linkedin_url,
            designation=req.designation,
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

        before = {"full_name": user.full_name, "email": user.email, "role": user.role, "is_authorized": user.is_authorized}

        user.full_name = req.full_name.strip()
        user.email = req.email
        user.role = req.role
        user.is_authorized = req.is_authorized
        if req.skills is not None:
            user.skills = req.skills
        if req.needsto_fetch_mail is not None:
            user.needsto_fetch_mail = req.needsto_fetch_mail
        if req.experience_years is not None:
            user.experience_years = req.experience_years
        if req.resume_info is not None:
            user.resume_info = req.resume_info
        if req.mobile_number is not None:
            user.mobile_number = req.mobile_number
        if req.extension is not None:
            user.extension = req.extension
        if req.linkedin_url is not None:
            user.linkedin_url = req.linkedin_url
        if req.designation is not None:
            user.designation = req.designation
        user = await UserRepository.update(db, user)

        # Apply consultant-only fields if this user has a linked consultant profile
        if req.role == "CONSULTANT":
            consultant = await ConsultantRepository.get_by_user_id(db, user.id)
            # BUG FIX: this only ever UPDATED an existing Consultant row —
            # if someone's role was CHANGED to CONSULTANT (as opposed to
            # being created as one from the start, which already
            # auto-creates this row above in create_user), no Consultant
            # row exists yet, so this whole block silently did nothing.
            # Their next visit to "My Profile" hit a 404 with no
            # explanation. Mirrors create_user's own auto-create logic
            # exactly, so both paths behave consistently.
            if not consultant:
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
                "role": user.role, "is_authorized": user.is_authorized,
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

        old_status = "Authorized" if user.is_authorized else "Unauthorized"
        user.is_authorized = (status_value == "ACTIVE" or status_value == "AUTHORIZED")
        user = await UserRepository.update(db, user)
        new_status = "Authorized" if user.is_authorized else "Unauthorized"

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

# Sentinel distinguishing "resume_info wasn't in the request body at all"
# from "resume_info was explicitly sent as null to clear it" — see the
# BUG FIX note in update_consultant() below. Plain `None` can't carry that
# distinction because it's also the value being cleared TO.
RESUME_INFO_NOT_PROVIDED = object()

_LABEL_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 /]{1,30}:\s*(.+)$")


def _split_categorized_segment(segment: str) -> List[str]:
    """Python port of SkillChipEditor.tsx's splitCategorized() — same
    format, same parsing, so resume_info['skills'] shows the same clean
    names the chip editor does rather than drifting from a different
    parser. See that file for the full rationale."""
    out: List[str] = []
    for i, piece in enumerate(p.strip() for p in segment.split(",")):
        if not piece:
            continue
        if i == 0:
            m = _LABEL_PREFIX_RE.match(piece)
            if m:
                piece = m.group(1).strip()
        piece = piece.lstrip("(").rstrip(")").strip()
        if piece:
            out.append(piece)
    return out


def _skills_to_list(value: Optional[str]) -> List[str]:
    """Same categorized-format parsing as the frontend chip editor
    (‖/| category separators, leftover 'Label:' prefixes, stray
    parentheses), case-insensitively deduped."""
    if not value or not value.strip():
        return []
    raw: List[str] = []
    for segment in re.split(r"[‖|]", value):
        raw.extend(_split_categorized_segment(segment))
    seen = set()
    deduped: List[str] = []
    for item in raw:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


class ConsultantAssignmentService:

    @staticmethod
    async def list_consultants(db: AsyncSession) -> List[ConsultantAdminRowDTO]:
        consultants = await ConsultantRepository.list_all(db)
        return await _consultants_to_dtos_bulk(db, consultants)

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
        linkedin_url: Optional[str] = None,
        education: Optional[list] = None,
        resume_info: Any = RESUME_INFO_NOT_PROVIDED,
        resume_rich_text: Optional[str] = None,
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
        if linkedin_url is not None:
            consultant.linkedin_url = linkedin_url
        if education is not None:
            consultant.education = education
        if resume_info is not RESUME_INFO_NOT_PROVIDED:
            consultant.resume_info = resume_info
        if resume_rich_text is not None:
            consultant.resume_rich_text = resume_rich_text

        # resume_info lives on User, not Consultant (see admin_create_consultant
        # and generate_resume() in resume_router.py, which both read/write it
        # there) — same field AddEditUserDrawer.tsx already edits from the
        # Users page, now also editable directly from this screen.
        # BUG FIX ("removing JSON doesn't save"): was `if resume_info is not
        # None` — indistinguishable from "field omitted from this request",
        # since None is also the value an explicit clear sends. Compare
        # against the sentinel instead, so an explicit null actually clears
        # it while a truly-omitted field still leaves it alone.
        if resume_info is not RESUME_INFO_NOT_PROVIDED and consultant.user_id:
            user_result = await db.execute(select(User).where(User.id == consultant.user_id))
            linked_user = user_result.scalars().first()
            if linked_user:
                linked_user.resume_info = resume_info

        # BUG FIX (resume_info["skills"] silently drifting out of sync):
        # generate_resume() in resume_router.py only ever backfills
        # resume_info["skills"] from consultant.primary_skills the FIRST
        # time a resume is generated (`if not resume_info.get("skills")`).
        # After that, nothing kept it in sync — editing Primary/Secondary
        # Skills via the chip editors had no path to resume_info at all,
        # so the JSON silently drifted from the consultant's actual
        # skills the moment either field was edited post-generation.
        # Only fires when skills actually changed AND this same request
        # isn't ALSO explicitly overwriting resume_info wholesale (that
        # takes precedence, handled above) — merges just the "skills" key
        # into the linked User's EXISTING resume_info, leaving every
        # other key (summary, experience, education, etc.) untouched.
        elif (primary_skills is not None or secondary_skills is not None) and consultant.user_id:
            user_result = await db.execute(select(User).where(User.id == consultant.user_id))
            linked_user = user_result.scalars().first()
            if linked_user:
                combined = ", ".join(filter(None, [consultant.primary_skills, consultant.secondary_skills]))
                skills_list = _skills_to_list(combined)
                existing_info = dict(linked_user.resume_info or {})
                existing_info["skills"] = skills_list
                linked_user.resume_info = existing_info

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
    async def update_resume_rich_text(
        db: AsyncSession, consultant_id: int, resume_rich_text: Optional[str], *, admin_id: str
    ) -> ConsultantAdminRowDTO:
        consultant = await ConsultantRepository.get_by_id(db, consultant_id)
        if not consultant:
            raise HTTPException(status_code=404, detail="Consultant not found")

        consultant.resume_rich_text = resume_rich_text
        consultant = await ConsultantRepository.update(db, consultant)

        await log_action(
            db, "USER_UPDATED",
            actor_user_id=admin_id, actor_name=admin_id, actor_role="ADMIN",
            entity_type="Consultant", entity_id=str(consultant.id),
            metadata={"type": "consultant_resume_rich_text_update"},
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