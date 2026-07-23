
# ---------------------------------------------------------------------------
# Email Queue endpoints
# Handles consultant email queue management
# ---------------------------------------------------------------------------
import os
import uuid
import math
from fastapi import UploadFile, File
 
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
 
from database import get_db
from auth import get_current_user
from models import User
 
router = APIRouter()
 
 
# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
 
class EmailQueueCreateRequest(BaseModel):
    consultant_id: Optional[int] = None
    requirement_id: Optional[int] = None
    from_email: str
    to_email: str
    cc_email: Optional[str] = None
    subject: str
    content: Optional[str] = None
    attachments: Optional[List[str]] = None
 
    @field_validator('requirement_id', 'consultant_id', mode='before')
    @classmethod
    def zero_to_none(cls, v):
        """Frontend sends Number('') = 0 for unset IDs — treat 0 as None."""
        if v == 0 or v == '' or v is None:
            return None
        return v
 
 
class EmailQueueStatusUpdate(BaseModel):
    status: str
 
 
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
 
async def _assert_email_queue_access(db: AsyncSession, current_user: User, item) -> None:
    """
    BUG FIX: get/update-status/delete on a single email-queue item had NO
    ownership or role check at all — any authenticated user could view,
    change the status of, or delete ANY other consultant's queued
    application email just by knowing the item id. list/create already
    scoped correctly by role; this brings the single-item endpoints in
    line with that same scoping.
    """
    from models import Consultant
    if current_user.role in ("ADMIN", "RECRUITER"):
        return
    if current_user.role == "CONSULTANT":
        result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        consultant = result.scalars().first()
        if consultant and item.consultant_id == consultant.id:
            return
    raise HTTPException(status_code=403, detail="Insufficient permissions for this email queue item.")
 
 
# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
 
