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
import asyncio
import logging
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
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signature sender resolution
# ---------------------------------------------------------------------------
# Delegates to email_template.resolve_sender_fields, which email_queue.py's
# send_email_now (the ACTUAL live endpoint the Apply buttons on Pending
# Applications/Requirements hit) also uses — see the comment there. Kept
# as a thin wrapper here so nothing else in this file has to change.
def _sender_fields(current_user: User, consultant: Consultant) -> dict:
    from email_template import resolve_sender_fields
    return resolve_sender_fields(current_user, consultant)


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
    # Rich HTML version of `body` with the sender's full signature card
    # (name, title, LinkedIn, contact block, company footer) — this is
    # what actually gets sent; `body` remains the plain-text fallback and
    # what the preview UI shows/lets you edit.
    html_body: Optional[str] = None
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

    # BUG FIX ("Insufficient Permission / ACCESS_TOKEN_SCOPE_INSUFFICIENT"
    # at actual send time): this used to hardcode send_permission_granted
    # = True on every successful token exchange, without ever checking
    # what scopes Google actually granted. Google's consent screen lets a
    # user uncheck the sensitive "Send email on your behalf" checkbox
    # while still completing sign-in with only openid/email — and an
    # unverified OAuth app can have the sensitive gmail.send scope
    # silently dropped even when requested. Either way the token exchange
    # still returns 200, so this endpoint was storing a token that looked
    # fully connected but couldn't actually send — the only place that
    # ever surfaced was a raw Gmail API 403 deep inside the send queue,
    # with no indication of what to fix. Google returns the actually-
    # granted scopes in `scope` (space-separated); only mark send
    # permission granted when gmail.send is genuinely present.
    granted_scopes = (token_data.get("scope") or "").split()
    has_send_scope = "https://www.googleapis.com/auth/gmail.send" in granted_scopes

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
        existing.send_permission_granted = has_send_scope
    else:
        db.add(ConsultantEmailToken(
            consultant_id=consultant.id,
            email_provider="GMAIL",
            email_address=gmail_email,
            access_token_encrypted=encrypt_token(access_token),
            refresh_token_encrypted=encrypt_token(refresh_token),
            token_expiry=token_expiry,
            send_permission_granted=has_send_scope,
        ))

    consultant.gmail_connected = True
    current_user.is_authorized = True
    await db.commit()

    if not has_send_scope:
        # Still a 200 — the account IS connected, just without send
        # permission — so the frontend can show a specific, actionable
        # message instead of the caller assuming this succeeded fully.
        return {
            "success": False,
            "message": (
                "Gmail connected, but send permission was not granted. "
                "Reconnect and make sure to allow \"Send email on your behalf\" "
                "on Google's permission screen — applications can't be sent until this is granted."
            ),
            "email_address": gmail_email,
            "send_permission_granted": False,
        }

    return {
        "success": True,
        "message": "Gmail connected successfully.",
        "email_address": gmail_email,
        "send_permission_granted": True,
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
    # BUG FIX: this used to also set current_user.is_authorized = False —
    # same conflation as email_queue.py's auto-deauthorize logic (see the
    # note there). Disconnecting Gmail correctly disables applying
    # (gmail_connected=False already does that), but it shouldn't also
    # lock the consultant out of the platform entirely — those are two
    # separate things. is_authorized is now only ever changed by an
    # explicit admin action in User Management.
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

    cc_email = current_user.email
    if current_user.role == "CONSULTANT":
        recruiter_email = await get_sales_recruiter_email(db, consultant)
        if recruiter_email:
            cc_email = recruiter_email
    elif current_user.role in ("ADMIN", "RECRUITER"):
        recruiter_email = await get_sales_recruiter_email(db, consultant)
        if recruiter_email and recruiter_email.lower() != (current_user.email or "").lower():
            cc_email = f"{current_user.email}, {recruiter_email}"

    email_content = build_application_email(
        vendor_contact_name=requirement.vendor_contact,
        role=requirement.role,
        consultant_name=consultant.full_name or "",
        consultant_email=consultant.email or "",
        consultant_phone=consultant.phone,
        primary_skills=consultant.primary_skills,
        **_sender_fields(current_user, consultant),
        for_preview=True,
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
        html_body=email_content["html_body"],
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

        # BUG FIX ("inactive user — restrict application send"): nothing
        # stopped an application from being sent for a consultant marked
        # Inactive — from either the Pending Applications (match-based
        # apply) or Requirements (direct apply) flow, since both route
        # through this same endpoint. Block it here so it's enforced
        # regardless of which frontend entry point was used.
        if consultant.status == "INACTIVE":
            raise HTTPException(
                status_code=400,
                detail=f"{consultant.full_name or 'This consultant'} is marked Inactive — "
                       f"applications can't be sent on their behalf until they're reactivated.",
            )

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

        cc_email = current_user.email
        if current_user.role == "CONSULTANT":
            recruiter_email = await get_sales_recruiter_email(db, consultant)
            if recruiter_email:
                cc_email = recruiter_email
        elif current_user.role in ("ADMIN", "RECRUITER"):
            recruiter_email = await get_sales_recruiter_email(db, consultant)
            if recruiter_email and recruiter_email.lower() != (current_user.email or "").lower():
                cc_email = f"{current_user.email}, {recruiter_email}"

        email_content = build_application_email(
            vendor_contact_name=requirement.vendor_contact,
            role=requirement.role,
            consultant_name=consultant.full_name or "",
            consultant_email=consultant.email or "",
            consultant_phone=consultant.phone,
            primary_skills=consultant.primary_skills,
            **_sender_fields(current_user, consultant),
        )

        if token and token.access_token_encrypted:
            access_token = decrypt_token(token.access_token_encrypted)
            from_email = token.email_address
        else:
            from gmail_send_service import get_service_account_access_token
            import os
            import asyncio
            sa_path = os.path.join(os.path.dirname(__file__), "service-account-key.json")
            from_email = consultant.email
            # Run the blocking JWT-signing/HTTP call off the event loop so
            # this async route doesn't stall other requests while it waits.
            access_token = await asyncio.to_thread(get_service_account_access_token, sa_path, from_email)

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
        #
        # BUG FIX (same root cause as apply_to_requirement in phase5.py):
        # generated_resume_id is Optional and independent of ats_score —
        # when it isn't populated (consultant applying with their base
        # resume, no tailored resume generated for this match), this whole
        # block was skipped and attachment_path stayed None the entire
        # way through, sending the email with no attachment and no error.
        # Fall back to the consultant's base resume in that case, same
        # source resume_router.py's download_base_resume already reads.
        attachment_path = None
        tmp_resume_path = None
        resume_source_path = None
        resume_display_name = None
        if request.generated_resume_id:
            try:
                resume_result = await db.execute(
                    select(GeneratedResume).where(GeneratedResume.id == request.generated_resume_id)
                )
                selected_resume = resume_result.scalars().first()
                if selected_resume and selected_resume.docx_path:
                    resume_source_path = selected_resume.docx_path
                    # See phase5.py's identical fix: GeneratedResume.filename
                    # is always built with a .pdf extension regardless of
                    # which file is actually attached — swap to .docx here
                    # since we're now sending the DOCX, not the PDF.
                    base_name = selected_resume.filename or f"Resume_{selected_resume.id}.pdf"
                    resume_display_name = Path(base_name).stem + ".docx"
            except Exception as attach_err:
                print(f"[confirm_send] resume lookup FAILED for resume_id={request.generated_resume_id}: {attach_err}")
                from error_logger import log_db_error
                await log_db_error(
                    stage="confirm_send_resume_attach",
                    error=attach_err,
                    source_type="resume",
                    source_id=request.generated_resume_id,
                )
        if resume_source_path is None and consultant.base_resume_file_path:
            resume_source_path = consultant.base_resume_file_path
            resume_display_name = Path(consultant.base_resume_file_path).name

        if resume_source_path:
            try:
                body_bytes = None
                if Path(resume_source_path).exists():
                    with open(resume_source_path, "rb") as f:
                        body_bytes = f.read()
                else:
                    from s3_service import download_file_from_s3
                    body_bytes, _ = download_file_from_s3(resume_source_path)
                if body_bytes:
                    import tempfile
                    safe_title = "".join(
                        c for c in (resume_display_name or "Resume.pdf")
                        if c.isalnum() or c in " -_."
                    ).strip() or "Resume.pdf"
                    tmp_dir = tempfile.mkdtemp(prefix="rap_apply_")
                    tmp_resume_path = os.path.join(tmp_dir, safe_title)
                    with open(tmp_resume_path, "wb") as f:
                        f.write(body_bytes)
                    attachment_path = tmp_resume_path
            except Exception as attach_err:
                print(f"[confirm_send] resume attach FAILED for path={resume_source_path}: {attach_err}")
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
                attachment_paths=[attachment_path] if attachment_path else [],
                html_body=email_content["html_body"],
                inline_images=email_content.get("inline_images"),
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
            # BUG FIX: was restricted to RECRUITER role only — admin-sent
            # confirm-sends got no sender attribution at all. recruiter_id
            # is used as a general "who sent this" field now.
            recruiter_id=current_user.id,
            generated_resume_id=request.generated_resume_id,
            ats_score_at_send=ats_score,
            vendor_email=requirement.vendor_email,
            cc_email=cc_email or None,
            gmail_message_id=send_result.get("gmail_message_id"),
            email_subject=email_content["subject"],
            email_body_preview=email_content["preview"],
            status="SENT",
            sent_at=datetime.now(timezone.utc),
            applied_at=datetime.now(timezone.utc),
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
    """
    Get stored email preview for a sent application.

    BUG FIX: this never returned an `attachments` field at all, so the
    Email Preview modal (consultant, recruiter, and admin all share the
    same modal code) always rendered zero attachments no matter what was
    actually sent — the resume link and any extra files (cover letter,
    etc.) were completely invisible here even though they were sent.
    Also, only the first attachment was ever persisted anywhere
    (Application.resume_attachment_path) — now reads the full list from
    Application.attachments_sent, falling back to the single legacy
    field for older rows sent before that column existed.

    Access control: admin sees any; recruiter only if the consultant is
    currently assigned to them; consultant only their own — same rule
    already enforced on the sibling resume-download endpoint below.
    """
    import mimetypes
    from s3_service import get_s3_file_metadata
    from email_queue import EMAIL_ATTACHMENT_S3_PREFIX, original_filename_from_ref

    result = await db.execute(select(Application).where(Application.id == application_id))
    app = result.scalars().first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")

    if current_user.role == "CONSULTANT":
        cons_result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        cons = cons_result.scalars().first()
        if not cons or cons.id != app.consultant_id:
            raise HTTPException(status_code=403, detail="Not your application.")
    elif current_user.role == "RECRUITER":
        rc_result = await db.execute(
            select(RecruiterConsultant).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.consultant_id == app.consultant_id,
            )
        )
        if not rc_result.scalars().first():
            raise HTTPException(status_code=403, detail="Not your consultant's application.")

    refs = app.attachments_sent or ([app.resume_attachment_path] if app.resume_attachment_path else [])

    attachments = []
    for ref in refs:
        if not ref:
            continue
        size_bytes, content_type = None, None
        local_path = os.path.join("/tmp/email_attachments", ref)
        if ref.startswith(EMAIL_ATTACHMENT_S3_PREFIX):
            size_bytes, content_type = get_s3_file_metadata(ref)
        elif os.path.exists(local_path):
            size_bytes = os.path.getsize(local_path)
            content_type = mimetypes.guess_type(local_path)[0]
        attachments.append({
            "filename": original_filename_from_ref(ref),
            "ref": ref,
            "mimeType": content_type or mimetypes.guess_type(ref)[0] or "application/octet-stream",
            "sizeBytes": size_bytes or 0,
        })

    return {
        "application_id": str(application_id),
        "email_subject": app.email_subject,
        "email_body_preview": app.email_body_preview,
        "vendor_email": app.vendor_email,
        "cc_email": app.cc_email,
        "status": app.status,
        "sent_at": app.sent_at,
        "attachments": attachments,
    }


@router.get("/applications/{application_id}/resume-download")
async def download_application_resume(
    application_id: int,
    # BUG FIX: this endpoint is used by TWO different frontend features —
    # admin/recruiter's "view" (which hands a presigned URL to Google's
    # Docs Viewer; Google's own servers fetch it, so browser CORS never
    # applies there) and the consultant's "download" button (which needs
    # real bytes in the browser, and DOES hit CORS against a presigned
    # URL, since this bucket has no CORS policy allowing direct browser
    # access). Rather than pick one behavior for both, force_stream lets
    # the download button explicitly ask for real bytes every time,
    # while the default (view) behavior is untouched.
    force_stream: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Stream the resume that was actually attached to a sent application.

    BUG FIX: the Applications Tracker's "Resume" column was a plain
    `<a href={resumePdfUrl}>` where resumePdfUrl was hardcoded to "" on
    the frontend (see applications.api.ts) — there was no backend field or
    endpoint to power it at all. Clicking an empty href triggers a full
    browser navigation to the current page, which the SPA router then
    treats like any other unauthenticated-looking navigation and bounces
    to login — that's the "navigates to login page" symptom, not an actual
    auth failure. This is a real authenticated download endpoint the
    frontend now calls via fetch/axios (so the Bearer token goes with the
    request) instead of a bare anchor tag.

    Access control: admin sees any; recruiter only if the consultant is
    currently assigned to them; consultant only their own.
    """
    from fastapi.responses import Response

    result = await db.execute(select(Application).where(Application.id == application_id))
    app = result.scalars().first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")
    if not app.generated_resume_id and not app.resume_attachment_path:
        raise HTTPException(status_code=404, detail="No resume was attached to this application.")

    if current_user.role == "CONSULTANT":
        cons_result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        cons = cons_result.scalars().first()
        if not cons or cons.id != app.consultant_id:
            raise HTTPException(status_code=403, detail="Not your application.")
    elif current_user.role == "RECRUITER":
        rc_result = await db.execute(
            select(RecruiterConsultant).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.consultant_id == app.consultant_id,
                RecruiterConsultant.is_active == True,
            )
        )
        if not rc_result.scalars().first():
            raise HTTPException(status_code=403, detail="Consultant not assigned to you.")
    elif current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Insufficient permissions.")

    filename = f"Resume_{application_id}.pdf"
    local_path = None
    s3_key = None

    if app.generated_resume_id:
        resume_result = await db.execute(select(GeneratedResume).where(GeneratedResume.id == app.generated_resume_id))
        resume = resume_result.scalars().first()
        if resume and resume.pdf_path:
            if Path(resume.pdf_path).exists():
                local_path = resume.pdf_path
            else:
                s3_key = resume.pdf_path
            filename = resume.filename or filename

    # BUG FIX: applications sent via the email-queue/Apply-to-Requirement
    # flow never have a generated_resume_id — the attached file is only
    # referenced as a raw path/S3 key on the EmailQueue item, now mirrored
    # onto Application.resume_attachment_path. Same S3-key-vs-local-path
    # resolution the send pipeline itself uses (email_queue.py).
    if local_path is None and s3_key is None and app.resume_attachment_path:
        ref = app.resume_attachment_path
        from email_queue import EMAIL_ATTACHMENT_S3_PREFIX
        import os as _os
        local_candidate = _os.path.join("/tmp/email_attachments", ref)
        if ref.startswith(EMAIL_ATTACHMENT_S3_PREFIX):
            s3_key = ref
        elif _os.path.exists(local_candidate):
            local_path = local_candidate
        elif Path(ref).exists():
            local_path = ref
        from email_queue import original_filename_from_ref
        filename = original_filename_from_ref(ref) or filename

    if local_path is None and s3_key is None:
        raise HTTPException(status_code=404, detail="Resume file could not be retrieved.")

    # BUG FIX: media_type was hardcoded to application/pdf regardless of
    # the actual file — a .docx attachment would download with a
    # Content-Type claiming it's a PDF, causing "Failed to load PDF
    # document" when a viewer tried to open it based on that header.
    import mimetypes
    guessed_type, _ = mimetypes.guess_type(filename)
    media_type = guessed_type or "application/octet-stream"

    # BUG FIX ("view keeps downloading"): a browser can only render a PDF
    # or image inline on its own — for a .docx (or any other Office
    # format), there is no in-browser renderer, so simply avoiding the
    # `attachment` Content-Disposition (see below) is NOT enough; the
    # browser still has nothing to show and falls back to a download
    # prompt regardless of headers. A prior version of this endpoint
    # always returned a presigned Spaces URL (JSON) for the frontend to
    # hand to an external viewer (Google Docs Viewer/Office Online)
    # instead of proxying bytes.
    #
    # BUG FIX (refined, not fully reverted): that approach broke the
    # consultant's plain "download" button — this bucket
    # (nyc3.digitaloceanspaces.com) has no CORS configuration allowing
    # browser access at all, so a presigned URL fetched directly by the
    # browser (as a real download needs to) failed outright with "blocked
    # by CORS policy: No 'Access-Control-Allow-Origin' header". Admin/
    # recruiter's "view" feature was unaffected by that, because it hands
    # the presigned URL to Google's Docs Viewer, whose own servers fetch
    # it server-side — browser CORS never applies there. So both behaviors
    # are correct for their respective caller: force_stream=True (used by
    # the download button) always streams real bytes through this
    # same-origin endpoint; the default (view) keeps using a presigned URL
    # when one is available, falling back to streaming only if presigning
    # itself fails.
    # CHANGED (view now serves DOCX, not a PDF conversion): this used to
    # convert an S3-stored .docx to PDF before serving, specifically so
    # the browser's built-in PDF viewer could render it inline. Per
    # updated requirement, View should show the actual DOCX file — same
    # as the fix already applied to My Resumes' View/Download. Browsers
    # have no native inline renderer for .docx, so this will generally
    # open a save/open-with-Word prompt in the new tab rather than an
    # in-page preview; that's an inherent browser limitation, not a bug.
    # BUG FIX ("view resume is downloading instead of previewing"): this
    # used to route non-PDF files to Google Docs Viewer via a presigned
    # URL — but Google's viewer isn't reliably able to fetch a
    # DigitalOcean Spaces presigned URL (unlike a real Google Drive
    # link), so it silently falls back to just downloading the raw file
    # instead of rendering a preview — exactly the "view keeps
    # downloading" symptom this was meant to fix, just reintroduced by a
    # different path. Reverted to on-demand DOCX->PDF conversion (the
    # same _convert_to_pdf already used for resume generation) for the
    # View path specifically — a real PDF is something every browser can
    # actually render inline, so this sidesteps Google's viewer
    # reliability entirely. The Download button (force_stream=True)
    # still gets the real original file untouched, since that's a
    # genuine "give me the file" request, not a preview.
    if s3_key:
        if not force_stream and filename.lower().endswith(".docx"):
            from s3_service import download_file_from_s3
            import tempfile
            from phase6 import _convert_to_pdf

            docx_bytes, _ = await asyncio.to_thread(download_file_from_s3, s3_key)
            if docx_bytes:
                try:
                    with tempfile.TemporaryDirectory(prefix="view_convert_") as tmpdir:
                        tmp_docx = Path(tmpdir) / "resume.docx"
                        tmp_pdf = Path(tmpdir) / "resume.pdf"
                        tmp_docx.write_bytes(docx_bytes)
                        converted = await asyncio.to_thread(_convert_to_pdf, tmp_docx, tmp_pdf)
                        if converted and tmp_pdf.exists():
                            pdf_bytes = tmp_pdf.read_bytes()
                            pdf_filename = filename[:-5] + ".pdf"
                            return Response(
                                content=pdf_bytes,
                                media_type="application/pdf",
                                headers={"Content-Disposition": f'inline; filename="{pdf_filename}"'},
                            )
                except Exception as exc:
                    logger.warning("On-demand DOCX->PDF conversion failed for view (application %s): %s", application_id, exc)
                    # Fall through to the presigned-URL/raw-bytes path below
                    # as a safety net so View degrades instead of breaking.
        if not force_stream:
            from s3_service import generate_presigned_url
            presigned = generate_presigned_url(s3_key)
            if presigned:
                return {"url": presigned, "filename": filename, "mimeType": media_type}
        from s3_service import download_file_from_s3
        body_bytes, _ = await asyncio.to_thread(download_file_from_s3, s3_key)
    else:
        with open(local_path, "rb") as f:
            body_bytes = f.read()
        # Same on-demand conversion for a locally-stored (non-S3) docx —
        # see the s3_key branch above for the full rationale.
        if not force_stream and filename.lower().endswith(".docx"):
            import tempfile
            from phase6 import _convert_to_pdf
            try:
                with tempfile.TemporaryDirectory(prefix="view_convert_") as tmpdir:
                    tmp_docx = Path(tmpdir) / "resume.docx"
                    tmp_pdf = Path(tmpdir) / "resume.pdf"
                    tmp_docx.write_bytes(body_bytes)
                    converted = await asyncio.to_thread(_convert_to_pdf, tmp_docx, tmp_pdf)
                    if converted and tmp_pdf.exists():
                        pdf_filename = filename[:-5] + ".pdf"
                        return Response(
                            content=tmp_pdf.read_bytes(),
                            media_type="application/pdf",
                            headers={"Content-Disposition": f'inline; filename="{pdf_filename}"'},
                        )
            except Exception as exc:
                logger.warning("On-demand DOCX->PDF conversion failed for view (application %s): %s", application_id, exc)
                # Fall through — body_bytes (the raw docx) still gets
                # served below via the normal inline response.

    if not body_bytes:
        raise HTTPException(status_code=404, detail="Resume file could not be retrieved.")

    return Response(
        content=body_bytes,
        media_type=media_type,
        # BUG FIX: was "attachment", which forces a browser save-dialog no
        # matter what the frontend does with the response. "inline" at
        # least lets natively-renderable types (pdf/image) open as a
        # plain viewer tab instead of always triggering a download.
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )