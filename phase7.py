# phase7.py
# ---------------------------------------------------------------------------
# Phase 7 — Consultant Gmail OAuth + Application Send Flow
#
# Follows the same router pattern as phase2.py–phase6.py.
# Reuses existing auth.py (get_current_user), database.py (get_db),
# and the EXISTING Application/Consultant/Requirement models — does not
# duplicate or replace anything from earlier phases.
#
# NOTE: uses status="SENT" (not "APPLIED") to stay compatible with the
# existing Application.VALID_STATUSES and Phase 5 dashboard logic.
# ---------------------------------------------------------------------------

import os
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func

from database import get_db
from auth import get_current_user
from models import (
    User,
    Consultant,
    RecruiterConsultant,
    Requirement,
    Application,
    ConsultantEmailToken,
    GeneratedResume,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class GmailConnectRequest(BaseModel):
    code: str = Field(..., min_length=1, description="OAuth authorization code")
    redirect_uri: str = Field(..., min_length=1)


class GmailStatusResponse(BaseModel):
    model_config = {"from_attributes": True}
    connected: bool
    email_address: Optional[str] = None
    send_permission_granted: bool = False


class EmailPreviewRequest(BaseModel):
    requirement_id: int
    consultant_id: Optional[int] = None


class EmailPreviewResponse(BaseModel):
    subject: str
    body: str
    to_email: Optional[str] = None
    cc_email: Optional[str] = None
    consultant_name: str
    consultant_email: str
    requirement_role: str
    # BUG FIX: previously missing — confirm-send needs the consultant's
    # actual generated resume for this requirement (id + real ATS score) to
    # send along, and the frontend had nothing to forward, so every
    # confirm-send silently defaulted ats_score to 0 (always failing the
    # server-side >=80 gate) and generated_resume_id to None (no attachment
    # ever sent). Populated from the current is_final GeneratedResume below.
    generated_resume_id: Optional[int] = None
    ats_score: Optional[float] = None
    attachment_filename: Optional[str] = None


class ConfirmSendRequest(BaseModel):
    requirement_id: int
    consultant_id: Optional[int] = None
    generated_resume_id: Optional[int] = None
    ats_score: Optional[float] = None


class ConfirmSendResponse(BaseModel):
    success: bool
    application_id: Optional[int] = None
    gmail_message_id: Optional[str] = None
    message: str


class ApplicationResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    requirement_id: Optional[str] = None
    consultant_id: Optional[str] = None
    recruiter_id: Optional[str] = None
    vendor_email: Optional[str] = None
    cc_email: Optional[str] = None
    gmail_message_id: Optional[str] = None
    email_subject: Optional[str] = None
    email_body_preview: Optional[str] = None
    status: str
    ats_score_at_send: Optional[float] = None
    sent_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_orm_obj(cls, app: Application) -> "ApplicationResponse":
        return cls(
            id=str(app.id),
            requirement_id=str(app.requirement_id) if app.requirement_id else None,
            consultant_id=str(app.consultant_id) if app.consultant_id else None,
            recruiter_id=str(app.recruiter_id) if app.recruiter_id else None,
            vendor_email=app.vendor_email,
            cc_email=app.cc_email,
            gmail_message_id=app.gmail_message_id,
            email_subject=app.email_subject,
            email_body_preview=app.email_body_preview,
            status=app.status,
            ats_score_at_send=float(app.ats_score_at_send) if app.ats_score_at_send else None,
            sent_at=app.sent_at,
            created_at=app.created_at,
        )


class ApplicationStatusUpdateRequest(BaseModel):
    status: str = Field(..., description="New status for the application")


class PaginatedApplications(BaseModel):
    data: List[ApplicationResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


# ---------------------------------------------------------------------------
# Task 1: Consultant Gmail OAuth
# ---------------------------------------------------------------------------

@router.get("/consultant/gmail/status", response_model=GmailStatusResponse)
async def get_gmail_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if the logged-in consultant has Gmail connected."""
    if current_user.role != "CONSULTANT":
        raise HTTPException(status_code=403, detail="Only consultants can check their own Gmail status.")

    result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
    consultant = result.scalars().first()
    if not consultant:
        return GmailStatusResponse(connected=False, send_permission_granted=False)

    result = await db.execute(
        select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant.id)
    )
    token = result.scalars().first()
    if not token:
        return GmailStatusResponse(connected=False, send_permission_granted=False)

    return GmailStatusResponse(
        connected=True,
        email_address=token.email_address,
        send_permission_granted=token.send_permission_granted,
    )


@router.get("/api/consultants/{consultant_id}/gmail-status", response_model=GmailStatusResponse)
async def get_consultant_gmail_status(
    consultant_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin/Recruiter check Gmail status for a specific consultant."""
    if current_user.role not in ("ADMIN", "RECRUITER"):
        raise HTTPException(status_code=403, detail="Insufficient permissions.")

    result = await db.execute(
        select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant_id)
    )
    token = result.scalars().first()
    if not token:
        return GmailStatusResponse(connected=False, send_permission_granted=False)

    return GmailStatusResponse(
        connected=True,
        email_address=token.email_address,
        send_permission_granted=token.send_permission_granted,
    )


@router.post("/consultant/gmail/connect")
async def connect_gmail(
    request: GmailConnectRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Step 1 of Gmail OAuth: exchange auth code for tokens."""
    from gmail_send_service import encrypt_token

    if current_user.role != "CONSULTANT":
        raise HTTPException(status_code=403, detail="Only consultants can connect Gmail.")

    result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
    consultant = result.scalars().first()
    if not consultant:
        raise HTTPException(status_code=404, detail="Consultant profile not found.")

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth not configured.")

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            token_res = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": request.code,
                    "grant_type": "authorization_code",
                    "redirect_uri": request.redirect_uri,
                },
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Failed to reach Google: {exc}")

    if token_res.status_code != 200:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth code.")

    token_data = token_res.json()
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    gmail_email = ""
    try:
        import jwt as pyjwt
        id_token = token_data.get("id_token", "")
        if id_token:
            decoded = pyjwt.decode(id_token, options={"verify_signature": False}, algorithms=["RS256"])
            gmail_email = decoded.get("email", "")
    except Exception:
        pass

    expires_in = token_data.get("expires_in", 3600)
    token_expiry = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=expires_in)

    result = await db.execute(
        select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant.id)
    )
    existing = result.scalars().first()

    if existing:
        existing.email_address = gmail_email
        existing.access_token_encrypted = encrypt_token(access_token)
        existing.refresh_token_encrypted = encrypt_token(refresh_token)
        existing.token_expiry = token_expiry
        existing.send_permission_granted = True
    else:
        db.add(ConsultantEmailToken(
            consultant_id=consultant.id,
            email_provider="GMAIL",
            email_address=gmail_email,
            access_token_encrypted=encrypt_token(access_token),
            refresh_token_encrypted=encrypt_token(refresh_token),
            token_expiry=token_expiry,
            send_permission_granted=True,
        ))

    consultant.gmail_connected = True
    await db.commit()

    return {
        "success": True,
        "message": "Gmail connected successfully.",
        "email_address": gmail_email,
    }


@router.delete("/consultant/gmail/disconnect")
async def disconnect_gmail(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disconnect consultant Gmail — disables apply button."""
    if current_user.role != "CONSULTANT":
        raise HTTPException(status_code=403, detail="Only consultants can disconnect Gmail.")

    result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
    consultant = result.scalars().first()
    if not consultant:
        raise HTTPException(status_code=404, detail="Consultant profile not found.")

    result = await db.execute(
        select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant.id)
    )
    token = result.scalars().first()
    if token:
        await db.delete(token)

    consultant.gmail_connected = False
    await db.commit()

    return {"success": True, "message": "Gmail disconnected."}


# ---------------------------------------------------------------------------
# Task 2 & 3: Email Preview + Confirm Send
# ---------------------------------------------------------------------------

@router.post("/applications/preview", response_model=EmailPreviewResponse)
async def get_email_preview(
    request: EmailPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate email preview before sending."""
    from email_template import build_application_email
    from permission_service import resolve_apply_consultant, get_sales_recruiter_email

    consultant = await resolve_apply_consultant(db, current_user, request.consultant_id)

    result = await db.execute(select(Requirement).where(Requirement.id == request.requirement_id))
    requirement = result.scalars().first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found.")

    cc_email = await get_sales_recruiter_email(db, consultant)

    email_content = build_application_email(
        vendor_contact_name=requirement.vendor_contact,
        role=requirement.role,
        consultant_name=consultant.full_name or "",
        consultant_email=consultant.email or "",
        consultant_phone=consultant.phone,
        primary_skills=consultant.primary_skills,
    )

    # BUG FIX: preview previously returned nothing about the generated
    # resume — confirm-send needs its id (to attach) and real ats_score
    # (for the server-side >=80 re-check). Same is_final lookup pattern
    # phase6.py uses for the consultant-side download/history endpoints.
    generated_resume_result = await db.execute(
        select(GeneratedResume).where(
            GeneratedResume.consultant_id == consultant.id,
            GeneratedResume.requirement_id == request.requirement_id,
            GeneratedResume.is_final == True,
        )
    )
    generated_resume = generated_resume_result.scalars().first()

    return EmailPreviewResponse(
        subject=email_content["subject"],
        body=email_content["body"],
        to_email=requirement.vendor_email,
        cc_email=cc_email or None,
        consultant_name=consultant.full_name or "",
        consultant_email=consultant.email or "",
        requirement_role=requirement.role,
        generated_resume_id=generated_resume.id if generated_resume else None,
        ats_score=float(generated_resume.ats_score) if generated_resume and generated_resume.ats_score is not None else None,
        attachment_filename=generated_resume.filename if generated_resume else None,
    )


@router.post("/applications/confirm-send", response_model=ConfirmSendResponse)
async def confirm_send(
    request: ConfirmSendRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Main confirm-send endpoint.
    Validates permissions → checks ATS → prevents duplicates
    → builds email → sends via Gmail → records application.
    """
    from permission_service import (
        resolve_apply_consultant,
        assert_gmail_connected,
        check_duplicate_application,
        get_sales_recruiter_email,
    )
    from email_template import build_application_email
    from gmail_send_service import send_application_email_async, decrypt_token

    try:
        consultant = await resolve_apply_consultant(db, current_user, request.consultant_id)

        ats_score = request.ats_score or 0
        if ats_score < 80:
            raise HTTPException(
                status_code=400,
                detail=f"ATS score {ats_score} is below minimum 80. Please improve resume first.",
            )

        await check_duplicate_application(db, request.requirement_id, consultant.id)

        token_res = await db.execute(select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant.id))
        token = token_res.scalars().first()

        result = await db.execute(select(Requirement).where(Requirement.id == request.requirement_id))
        requirement = result.scalars().first()
        if not requirement:
            raise HTTPException(status_code=404, detail="Requirement not found.")

        cc_email = await get_sales_recruiter_email(db, consultant)

        email_content = build_application_email(
            vendor_contact_name=requirement.vendor_contact,
            role=requirement.role,
            consultant_name=consultant.full_name or "",
            consultant_email=consultant.email or "",
            consultant_phone=consultant.phone,
            primary_skills=consultant.primary_skills,
        )

        if token and token.access_token_encrypted:
            access_token = decrypt_token(token.access_token_encrypted)
            from_email = token.email_address
        else:
            from gmail_send_service import get_service_account_access_token
            import os
            sa_path = os.path.join(os.path.dirname(__file__), "service-account-key.json")
            from_email = consultant.email
            access_token = get_service_account_access_token(sa_path, from_email)

        # BUG FIX: was querying the `Resume` table (self-service consultant
        # resume builder) by generated_resume_id — but Application.generated_resume_id's
        # actual foreign key points at `generated_resumes` (GeneratedResume),
        # the per-requirement tailored resume this screen is previewing/
        # sending. Querying the wrong table meant `selected_resume` was
        # always None here, so no resume was ever attached even when
        # request.generated_resume_id was populated. Also handles pdf_path
        # holding a Spaces object key instead of a local path — same
        # pattern phase6.py's download endpoint uses, since the local file
        # is deleted after upload.
        attachment_path = None
        tmp_resume_path = None
        if request.generated_resume_id:
            try:
                resume_result = await db.execute(
                    select(GeneratedResume).where(GeneratedResume.id == request.generated_resume_id)
                )
                selected_resume = resume_result.scalars().first()
                if selected_resume and selected_resume.pdf_path:
                    body_bytes = None
                    if Path(selected_resume.pdf_path).exists():
                        with open(selected_resume.pdf_path, "rb") as f:
                            body_bytes = f.read()
                    else:
                        from s3_service import download_file_from_s3
                        body_bytes, _ = download_file_from_s3(selected_resume.pdf_path)
                    if body_bytes:
                        import tempfile
                        safe_title = "".join(
                            c for c in (selected_resume.filename or f"Resume_{selected_resume.id}.pdf")
                            if c.isalnum() or c in " -_."
                        ).strip() or f"Resume_{selected_resume.id}.pdf"
                        tmp_dir = tempfile.mkdtemp(prefix="rap_apply_")
                        tmp_resume_path = os.path.join(tmp_dir, safe_title)
                        with open(tmp_resume_path, "wb") as f:
                            f.write(body_bytes)
                        attachment_path = tmp_resume_path
            except Exception as attach_err:
                print(f"[confirm_send] resume attach FAILED for resume_id={request.generated_resume_id}: {attach_err}")
                from error_logger import log_db_error
                await log_db_error(
                    stage="confirm_send_resume_attach",
                    error=attach_err,
                    source_type="resume",
                    source_id=request.generated_resume_id,
                )

        try:
            send_result = await send_application_email_async(
                access_token=access_token,
                from_email=from_email,
                to_email=requirement.vendor_email or "",
                cc_email=cc_email,
                subject=email_content["subject"],
                body=email_content["body"],
                attachment_path=attachment_path,
            )
        finally:
            if tmp_resume_path:
                try:
                    os.remove(tmp_resume_path)
                    os.rmdir(os.path.dirname(tmp_resume_path))
                except OSError:
                    pass

        # NOTE: status="SENT" (existing VALID_STATUSES), not "APPLIED"
        application = Application(
            requirement_id=request.requirement_id,
            consultant_id=consultant.id,
            recruiter_id=current_user.id if current_user.role == "RECRUITER" else None,
            generated_resume_id=request.generated_resume_id,
            ats_score_at_send=ats_score,
            vendor_email=requirement.vendor_email,
            cc_email=cc_email or None,
            gmail_message_id=send_result.get("gmail_message_id"),
            email_subject=email_content["subject"],
            email_body_preview=email_content["preview"],
            status="SENT",
            sent_at=datetime.now(timezone.utc),
        )
        db.add(application)
        await db.commit()
        await db.refresh(application)

        return ConfirmSendResponse(
            success=True,
            application_id=application.id,
            gmail_message_id=send_result.get("gmail_message_id"),
            message="Application sent successfully!",
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from error_logger import log_db_error
        await log_db_error(
            stage="confirm_send",
            error=e,
            source_type="application",
            requirement_id=request.requirement_id,
            consultant_id=request.consultant_id,
        )
        try:
            failed_app = Application(
                requirement_id=request.requirement_id,
                consultant_id=request.consultant_id,
                status="FAILED",
                error_message=str(e),
                sent_at=datetime.now(timezone.utc),
            )
            db.add(failed_app)
            await db.commit()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to send application: {str(e)}")


# ---------------------------------------------------------------------------
# Application History & Status APIs
# ---------------------------------------------------------------------------

@router.get("/applications/history", response_model=PaginatedApplications)
async def get_application_history(
    page: int = 1,
    page_size: int = 10,
    consultant_id: Optional[int] = None,
    requirement_id: Optional[int] = None,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get application history.
    - CONSULTANT sees only their own applications
    - RECRUITER sees applications for their assigned consultants
    - ADMIN sees all applications
    """
    query = select(Application)

    if current_user.role == "CONSULTANT":
        result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        consultant = result.scalars().first()
        if consultant:
            query = query.where(Application.consultant_id == consultant.id)
        else:
            return PaginatedApplications(data=[], total=0, page=page, page_size=page_size, total_pages=0)

    elif current_user.role == "RECRUITER":
        assigned = select(RecruiterConsultant.consultant_id).where(
            RecruiterConsultant.recruiter_id == current_user.id,
            RecruiterConsultant.is_active == True,
        )
        query = query.where(Application.consultant_id.in_(assigned))

    if consultant_id:
        query = query.where(Application.consultant_id == consultant_id)
    if requirement_id:
        query = query.where(Application.requirement_id == requirement_id)
    if status:
        query = query.where(Application.status == status)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    query = query.order_by(Application.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    apps = (await db.execute(query)).scalars().all()
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0

    return PaginatedApplications(
        data=[ApplicationResponse.from_orm_obj(a) for a in apps],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/applications/{application_id}", response_model=ApplicationResponse)
async def get_application(
    application_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get single application by ID."""
    result = await db.execute(select(Application).where(Application.id == application_id))
    app = result.scalars().first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")
    return ApplicationResponse.from_orm_obj(app)


@router.patch("/applications/{application_id}/status")
async def update_application_status(
    application_id: int,
    request: ApplicationStatusUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update application status. Used by recruiters/admins to track progress."""
    # NOTE: uses existing Application.VALID_STATUSES, not Phase 7's original set
    if request.status not in Application.VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {sorted(Application.VALID_STATUSES)}",
        )

    result = await db.execute(select(Application).where(Application.id == application_id))
    app = result.scalars().first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")

    app.status = request.status
    await db.commit()
    return {"success": True, "message": f"Application status updated to {request.status}"}


@router.get("/recruiter/applications/email/{application_id}/preview")
async def get_application_email_preview(
    application_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get stored email preview for a sent application."""
    result = await db.execute(select(Application).where(Application.id == application_id))
    app = result.scalars().first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")

    return {
        "application_id": str(application_id),
        "email_subject": app.email_subject,
        "email_body_preview": app.email_body_preview,
        "vendor_email": app.vendor_email,
        "cc_email": app.cc_email,
        "status": app.status,
        "sent_at": app.sent_at,
    }