@router.post("/api/consultant/email-queue")
async def create_email_queue(
    body: EmailQueueCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add email to queue."""
    from models import EmailQueue
    from models import Consultant
    from sqlalchemy import select as sa_select
 
    consultant_id = None
 
    if current_user.role == "ADMIN":
        if body.consultant_id:
            # Admin provided consultant_id explicitly — verify it exists
            cons_result = await db.execute(
                sa_select(Consultant).where(Consultant.id == body.consultant_id)
            )
            consultant = cons_result.scalars().first()
            if not consultant:
                raise HTTPException(status_code=404, detail="Consultant not found.")
            consultant_id = consultant.id
        else:
            # Admin didn't provide consultant_id — try to resolve from user's email
            cons_result = await db.execute(
                sa_select(Consultant).where(Consultant.email == current_user.email)
            )
            consultant = cons_result.scalars().first()
            if consultant:
                consultant_id = consultant.id
            else:
                # Fallback: pick first active consultant
                cons_result = await db.execute(
                    sa_select(Consultant).where(Consultant.status == "ACTIVE").limit(1)
                )
                consultant = cons_result.scalars().first()
                if not consultant:
                    raise HTTPException(status_code=400, detail="No consultants found in the system.")
                consultant_id = consultant.id
    elif current_user.role == "RECRUITER":
        # Recruiter: same logic — try to resolve or fallback
        if body.consultant_id:
            cons_result = await db.execute(
                sa_select(Consultant).where(Consultant.id == body.consultant_id)
            )
            consultant = cons_result.scalars().first()
            if not consultant:
                raise HTTPException(status_code=404, detail="Consultant not found.")
            consultant_id = consultant.id
        else:
            cons_result = await db.execute(
                sa_select(Consultant).where(Consultant.email == current_user.email)
            )
            consultant = cons_result.scalars().first()
            if consultant:
                consultant_id = consultant.id
            else:
                cons_result = await db.execute(
                    sa_select(Consultant).where(Consultant.status == "ACTIVE").limit(1)
                )
                consultant = cons_result.scalars().first()
                if not consultant:
                    raise HTTPException(status_code=400, detail="No consultants found in the system.")
                consultant_id = consultant.id
    else:
        # Consultant: resolve from logged-in user
        cons_result = await db.execute(
            sa_select(Consultant).where(Consultant.user_id == current_user.id)
        )
        consultant = cons_result.scalars().first()
        consultant_id = consultant.id if consultant else body.consultant_id
        if not consultant_id:
            raise HTTPException(status_code=400, detail="Consultant profile not found.")
 
    final_cc = body.cc_email.strip() if body.cc_email else ""
    if final_cc:
        if current_user.email not in final_cc:
            final_cc = f"{final_cc},{current_user.email}"
    else:
        final_cc = current_user.email
 
    item = EmailQueue(
        consultant_id=consultant_id,
        requirement_id=body.requirement_id,
        from_email=body.from_email,
        to_email=body.to_email,
        cc_email=final_cc,
        subject=body.subject,
        content=body.content,
        attachments=body.attachments,
        status="QUEUED",
    )
    db.add(item)
    # BUG FIX: the Apply button on the dashboard and "My Applications" both
    # only update once the ACTUAL send happens — a background worker run
    # up to 60s+ later. Until then the button kept showing "Apply Now"
    # again, letting a consultant queue a second email for the same
    # requirement before the first one even sent. Mark this as applied
    # the moment it's queued instead; the worker loop below updates this
    # same Application row to SENT/FAILED once it actually goes out.
    if body.requirement_id:
        from models import Application, RequirementConsultantMatch
        existing_app_result = await db.execute(
            select(Application).where(
                Application.consultant_id == consultant_id,
                Application.requirement_id == body.requirement_id,
            )
        )
        existing_app = existing_app_result.scalars().first()
        if existing_app:
            existing_app.status = "PENDING"
            existing_app.vendor_email = body.to_email
            existing_app.cc_email = final_cc
            existing_app.email_subject = body.subject
        else:
            db.add(Application(
                consultant_id=consultant_id,
                requirement_id=body.requirement_id,
                status="PENDING",
                vendor_email=body.to_email,
                cc_email=final_cc,
                email_subject=body.subject,
            ))
        match_result = await db.execute(
            select(RequirementConsultantMatch).where(
                RequirementConsultantMatch.requirement_id == body.requirement_id,
                RequirementConsultantMatch.consultant_id == consultant_id,
            )
        )
        match = match_result.scalars().first()
        if match:
            match.status = "APPLIED"
    await db.commit()
    await db.refresh(item)
    return {
        "success": True,
        "id": str(item.id),
        "status": item.status,
    }
 
 
@router.get("/api/consultant/email-queue")
async def list_email_queue(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all emails in queue."""
    from models import EmailQueue, Consultant
 
    query = select(EmailQueue)
    count_query = select(func.count()).select_from(EmailQueue)
 
    if current_user.role == "CONSULTANT":
        result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        consultant = result.scalars().first()
        if not consultant:
            return {"data": [], "total": 0, "page": page, "page_size": page_size, "pages": 1}
        query = query.where(EmailQueue.consultant_id == consultant.id)
        count_query = count_query.where(EmailQueue.consultant_id == consultant.id)
    elif current_user.role != "ADMIN" and current_user.role != "RECRUITER":
        raise HTTPException(status_code=403, detail="Insufficient permissions")
 
    # BUG FIX: page/page_size were accepted by the frontend
    # (fetchEmailQueueItems always sent them) but silently ignored here —
    # every request returned the entire table regardless of page, and the
    # response had no `page`/`page_size`/`total_pages` fields at all, so
    # AdminEmailQueueListPage's `data?.pages ?? 1` always fell back to 1.
    # Next/Prev controls looked like they worked but always showed the
    # same full list. Now actually paginated server-side.
    total = (await db.execute(count_query)).scalar_one()
 
    result = await db.execute(
        query.order_by(EmailQueue.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = result.scalars().all()
    return {
        "data": [
            {
                "id": str(item.id),
                "consultant_id": str(item.consultant_id),
                "requirement_id": str(item.requirement_id) if item.requirement_id else None,
                "from_email": item.from_email,
                "to_email": item.to_email,
                "subject": item.subject,
                "content": item.content,
                "attachments": item.attachments,
                "status": item.status,
                "status_message": item.status_text,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            }
            for item in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 1,
    }
 
 
@router.get("/api/consultant/email-queue/{item_id}")
async def get_email_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get single email queue item."""
    from models import EmailQueue
    result = await db.execute(
        select(EmailQueue).where(EmailQueue.id == item_id)
    )
    item = result.scalars().first()
    if not item:
        raise HTTPException(status_code=404, detail="Email queue item not found")
    await _assert_email_queue_access(db, current_user, item)
    return {
        "id": str(item.id),
        "consultant_id": str(item.consultant_id),
        "requirement_id": str(item.requirement_id) if item.requirement_id else None,
        "from_email": item.from_email,
        "to_email": item.to_email,
        "subject": item.subject,
        "content": item.content,
        "attachments": item.attachments,
        "status": item.status,
        "status_message": item.status_text,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }
 
 
@router.patch("/api/consultant/email-queue/{item_id}/status")
async def update_email_queue_status(
    item_id: int,
    body: EmailQueueStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update email queue item status."""
    from models import EmailQueue
    result = await db.execute(
        select(EmailQueue).where(EmailQueue.id == item_id)
    )
    item = result.scalars().first()
    if not item:
        raise HTTPException(status_code=404, detail="Email queue item not found")
    await _assert_email_queue_access(db, current_user, item)
 
    valid_statuses = {"QUEUED", "SENT", "FAILED"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {sorted(valid_statuses)}"
        )
 
    item.status = body.status
    await db.commit()
    return {"success": True, "id": str(item.id), "status": item.status}
 
 
@router.delete("/api/consultant/email-queue/{item_id}")
async def delete_email_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete email queue item."""
    from models import EmailQueue
    result = await db.execute(
        select(EmailQueue).where(EmailQueue.id == item_id)
    )
    item = result.scalars().first()
    if not item:
        raise HTTPException(status_code=404, detail="Email queue item not found")
    await _assert_email_queue_access(db, current_user, item)
    await db.delete(item)
    await db.commit()
    return {"success": True, "message": f"Email queue item {item_id} deleted"}
 
UPLOAD_DIR = "/tmp/email_attachments"
os.makedirs(UPLOAD_DIR, exist_ok=True)
 
# Prefix so download_file_from_s3 in the send worker can tell "this is a
# Spaces key" apart from a bare local filename (legacy queue rows saved
# before this fix still have a plain filename with no prefix).
EMAIL_ATTACHMENT_S3_PREFIX = "email-queue-attachments/"
 
# TESTING GUARD: while we validate the email queue pipeline, only allow sends
# to this domain. Remove/relax this check once testing is complete and real
# sends to arbitrary vendor/client addresses are approved. Lives here (not
# main.py) so both the background worker and the send-now endpoint below
# read the exact same value.
EMAIL_QUEUE_TEST_DOMAIN_SUFFIX = "@savantisintelli.com"
 
async def process_single_email_queue_item(session: AsyncSession, item) -> None:
    """
    Send one QUEUED EmailQueue item via Gmail and update its status
    (SENT/FAILED), including creating/updating the matching Application row
    on success.
 
    Shared by:
      - main.py's _email_queue_worker_loop (polls for QUEUED items on a
        timer)
      - send_email_now below (used by the Apply-to-Requirement flow so
        admin/recruiter/consultant "Apply" actions send immediately instead
        of waiting for the next poll cycle, and so they get a real
        success/failure result back instead of a generic "queued")
 
    Extracted out of main.py's worker loop body so both callers share the
    exact same send/attachment-resolution/Application-upsert logic rather
    than risking two copies drifting apart.
    """
    from gmail_send_service import send_application_email_async, decrypt_token
    from models import EmailQueue, Application
    from datetime import datetime, timezone, timedelta
 
    try:
        import re
        if not item.to_email or not re.match(r"[^@]+@[^@]+\.[^@]+", item.to_email):
            print(f"[email-queue] item {item.id} failed: Invalid to_email '{item.to_email}'")
            item.status = "FAILED"
            item.status_text = f"Invalid to_email '{item.to_email}'"
            await session.commit()
            return
 
        # TESTING GUARD: only send to the internal test domain.
        if not item.to_email.lower().endswith(EMAIL_QUEUE_TEST_DOMAIN_SUFFIX):
            print(f"[email-queue] item {item.id} skipped: '{item.to_email}' is not a test recipient ({EMAIL_QUEUE_TEST_DOMAIN_SUFFIX})")
            item.status = "FAILED"
            item.status_text = "not test domain for now"
            await session.commit()
            return
 
        from gmail_send_service import get_service_account_access_token, decrypt_token
        from models import User, Consultant, ConsultantEmailToken
        import os
 
        access_token = None
 
        # 1. Try Consultant OAuth Token First
        email_tok = None
 
        # First try looking up by the new email_address column
        tok_res = await session.execute(select(ConsultantEmailToken).where(ConsultantEmailToken.email_address == item.from_email))
        email_tok = tok_res.scalars().first()
 
        # Fallback to the old method (User -> Consultant -> Token)
        if not email_tok:
            user_res = await session.execute(select(User).where(User.email == item.from_email))
            from_user = user_res.scalars().first()
            if from_user and from_user.role == "CONSULTANT":
                cons_res = await session.execute(select(Consultant).where(Consultant.user_id == from_user.id))
                cons = cons_res.scalars().first()
                if cons:
                    tok_res = await session.execute(select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == cons.id))
                    email_tok = tok_res.scalars().first()
 
        # --- TEMPORARY FALLBACK FOR ADMIN TESTING ---
        # If the candidate hasn't authorized their token, the admin's test token won't match the candidate's from_email.
        # We fallback to ANY available token, but we MUST rewrite the from_email so Gmail doesn't throw 403/401.
        if not email_tok:
            tok_res = await session.execute(select(ConsultantEmailToken))
            email_tok = tok_res.scalars().first()
            if email_tok and email_tok.email_address:
                print(f"[email-queue] TEST FALLBACK: Rewriting from_email from {item.from_email} to {email_tok.email_address}")
                item.from_email = email_tok.email_address
        # ----------------------------------------------
 
 
        if email_tok and email_tok.access_token_encrypted:
            from datetime import datetime, timezone, timedelta
            import httpx
            from gmail_send_service import encrypt_token
 
            now = datetime.now(timezone.utc)
            # Check if token is expired or about to expire in next 5 mins
            if email_tok.token_expiry and now >= (email_tok.token_expiry - timedelta(minutes=5)):
                if email_tok.refresh_token_encrypted:
                    ref_token = decrypt_token(email_tok.refresh_token_encrypted)
                    client_id = os.getenv("GOOGLE_CLIENT_ID")
                    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
                    if client_id and client_secret:
                        async with httpx.AsyncClient() as client:
                            res = await client.post(
                                "https://oauth2.googleapis.com/token",
                                data={
                                    "client_id": client_id,
                                    "client_secret": client_secret,
                                    "refresh_token": ref_token,
                                    "grant_type": "refresh_token"
                                }
                            )
                            if res.status_code == 200:
                                new_data = res.json()
                                access_token = new_data["access_token"]
                                email_tok.access_token_encrypted = encrypt_token(access_token)
                                if "refresh_token" in new_data:
                                    email_tok.refresh_token_encrypted = encrypt_token(new_data["refresh_token"])
                                email_tok.token_expiry = now + timedelta(seconds=new_data.get("expires_in", 3599))
                                await session.commit()
            else:
                access_token = decrypt_token(email_tok.access_token_encrypted)
 
        # 2. Fallback to Domain Delegation
        if not access_token:
            sa_path = os.path.join(os.path.dirname(__file__), "service-account-key.json")
            access_token = get_service_account_access_token(sa_path, item.from_email)
 
        # BUG FIX: previously built a path under /tmp and
        # handed it straight to send_application_email_async,
        # which builds the MIME message with
        # `if attachment_path and os.path.exists(attachment_path)`
        # — if the file was missing (e.g. /tmp wiped by a
        # restart since upload), that check just silently
        # skipped the attachment. The email still sent,
        # still got marked "Sent successfully", with the
        # resume quietly missing and nothing anywhere to
        # show it. Also only ever used attachments[0],
        # silently dropping any additional files.
        #
        # Now: resolve every attachment (Spaces key first,
        # /tmp fallback for legacy rows), and if ANY
        # attachment the consultant selected can't be found
        # anywhere, FAIL the item instead of sending
        # attachment-less — a failed send with a clear
        # reason is recoverable; a silently incomplete
        # "success" is not.
        attachment_paths = []
        missing_attachments = []
        tmp_cleanup_paths = []
        if item.attachments:
            import os
            import tempfile
            from s3_service import download_file_from_s3
 
            for ref in item.attachments:
                local_candidate = os.path.join("/tmp/email_attachments", ref)
                if ref.startswith(EMAIL_ATTACHMENT_S3_PREFIX):
                    body_bytes, _ = download_file_from_s3(ref)
                    if body_bytes:
                        fd, tmp_path = tempfile.mkstemp(
                            suffix=os.path.splitext(ref)[1] or ".pdf",
                            prefix="email_queue_attach_",
                        )
                        with os.fdopen(fd, "wb") as f:
                            f.write(body_bytes)
                        attachment_paths.append(tmp_path)
                        tmp_cleanup_paths.append(tmp_path)
                    elif os.path.exists(local_candidate):
                        # Spaces fetch failed but the /tmp copy
                        # from this same server session is
                        # still there — use it rather than fail.
                        attachment_paths.append(local_candidate)
                    else:
                        missing_attachments.append(ref)
                elif os.path.exists(local_candidate):
                    attachment_paths.append(local_candidate)
                else:
                    missing_attachments.append(ref)
 
        if missing_attachments:
            item.status = "FAILED"
            item.status_text = (
                f"Attachment(s) no longer available: {', '.join(missing_attachments)}. "
                f"Re-attach the resume and resend."
            )
            await session.commit()
            for p in tmp_cleanup_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
            return
 
        try:
            send_result = await send_application_email_async(
                access_token=access_token,
                from_email=item.from_email,
                to_email=item.to_email,
                cc_email=item.cc_email or "",
                subject=item.subject,
                body=item.content or "",
                attachment_paths=attachment_paths
            )
            item.status = "SENT"
            item.status_text = "Sent successfully"
 
            # BUG FIX (revised): the Application row and
            # match.status="APPLIED" now get set the moment
            # the consultant queues the email (see
            # create_email_queue above) so the dashboard
            # reflects "applied" immediately instead of only
            # after this worker actually sends, up to 60s+
            # later. This block now just updates that existing
            # row to reflect the real outcome, rather than
            # creating it from scratch.
            if item.requirement_id:
                from models import Application
 
                existing_app_result = await session.execute(
                    select(Application).where(
                        Application.consultant_id == item.consultant_id,
                        Application.requirement_id == item.requirement_id,
                    )
                )
                existing_app = existing_app_result.scalars().first()
                now = datetime.now(timezone.utc)
                if existing_app:
                    existing_app.status = "SENT"
                    existing_app.gmail_message_id = send_result.get("gmail_message_id")
                    existing_app.sent_at = now
                    existing_app.applied_at = now
                else:
                    # Shouldn't normally happen — the row is
                    # created at queue time — but don't lose
                    # the send if it's somehow missing.
                    session.add(Application(
                        consultant_id=item.consultant_id,
                        requirement_id=item.requirement_id,
                        status="SENT",
                        vendor_email=item.to_email,
                        cc_email=item.cc_email,
                        gmail_message_id=send_result.get("gmail_message_id"),
                        email_subject=item.subject,
                        sent_at=now,
                        applied_at=now,
                    ))
 
            await session.commit()
        finally:
            for p in tmp_cleanup_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception as e:
        item_id = item.id
        print(f"[email-queue] failed to send item {item_id}: {e}")
        from error_logger import log_db_error
        await log_db_error(stage="email_queue_worker_item", error=e, source_type="email_queue", source_id=item_id)
        await session.rollback()
        # Re-fetch item to update status safely after rollback
        result = await session.execute(select(EmailQueue).where(EmailQueue.id == item_id))
        failed_item = result.scalars().first()
        if failed_item:
            failed_item.status = "FAILED"
            failed_item.status_text = str(e)
            if failed_item.requirement_id:
                from models import Application
                app_result = await session.execute(
                    select(Application).where(
                        Application.consultant_id == failed_item.consultant_id,
                        Application.requirement_id == failed_item.requirement_id,
                    )
                )
                failed_app = app_result.scalars().first()
                if failed_app:
                    failed_app.status = "FAILED"
                    failed_app.error_message = str(e)
            try:
                await session.commit()
            except Exception as inner_e:
                print(f"[email-queue] completely failed to update item {item_id}: {inner_e}")
                await session.rollback()
 
 
@router.post("/api/consultant/email-queue/send-now")
async def send_email_now(
    body: EmailQueueCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Send an application email immediately and record it in `applications`
    on success — used by the Apply-to-Requirement page (admin, recruiter,
    and consultant all share that page) instead of create_email_queue.
 
    BUG FIX: that page previously called create_email_queue for every role
    (admin/recruiter/consultant), which only ever inserts a QUEUED row and
    waits for the background worker's next poll — it never wrote to
    `applications` at all, on success or failure. Every "Apply" from that
    page silently only ever showed up in the Email Queue, never in the
    Applications tracker, no matter who sent it. This endpoint reuses the
    exact same consultant-resolution logic as create_email_queue (copied
    below, not refactored into a shared helper, to avoid risking a change
    in create_email_queue's still-used queue-and-wait behavior for the
    actual Compose pages), then sends immediately via the same
    process_single_email_queue_item used by the background worker, so the
    caller gets a real success/failure result and, on success, a real
    `applications` row — not just "queued".
    """
    from models import EmailQueue, Consultant
    from sqlalchemy import select as sa_select
 
    consultant_id = None
 
    if current_user.role == "ADMIN":
        if body.consultant_id:
            cons_result = await db.execute(
                sa_select(Consultant).where(Consultant.id == body.consultant_id)
            )
            consultant = cons_result.scalars().first()
            if not consultant:
                raise HTTPException(status_code=404, detail="Consultant not found.")
            consultant_id = consultant.id
        else:
            cons_result = await db.execute(
                sa_select(Consultant).where(Consultant.email == current_user.email)
            )
            consultant = cons_result.scalars().first()
            if consultant:
                consultant_id = consultant.id
            else:
                cons_result = await db.execute(
                    sa_select(Consultant).where(Consultant.status == "ACTIVE").limit(1)
                )
                consultant = cons_result.scalars().first()
                if not consultant:
                    raise HTTPException(status_code=400, detail="No consultants found in the system.")
                consultant_id = consultant.id
    elif current_user.role == "RECRUITER":
        if body.consultant_id:
            cons_result = await db.execute(
                sa_select(Consultant).where(Consultant.id == body.consultant_id)
            )
            consultant = cons_result.scalars().first()
            if not consultant:
                raise HTTPException(status_code=404, detail="Consultant not found.")
            consultant_id = consultant.id
        else:
            cons_result = await db.execute(
                sa_select(Consultant).where(Consultant.email == current_user.email)
            )
            consultant = cons_result.scalars().first()
            if consultant:
                consultant_id = consultant.id
            else:
                cons_result = await db.execute(
                    sa_select(Consultant).where(Consultant.status == "ACTIVE").limit(1)
                )
                consultant = cons_result.scalars().first()
                if not consultant:
                    raise HTTPException(status_code=400, detail="No consultants found in the system.")
                consultant_id = consultant.id
    else:
        cons_result = await db.execute(
            sa_select(Consultant).where(Consultant.user_id == current_user.id)
        )
        consultant = cons_result.scalars().first()
        consultant_id = consultant.id if consultant else body.consultant_id
        if not consultant_id:
            raise HTTPException(status_code=400, detail="Consultant profile not found.")
 
    final_cc = body.cc_email.strip() if body.cc_email else ""
    if final_cc:
        if current_user.email not in final_cc:
            final_cc = f"{final_cc},{current_user.email}"
    else:
        final_cc = current_user.email
 
    item = EmailQueue(
        consultant_id=consultant_id,
        requirement_id=body.requirement_id,
        from_email=body.from_email,
        to_email=body.to_email,
        cc_email=final_cc,
        subject=body.subject,
        content=body.content,
        attachments=body.attachments,
        status="QUEUED",
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
 
    await process_single_email_queue_item(db, item)
    await db.refresh(item)
 
    if item.status == "SENT":
        return {"success": True, "id": str(item.id), "status": item.status}
    raise HTTPException(status_code=502, detail=item.status_text or "Failed to send email.")
 
@router.post("/api/consultant/email-queue/upload-attachment")
async def upload_attachment(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Upload attachment file and return a file reference for the email queue.
 
    BUG FIX: this used to save ONLY to /tmp/email_attachments. That
    directory is not durable — it can be (and has been) wiped by a
    server restart or reboot in the window between a consultant
    attaching a resume and the background worker actually sending the
    queued email (up to 60s later, longer if the queue backs up). When
    the file was gone by send time, the app silently sent the email
    with no attachment and marked it "Sent successfully" with no error
    anywhere. Now the file is also uploaded to Spaces (durable,
    survives restarts) and the returned reference points there; the
    /tmp copy is kept only as a same-process fast path.
    """
    ext = os.path.splitext(file.filename)[1]
    unique_name = f"{uuid.uuid4()}{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)
    contents = await file.read()
    with open(file_path, "wb") as f:
        f.write(contents)
 
    from s3_service import upload_file_to_s3
    import io
 
    s3_key = f"{EMAIL_ATTACHMENT_S3_PREFIX}{unique_name}"
    uploaded = upload_file_to_s3(
        io.BytesIO(contents), s3_key, file.content_type or "application/octet-stream"
    )
    stored_reference = s3_key if uploaded else unique_name
    if not uploaded:
        print(f"[email_queue] WARNING: Spaces upload failed for {unique_name} — "
              f"falling back to /tmp only, which is NOT durable across restarts.")
 
    return {
        "success": True,
        "filename": file.filename,
        "stored_name": stored_reference,
        "path": file_path,
        "size_bytes": len(contents),
        "content_type": file.content_type,
    }
 
