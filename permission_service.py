# permission_service.py
# ---------------------------------------------------------------------------
# Phase 7 - Permission Service
# Validates who can apply for which consultant/requirement
# ---------------------------------------------------------------------------

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select


async def resolve_apply_consultant(
    db: AsyncSession,
    current_user,
    consultant_id: int = None,
):
    """
    Resolve which consultant to apply for based on logged-in user role.

    - CONSULTANT → applies for themselves (ignore consultant_id)
    - RECRUITER  → must specify consultant_id from their assigned list
    - ADMIN      → can specify any consultant_id
    """
    from models import Consultant, RecruiterConsultant, User

    if current_user.role == "CONSULTANT":
        result = await db.execute(
            select(Consultant).join(User, User.id == Consultant.user_id).where(
                Consultant.user_id == current_user.id,
                Consultant.status == "ACTIVE",
                User.is_authorized == True
            )
        )
        consultant = result.scalars().first()
        if not consultant:
            raise HTTPException(
                status_code=404,
                detail="Consultant profile not found for this user.",
            )
        return consultant

    elif current_user.role == "RECRUITER":
        if not consultant_id:
            raise HTTPException(
                status_code=400,
                detail="Recruiter must specify consultant_id.",
            )
        result = await db.execute(
            select(RecruiterConsultant).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.consultant_id == consultant_id,
                RecruiterConsultant.is_active == True,
            )
        )
        if not result.scalars().first():
            raise HTTPException(
                status_code=403,
                detail="You are not assigned to this consultant.",
            )
        result = await db.execute(
            select(Consultant).join(User, User.id == Consultant.user_id).where(
                Consultant.id == consultant_id,
                Consultant.status == "ACTIVE",
                User.is_authorized == True
            )
        )
        consultant = result.scalars().first()
        if not consultant:
            raise HTTPException(status_code=404, detail="Consultant not found.")
        return consultant

    elif current_user.role == "ADMIN":
        if not consultant_id:
            raise HTTPException(
                status_code=400,
                detail="Admin must specify consultant_id.",
            )
        result = await db.execute(
            select(Consultant).join(User, User.id == Consultant.user_id).where(
                Consultant.id == consultant_id,
                Consultant.status == "ACTIVE",
                User.is_authorized == True
            )
        )
        consultant = result.scalars().first()
        if not consultant:
            raise HTTPException(status_code=404, detail="Consultant not found.")
        return consultant

    raise HTTPException(status_code=403, detail="Insufficient permissions to apply.")


async def assert_gmail_connected(db: AsyncSession, consultant_id: int):
    """
    Check consultant has Gmail connected with send permission.
    Returns token record if valid.
    """
    from models import ConsultantEmailToken

    result = await db.execute(
        select(ConsultantEmailToken).where(
            ConsultantEmailToken.consultant_id == consultant_id
        )
    )
    token = result.scalars().first()

    if not token:
        raise HTTPException(
            status_code=400,
            detail="Consultant Gmail not connected. Please connect Gmail first.",
        )
    if not token.send_permission_granted:
        raise HTTPException(
            status_code=403,
            detail="Gmail send permission not granted.",
        )
    if not token.email_address:
        raise HTTPException(
            status_code=400,
            detail="Consultant Gmail email address not found.",
        )
    return token


async def check_duplicate_application(
    db: AsyncSession,
    requirement_id: int,
    consultant_id: int,
) -> None:
    """Raise 409 if application already exists for same requirement+consultant."""
    from models import Application

    result = await db.execute(
        select(Application).where(
            Application.requirement_id == requirement_id,
            Application.consultant_id == consultant_id,
        )
    )
    existing = result.scalars().first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Application already submitted. Current status: {existing.status}",
        )


async def get_sales_recruiter_email(db: AsyncSession, consultant) -> str:
    """Get CC email for sales recruiter. Falls back to empty string."""
    from models import User

    if not consultant.sales_recruiter_user_id:
        return ""
    result = await db.execute(
        select(User).where(User.id == consultant.sales_recruiter_user_id)
    )
    recruiter = result.scalars().first()
    return recruiter.email if recruiter else ""


async def get_handling_recruiter(db: AsyncSession, consultant) -> dict | None:
    """Resolve the recruiter actually handling this consultant, for the
    email signature's "Employer Details" block — distinct from
    get_sales_recruiter_email above, which only returns a bare email for
    CC purposes. Same two-step fallback already proven in email_queue.py's
    CC resolution:
      1. Active RecruiterConsultant assignment (the real roster mapping).
      2. Consultant.sales_recruiter_user_id (legacy/simple single-field
         assignment), only if no active roster mapping exists.
    Returns None if the consultant has no assigned recruiter either way —
    callers should omit the Employer Details block entirely in that case
    rather than show a blank one.
    """
    from models import User, RecruiterConsultant
    recruiter = None
    mapped = await db.execute(
        select(User)
        .join(RecruiterConsultant, RecruiterConsultant.recruiter_id == User.id)
        .where(
            RecruiterConsultant.consultant_id == consultant.id,
            RecruiterConsultant.is_active == True,
        )
        .limit(1)
    )
    recruiter = mapped.scalars().first()
    if not recruiter and consultant.sales_recruiter_user_id:
        fallback = await db.execute(
            select(User).where(User.id == consultant.sales_recruiter_user_id)
        )
        recruiter = fallback.scalars().first()
    if not recruiter:
        return None
    return {
        "employer_name": recruiter.full_name or "",
        "employer_title": getattr(recruiter, "designation", None) or "Recruiter",
        "employer_email": recruiter.email or "",
        "employer_phone": getattr(recruiter, "mobile_number", None),
        "employer_extension": getattr(recruiter, "extension", None),
        "employer_linkedin_url": getattr(recruiter, "linkedin_url", None),
    }