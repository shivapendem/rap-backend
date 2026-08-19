# ---------------------------------------------------------------------------
# Email Queue endpoints
# Handles consultant email queue management
# ---------------------------------------------------------------------------
import os
import uuid
import math
import asyncio
from fastapi import UploadFile, File

from typing import Optional, List
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from database import get_db
from auth import get_current_user
from models import User

router = APIRouter()

# How long send_email_now waits for the actual send (token refresh +
# attachment download + Gmail API call) before giving up and returning
# "queued" instead — see the BUG FIX comment at its call site below for
# why this exists. Kept comfortably under common reverse-proxy/Cloudflare
# timeout windows (usually 30-100s) so THIS server is always the one that
# gives up first and returns a clean response, rather than an intermediary
# silently killing the connection with none at all.
SEND_NOW_TIMEOUT_SECONDS = 20.0


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

    @field_validator('from_email', 'to_email', mode='before')
    @classmethod
    def validate_email_format(cls, v: str, info) -> str:
        import re
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{info.field_name} is required and cannot be empty.")
        clean_v = v.strip().lower()
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", clean_v):
            raise ValueError(f"Invalid email address for {info.field_name}: '{v}'")
        return clean_v

    @field_validator('subject', mode='before')
    @classmethod
    def validate_subject(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Subject cannot be empty.")
        return v.strip()


class EmailQueueStatusUpdate(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ANTI_SPAM_DELAY_MINUTES = 5


async def calculate_next_scheduled_at(db: AsyncSession, from_email: str) -> datetime:
    """
    Calculates the next available scheduled_at timestamp for a given from_email.
    Enforces a mandatory 5-minute anti-spam delay between consecutive emails sent from the same sender email.
    Checks both EmailQueue and Application tables for any recent sends/schedules.
    If no prior email was scheduled/sent for this from_email in the last 5 minutes, returns current UTC time.
    Otherwise, returns max_previous_time + 5 minutes.
    """
    from datetime import datetime, timezone, timedelta
    from models import EmailQueue, Application, Consultant
    from sqlalchemy import text
    import hashlib

    norm_from = from_email.strip().lower()

    # Acquire transaction-level advisory lock to serialize concurrent enqueues for the same sender
    email_hash = int(hashlib.sha256(norm_from.encode('utf-8')).hexdigest()[:16], 16)
    if email_hash > 9223372036854775807:
        email_hash = email_hash - 18446744073709551616
    await db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": email_hash})

    now_utc = datetime.now(timezone.utc)

    # 1. Check max scheduled_at or created_at in EmailQueue for this sender (including PROCESSING)
    stmt_eq = (
        select(func.max(func.coalesce(EmailQueue.scheduled_at, EmailQueue.created_at)))
        .where(
            func.lower(EmailQueue.from_email) == norm_from,
            EmailQueue.status.in_(["QUEUED", "SENT", "PROCESSING"])
        )
    )
    res_eq = await db.execute(stmt_eq)
    last_eq_time = res_eq.scalar()

    # 2. Check max sent_at or created_at in Application table for this sender
    stmt_app = (
        select(func.max(func.coalesce(Application.sent_at, Application.created_at)))
        .join(Consultant, Consultant.id == Application.consultant_id)
        .where(
            func.lower(Consultant.email) == norm_from,
            Application.status == "SENT"
        )
    )
    res_app = await db.execute(stmt_app)
    last_app_time = res_app.scalar()

    times = [t for t in (last_eq_time, last_app_time) if t is not None]

    # Base reference floor is (now - 5 minutes)
    reference_time = now_utc - timedelta(minutes=ANTI_SPAM_DELAY_MINUTES)

    if times:
        last_time = max(times)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        reference_time = max(reference_time, last_time)

    return reference_time + timedelta(minutes=ANTI_SPAM_DELAY_MINUTES)


async def reschedule_remaining_queued_emails(db: AsyncSession, from_email: str, actual_sent_at: datetime) -> None:
    """
    Defense-in-depth: Called after an email for from_email is sent.
    Pushes any remaining QUEUED emails for the same from_email to ensure they are at least
    5 minutes after actual_sent_at and spaced 5 minutes apart.
    """
    from datetime import datetime, timezone, timedelta
    from models import EmailQueue

    norm_from = from_email.strip().lower()
    if actual_sent_at.tzinfo is None:
        actual_sent_at = actual_sent_at.replace(tzinfo=timezone.utc)

    stmt = (
        select(EmailQueue)
        .where(
            func.lower(EmailQueue.from_email) == norm_from,
            EmailQueue.status == "QUEUED"
        )
        .order_by(func.coalesce(EmailQueue.scheduled_at, EmailQueue.created_at).asc(), EmailQueue.id.asc())
    )
    res = await db.execute(stmt)
    remaining_items = res.scalars().all()

    current_slot = actual_sent_at + timedelta(minutes=ANTI_SPAM_DELAY_MINUTES)
    for item in remaining_items:
        item_sched = item.scheduled_at or item.created_at
        if item_sched and item_sched.tzinfo is None:
            item_sched = item_sched.replace(tzinfo=timezone.utc)

        if not item_sched or item_sched < current_slot:
            item.scheduled_at = current_slot
            current_slot += timedelta(minutes=ANTI_SPAM_DELAY_MINUTES)
        else:
            current_slot = item_sched + timedelta(minutes=ANTI_SPAM_DELAY_MINUTES)


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
                sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                    Consultant.id == body.consultant_id,
                    Consultant.status == "ACTIVE",
                    User.is_authorized == True
                )
            )
            consultant = cons_result.scalars().first()
            if not consultant:
                raise HTTPException(status_code=404, detail="Consultant not found.")
            consultant_id = consultant.id
        else:
            # Admin didn't provide consultant_id — try to resolve from user's email
            cons_result = await db.execute(
                sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                    Consultant.email == current_user.email,
                    Consultant.status == "ACTIVE",
                    User.is_authorized == True
                )
            )
            consultant = cons_result.scalars().first()
            if consultant:
                consultant_id = consultant.id
            else:
                # BUG FIX ("application for Ram Babu sent out under a
                # different person's name/from-address"): this used to
                # fall back to whichever ACTIVE consultant happened to
                # come back first from an unordered `.limit(1)` query —
                # a completely arbitrary person, unrelated to whoever the
                # admin was actually applying for. Combined with
                # valid_from_emails below always allowing the admin's own
                # email through, an application generated for one
                # candidate could end up queued/sent under the admin's or
                # some other unrelated consultant's identity with no
                # error at any point. A missing consultant_id here means
                # the request itself is incomplete — surface that plainly
                # instead of guessing who to send as.
                raise HTTPException(
                    status_code=400,
                    detail="consultant_id is required to send this email.",
                )
    elif current_user.role == "RECRUITER":
        # Recruiter: same logic — try to resolve or fallback
        if body.consultant_id:
            cons_result = await db.execute(
                sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                    Consultant.id == body.consultant_id,
                    Consultant.status == "ACTIVE",
                    User.is_authorized == True
                )
            )
            consultant = cons_result.scalars().first()
            if not consultant:
                raise HTTPException(status_code=404, detail="Consultant not found.")
            consultant_id = consultant.id
        else:
            cons_result = await db.execute(
                sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                    Consultant.email == current_user.email,
                    Consultant.status == "ACTIVE",
                    User.is_authorized == True
                )
            )
            consultant = cons_result.scalars().first()
            if consultant:
                consultant_id = consultant.id
            else:
                # Same fix as the ADMIN branch above — no more picking an
                # arbitrary active consultant when consultant_id is missing.
                raise HTTPException(
                    status_code=400,
                    detail="consultant_id is required to send this email.",
                )
    else:
        # Consultant: resolve from logged-in user
        cons_result = await db.execute(
            sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                Consultant.user_id == current_user.id,
                Consultant.status == "ACTIVE",
                User.is_authorized == True
            )
        )
        consultant = cons_result.scalars().first()
        consultant_id = consultant.id if consultant else body.consultant_id
        if not consultant_id:
            raise HTTPException(status_code=400, detail="Consultant profile not found.")

    # BUG FIX ("a from address not tied to any real user/consultant row
    # is being used as the sender"): body.from_email was trusted directly
    # from the request payload with no server-side verification at all —
    # the frontend's From field is read-only, but the backend had no
    # defense if that value were ever wrong, stale, or bypassed some
    # other way.
    # TIGHTENED further ("application for Ram Babu sent under a different
    # person's from-address"): this used to always allow current_user's
    # own email through as valid, even when a specific consultant had
    # just been resolved for this exact send — so a request meant to go
    # out as the resolved consultant could still slip through carrying
    # the admin/recruiter's own email as from_email with no error. Once a
    # consultant is resolved, THEIR email is the only legitimate sender —
    # the current user's own email is only accepted when no consultant
    # context exists at all (e.g. a consultant sending for themselves).
    if consultant and consultant.email:
        valid_from_emails = {consultant.email.strip().lower()}
    else:
        valid_from_emails = {(current_user.email or "").strip().lower()}
    if (body.from_email or "").strip().lower() not in valid_from_emails:
        raise HTTPException(
            status_code=400,
            detail="from_email does not match the current user or the resolved consultant — refusing to send.",
        )

    final_cc = current_user.email

    # Resolve the consultant's handling recruiter once — reused below for
    # both the CONSULTANT self-apply case (swap the self-CC for the
    # recruiter, since CC'ing yourself on your own sent mail is a no-op)
    # and the ADMIN-applies-on-behalf case (add the recruiter alongside
    # the admin). Same resolution used for the signature's Employer
    # Details block, so both agree on who "the recruiter" is.
    recruiter_email = None
    if consultant_id and consultant:
        from permission_service import get_handling_recruiter
        handling = await get_handling_recruiter(db, consultant)
        recruiter_email = handling.get("employer_email") if handling else None

    is_self_apply = final_cc.strip() == body.from_email.strip()

    if is_self_apply and recruiter_email:
        # CONSULTANT applying for themselves — CC their recruiter.
        final_cc = recruiter_email
    elif (
        current_user.role in ("ADMIN", "RECRUITER")
        and recruiter_email
        and recruiter_email.strip().lower() not in final_cc.strip().lower()
    ):
        # FEATURE CHANGE: admin or recruiter applying on the consultant's behalf now
        # CCs both the sender AND the consultant's assigned recruiter.
        final_cc = f"{final_cc},{recruiter_email}"
    # RECRUITER applying on behalf: final_cc already equals the
    # recruiter's own email (current_user.email, set by default above) —
    # no extra branch needed here.

    scheduled_at = await calculate_next_scheduled_at(db, body.from_email)

    # Auto-append the sender's signature — custom (from Email Signature
    # settings) if saved, else the default computed contact-card
    # signature — same logic send_email_now uses below. This endpoint
    # (create_email_queue, used by the Compose pages) previously stored
    # body.content completely raw with no signature of any kind ever
    # appended, so a saved custom signature never showed up on emails
    # queued from Compose, only on emails sent via the Apply-to-
    # Requirement flow's send_email_now.
    final_content = body.content or ""
    final_html_content = None
    try:
        from email_template import build_signature_text, build_signature_html, resolve_sender_fields
        from permission_service import get_handling_recruiter
        sender = resolve_sender_fields(current_user, consultant)

        # Employer Details — the recruiter actually handling this
        # consultant, shown as a second block below the consultant's own
        # signature regardless of who is sending. Omitted entirely (empty
        # dict) when the consultant has no assigned recruiter either way.
        employer = await get_handling_recruiter(db, consultant) if consultant else None
        employer = employer or {}

        # Custom signature editor/save removed — always use the default
        # signature card built from the consultant's own profile details
        # (name, title, email, phone, extension, LinkedIn), plus the
        # handling recruiter's Employer Details block.
        signature = build_signature_text(
            sender["sender_name"],
            sender["sender_title"],
            sender["sender_email"],
            sender["sender_direct_number"],
            sender["sender_extension"],
            sender["sender_linkedin_url"],
            employer_name=employer.get("employer_name"),
            employer_title=employer.get("employer_title"),
            employer_email=employer.get("employer_email"),
            employer_phone=employer.get("employer_phone"),
            employer_extension=employer.get("employer_extension"),
        )
        signature_html = build_signature_html(
            sender["sender_name"],
            sender["sender_title"],
            sender["sender_email"],
            sender["sender_direct_number"],
            sender["sender_extension"],
            sender["sender_linkedin_url"],
            employer_name=employer.get("employer_name"),
            employer_title=employer.get("employer_title"),
            employer_email=employer.get("employer_email"),
            employer_phone=employer.get("employer_phone"),
            employer_extension=employer.get("employer_extension"),
            employer_linkedin_url=employer.get("employer_linkedin_url"),
        )

        final_content = f"{body.content.rstrip()}\n\n{signature.strip()}" if (body.content or "").strip() else signature.strip()

        import html as _html
        intro_html = _html.escape(body.content or "").replace("\n", "<br>")
        final_html_content = (
            f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1e293b;">{intro_html}</div>'
            + signature_html
        )
    except Exception as sig_err:
        print(f"[email_queue] signature build failed, sending without one: {sig_err}")
        final_content = body.content or ""
        final_html_content = None

    item = EmailQueue(
        consultant_id=consultant_id,
        requirement_id=body.requirement_id,
        from_email=body.from_email,
        to_email=body.to_email,
        cc_email=final_cc,
        subject=body.subject,
        content=final_content,
        html_content=final_html_content,
        attachments=body.attachments,
        # NOTE: must stay QUEUED (not PROCESSING) — this item is picked up
        # later by the background worker loop (main.py's
        # _email_queue_worker_loop), which only selects/claims rows where
        # status == "QUEUED". Marking it PROCESSING here pre-empts that
        # claim query entirely, so the worker never sees it, never sends
        # it, and it sits stuck until the next server restart (the only
        # thing that resets stray PROCESSING rows back to QUEUED). The
        # "claim immediately" PROCESSING approach belongs to send_email_now,
        # which sends the item itself right away — not to this
        # queue-and-wait endpoint.
        status="QUEUED",
        sent_by_user_id=current_user.id,
        scheduled_at=scheduled_at,
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
            from datetime import datetime, timezone
            db.add(Application(
                consultant_id=consultant_id,
                requirement_id=body.requirement_id,
                status="PENDING",
                vendor_email=body.to_email,
                cc_email=final_cc,
                email_subject=body.subject,
                applied_at=datetime.now(timezone.utc),
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
        "scheduled_at": item.scheduled_at.isoformat() if item.scheduled_at else None,
    }


@router.get("/api/consultant/email-queue")
async def list_email_queue(
    page: int = 1,
    page_size: int = 20,
    consultant_id: Optional[str] = Query(None, description="Comma-separated consultant profile ids to filter by"),
    sent_by_role: Optional[str] = Query(None, description="Comma-separated roles to filter by: ADMIN, RECRUITER, CONSULTANT"),
    search: Optional[str] = Query(None, description="Free-text match against subject, to/from email, and consultant name"),
    status: Optional[str] = Query(None, description="QUEUED, SENT, or FAILED — filters this consultant's own queue view"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all emails in queue."""
    from models import EmailQueue, Consultant, RecruiterConsultant

    query = (
        select(EmailQueue, Consultant, User)
        .outerjoin(Consultant, Consultant.id == EmailQueue.consultant_id)
        .outerjoin(User, User.id == EmailQueue.sent_by_user_id)
    )
    count_query = select(func.count()).select_from(EmailQueue)

    if current_user.role == "CONSULTANT":
        result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        consultant = result.scalars().first()
        if not consultant:
            return {"data": [], "total": 0, "page": page, "page_size": page_size, "pages": 1}
        query = query.where(EmailQueue.consultant_id == consultant.id)
        count_query = count_query.where(EmailQueue.consultant_id == consultant.id)
    elif current_user.role == "RECRUITER":
        # BUG FIX: recruiters saw every consultant's queue items, not just
        # their own assigned ones -- same scoping gap already fixed on the
        # Applications tracker and Pending Matches. Admin still sees all.
        assigned_result = await db.execute(
            select(RecruiterConsultant.consultant_id).where(
                RecruiterConsultant.recruiter_id == current_user.id,
                RecruiterConsultant.is_active == True,
            )
        )
        assigned_ids = [row[0] for row in assigned_result.all()]
        query = query.where(EmailQueue.consultant_id.in_(assigned_ids))
        count_query = count_query.where(EmailQueue.consultant_id.in_(assigned_ids))
    elif current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Optional filters — "Filter by Consultant" and "Filter by Sent By Role"
    # dropdowns on the Global Email Queue screen (admin/recruiter views only;
    # a single consultant's own view has nothing meaningful to filter by).
    # For recruiters this narrows further within the assigned_ids scoping
    # already applied above (the two .where() clauses AND together), so a
    # recruiter can never widen their view to a consultant outside their
    # own roster just by passing an arbitrary consultant_id.
    if consultant_id:
        try:
            ids = [int(cid) for cid in consultant_id.split(",") if cid.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid consultant_id filter")
        if ids:
            query = query.where(EmailQueue.consultant_id.in_(ids))
            count_query = count_query.where(EmailQueue.consultant_id.in_(ids))

    # NOTE: de-duplicated — this block was accidentally duplicated
    # verbatim (copy-paste) in an earlier revision, which was harmless
    # (the second application of the same .where() clause is a no-op)
    # but confusing and worth cleaning up.
    if status:
        valid_statuses = {"QUEUED", "SENT", "FAILED"}
        status_upper = status.strip().upper()
        if status_upper not in valid_statuses:
            raise HTTPException(status_code=422, detail=f"status must be one of {sorted(valid_statuses)}")
        query = query.where(EmailQueue.status == status_upper)
        count_query = count_query.where(EmailQueue.status == status_upper)

    if sent_by_role:
        roles = [r.strip().upper() for r in sent_by_role.split(",") if r.strip()]
        valid_roles = {"ADMIN", "RECRUITER", "CONSULTANT"}
        invalid = [r for r in roles if r not in valid_roles]
        if invalid:
            raise HTTPException(status_code=422, detail=f"Invalid sent_by_role values: {invalid}")
        if roles:
            query = query.where(EmailQueue.sent_by_user_id.in_(
                select(User.id).where(User.role.in_(roles))
            ))
            count_query = count_query.where(EmailQueue.sent_by_user_id.in_(
                select(User.id).where(User.role.in_(roles))
            ))

    if search:
        term = f"%{search.strip()}%"
        search_clause = or_(
            EmailQueue.subject.ilike(term),
            EmailQueue.to_email.ilike(term),
            EmailQueue.from_email.ilike(term),
            EmailQueue.consultant_id.in_(
                select(Consultant.id).where(Consultant.full_name.ilike(term))
            ),
        )
        query = query.where(search_clause)
        count_query = count_query.where(search_clause)

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
    rows = result.all()
    return {
        "data": [
            {
                "id": str(item.id),
                "consultant_id": str(item.consultant_id),
                "consultant_name": cons.full_name if cons else str(item.consultant_id),
                "requirement_id": str(item.requirement_id) if item.requirement_id else None,
                "from_email": item.from_email,
                "to_email": item.to_email,
                "cc_email": item.cc_email,
                "subject": item.subject,
                "content": item.content,
                "attachments": item.attachments,
                "status": item.status,
                "status_message": item.status_text,
                "sent_by_name": sender.full_name if sender else None,
                "sent_by_role": sender.role if sender else None,
                "scheduled_at": item.scheduled_at.isoformat() if item.scheduled_at else None,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            }
            for item, cons, sender in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 1,
    }


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
    from models import EmailQueue, Consultant, User
    from sqlalchemy import select as sa_select

    consultant_id = None

    if current_user.role == "ADMIN":
        if body.consultant_id:
            cons_result = await db.execute(
                sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                    Consultant.id == body.consultant_id,
                    Consultant.status == "ACTIVE",
                    User.is_authorized == True
                )
            )
            consultant = cons_result.scalars().first()
            if not consultant:
                raise HTTPException(status_code=404, detail="Consultant not found.")
            consultant_id = consultant.id
        else:
            cons_result = await db.execute(
                sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                    Consultant.email == current_user.email,
                    Consultant.status == "ACTIVE",
                    User.is_authorized == True
                )
            )
            consultant = cons_result.scalars().first()
            if consultant:
                consultant_id = consultant.id
            else:
                # BUG FIX ("application for Ram Babu sent out under a
                # different person's name/from-address") — same fix as
                # create_email_queue above: no more silently picking an
                # arbitrary ACTIVE consultant via an unordered `.limit(1)`
                # when consultant_id is missing. That person had nothing
                # to do with who this email was actually meant for.
                raise HTTPException(
                    status_code=400,
                    detail="consultant_id is required to send this email.",
                )
    elif current_user.role == "RECRUITER":
        if body.consultant_id:
            cons_result = await db.execute(
                sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                    Consultant.id == body.consultant_id,
                    Consultant.status == "ACTIVE",
                    User.is_authorized == True
                )
            )
            consultant = cons_result.scalars().first()
            if not consultant:
                raise HTTPException(status_code=404, detail="Consultant not found.")
            consultant_id = consultant.id
        else:
            cons_result = await db.execute(
                sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                    Consultant.email == current_user.email,
                    Consultant.status == "ACTIVE",
                    User.is_authorized == True
                )
            )
            consultant = cons_result.scalars().first()
            if consultant:
                consultant_id = consultant.id
            else:
                # Same fix as the ADMIN branch above.
                raise HTTPException(
                    status_code=400,
                    detail="consultant_id is required to send this email.",
                )
    else:
        cons_result = await db.execute(
            sa_select(Consultant).join(User, User.id == Consultant.user_id).where(
                Consultant.user_id == current_user.id,
                Consultant.status == "ACTIVE",
                User.is_authorized == True
            )
        )
        consultant = cons_result.scalars().first()
        consultant_id = consultant.id if consultant else body.consultant_id
        if not consultant_id:
            raise HTTPException(status_code=400, detail="Consultant profile not found.")

    # BUG FIX (the actual thing blocking sends): once a consultant is
    # resolved, THEIR email used to be the ONLY accepted from_email — full
    # stop. If that consultant hadn't connected Gmail yet (no OAuth token
    # on file), every application on their behalf was permanently stuck:
    # "No OAuth token found for candidate/consultant and not a Savantis
    # sender", with no way to actually get the email out until the
    # consultant went and connected Gmail themselves. An admin/recruiter
    # standing right here trying to send it for them had no path forward.
    #
    # FEATURE: for ADMIN/RECRUITER only (never for a CONSULTANT applying
    # for themselves — they should still connect their own Gmail, this
    # isn't a way around that), fall back to sending via the admin/
    # recruiter's OWN mailbox when the resolved consultant has no Gmail
    # connected. process_single_email_queue_item already sends any
    # "savantis" address through the domain-delegated service account
    # with no OAuth token required, so this fallback "just works" for any
    # admin/recruiter on a real @savantis.com account — no new sending
    # infrastructure needed.
    #
    # This is a real, visible trade-off, not a bug: the vendor will see
    # the admin/recruiter's address as the sender for this specific send,
    # not the consultant's. effective_from_email (not body.from_email) is
    # used for every downstream use of "who is actually sending this" —
    # validation, anti-spam scheduling, and the EmailQueue row itself —
    # so the record accurately reflects who really sent it.
    # fallback_sender_used logic removed per user request: from should ALWAYS be the applicant email address.
    effective_from_email = body.from_email

    # BUG FIX ("a from address not tied to any real user/consultant row
    # is being used as the sender"): body.from_email was trusted directly
    # from the request payload with no server-side verification at all —
    # the frontend's From field is read-only, but the backend had no
    # defense if that value were ever wrong, stale, or bypassed some
    # other way.
    # TIGHTENED further ("application for Ram Babu sent under a different
    # person's from-address"): this used to always allow current_user's
    # own email through as valid, even when a specific consultant had
    # just been resolved for this exact send — so a request meant to go
    # out as the resolved consultant could still slip through carrying
    # the admin/recruiter's own email as from_email with no error. Once a
    # consultant is resolved, THEIR email is the only legitimate sender —
    # the current user's own email is only accepted when no consultant
    # context exists at all (e.g. a consultant sending for themselves).
    if consultant and consultant.email:
        valid_from_emails = {consultant.email.strip().lower()}
    else:
        valid_from_emails = {(current_user.email or "").strip().lower()}
    if effective_from_email.strip().lower() not in valid_from_emails:
        raise HTTPException(
            status_code=400,
            detail="from_email does not match the current user or the resolved consultant — refusing to send.",
        )

    final_cc = body.cc_email.strip() if body.cc_email else ""
    if final_cc:
        if current_user.email not in final_cc:
            final_cc = f"{final_cc},{current_user.email}"
    else:
        final_cc = current_user.email

    # Resolve the consultant's handling recruiter once — reused below for
    # both the CONSULTANT self-apply case (swap the self-CC for the
    # recruiter, since CC'ing yourself on your own sent mail is a no-op)
    # and the ADMIN-applies-on-behalf case (add the recruiter alongside
    # the admin). Same resolution used for the signature's Employer
    # Details block, so both agree on who "the recruiter" is.
    recruiter_email = None
    if consultant_id and consultant:
        from permission_service import get_handling_recruiter
        handling = await get_handling_recruiter(db, consultant)
        recruiter_email = handling.get("employer_email") if handling else None

    is_self_apply = final_cc.strip() == effective_from_email.strip()

    if is_self_apply and recruiter_email:
        # CONSULTANT applying for themselves — CC their recruiter.
        final_cc = recruiter_email
    elif (
        current_user.role in ("ADMIN", "RECRUITER")
        and recruiter_email
        and recruiter_email.strip().lower() not in final_cc.strip().lower()
    ):
        # FEATURE CHANGE: admin or recruiter applying on the consultant's behalf now
        # CCs both the sender AND the consultant's assigned recruiter.
        final_cc = f"{final_cc},{recruiter_email}"
    # RECRUITER applying on behalf: final_cc already equals the
    # recruiter's own email (current_user.email, set by default above) —
    # no extra branch needed here.

    # Auto-append the sender's contact-card signature (name, title/
    # designation, LinkedIn, email/mobile/office-extension, address) after
    # whatever the admin/recruiter/consultant typed — same sender identity
    # logic phase7.py's older preview/confirm-send endpoints use, shared
    # via email_template.resolve_sender_fields. This is the ACTUAL signature
    # that goes out on every real Apply-button send, since this endpoint
    # (not phase7.py's) is what those buttons hit.
    #
    # Also builds the matching HTML version (signature card + real company
    # banner image, embedded inline via Content-ID at send time — see
    # gmail_send_service.build_mime_message) and stores it in the new
    # html_content column, so process_single_email_queue_item can send a
    # proper multipart/alternative message later exactly as composed here,
    # without re-deriving the sender's identity at actual send time.
    #
    # BUG FIX: wrapped in try/except — any failure while building the
    # signature/HTML (e.g. models.py's new columns not migrated on this
    # database yet, or a bad field on the current_user row) used to take
    # the whole send down with it, surfacing as this endpoint's generic
    # 502 with no clue that "signature building" was the actual failure
    # point. Now falls back to plain content with no signature at all
    # rather than blocking the send — sending the application is more
    # important than the signature being present.
    final_content = body.content or ""
    final_html_content = None
    try:
        from email_template import build_signature_text, build_signature_html, resolve_sender_fields
        from permission_service import get_handling_recruiter
        sender = resolve_sender_fields(current_user, consultant)

        # Employer Details — the recruiter actually handling this
        # consultant, shown as a second block below the consultant's own
        # signature regardless of who is sending. Omitted entirely (empty
        # dict) when the consultant has no assigned recruiter either way.
        employer = await get_handling_recruiter(db, consultant) if consultant else None
        employer = employer or {}

        # Custom signature editor/save removed — always use the default
        # signature card built from the consultant's own profile details
        # (name, title, email, phone, extension, LinkedIn), plus the
        # handling recruiter's Employer Details block.
        signature = build_signature_text(
            sender["sender_name"],
            sender["sender_title"],
            sender["sender_email"],
            sender["sender_direct_number"],
            sender["sender_extension"],
            sender["sender_linkedin_url"],
            employer_name=employer.get("employer_name"),
            employer_title=employer.get("employer_title"),
            employer_email=employer.get("employer_email"),
            employer_phone=employer.get("employer_phone"),
            employer_extension=employer.get("employer_extension"),
        )
        signature_html = build_signature_html(
            sender["sender_name"],
            sender["sender_title"],
            sender["sender_email"],
            sender["sender_direct_number"],
            sender["sender_extension"],
            sender["sender_linkedin_url"],
            employer_name=employer.get("employer_name"),
            employer_title=employer.get("employer_title"),
            employer_email=employer.get("employer_email"),
            employer_phone=employer.get("employer_phone"),
            employer_extension=employer.get("employer_extension"),
            employer_linkedin_url=employer.get("employer_linkedin_url"),
        )

        final_content = f"{body.content.rstrip()}\n\n{signature.strip()}" if (body.content or "").strip() else signature.strip()

        import html as _html
        intro_html = _html.escape(body.content or "").replace("\n", "<br>")
        final_html_content = (
            f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1e293b;">{intro_html}</div>'
            + signature_html
        )
    except Exception as sig_err:
        print(f"[email_queue] signature build failed, sending without one: {sig_err}")
        final_content = body.content or ""
        final_html_content = None

    from datetime import datetime, timezone
    scheduled_at = await calculate_next_scheduled_at(db, effective_from_email)
    now_utc = datetime.now(timezone.utc)

    # BUG FIX ("Failed to send application email." with literally nothing
    # else to go on): calculate_next_scheduled_at enforces a mandatory
    # ANTI_SPAM_DELAY_MINUTES gap between sends from the same from_email —
    # correct and needed for the deferred/background-queue flow, but this
    # is send_email_now, which exists specifically to send RIGHT NOW and
    # give the caller a real result (see this function's own docstring
    # further down). Reusing the same staggering function here meant that
    # sending twice from the same address within the cooldown window
    # silently created a QUEUED item scheduled minutes in the future, and
    # process_single_email_queue_item's own SCHEDULE TIME GUARD then
    # bailed out instantly and silently — before printing a single debug
    # line, before setting any status_text — leaving item.status at its
    # default and this function's `item.status_text or "Failed to send
    # application email."` fallback as the ONLY thing the caller ever
    # saw. Fail fast here instead, with the real reason and exactly how
    # long is left, rather than creating an item that was doomed to this
    # same silent failure the moment it was written to the DB.
    if scheduled_at > now_utc:
        wait_seconds = (scheduled_at - now_utc).total_seconds()
        wait_minutes = max(1, round(wait_seconds / 60))
        raise HTTPException(
            status_code=429,
            detail=(
                f"To prevent spam, only one email can be sent from {effective_from_email} "
                f"every {ANTI_SPAM_DELAY_MINUTES} minutes. Please wait about "
                f"{wait_minutes} more minute{'s' if wait_minutes != 1 else ''} and try again."
            ),
        )

    item = EmailQueue(
        consultant_id=consultant_id,
        requirement_id=body.requirement_id,
        from_email=effective_from_email,
        to_email=body.to_email,
        cc_email=final_cc,
        subject=body.subject,
        content=final_content,
        html_content=final_html_content,
        attachments=body.attachments,
        status="QUEUED",
        sent_by_user_id=current_user.id,
        scheduled_at=scheduled_at,
    )
    db.add(item)

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
            from datetime import datetime, timezone
            db.add(Application(
                consultant_id=consultant_id,
                requirement_id=body.requirement_id,
                status="PENDING",
                vendor_email=body.to_email,
                cc_email=final_cc,
                email_subject=body.subject,
                applied_at=datetime.now(timezone.utc),
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

    # BUG FIX: despite this function's docstring claiming it "sends
    # immediately via process_single_email_queue_item", the code never
    # actually called it — it only ever inserted a QUEUED row and
    # returned success, leaving the real send to the background
    # worker's next poll (main.py's _email_queue_worker_loop, on its
    # own timer). Every "Apply Now" (admin Pending Applications,
    # recruiter, and consultant) reported success immediately, but the
    # email only went out once/if that later poll picked the item up —
    # and the Application row's resume_attachment_path is only ever set
    # inside process_single_email_queue_item's own SENT-path upsert
    # (further below in this file), so until that poll actually ran,
    # the Applications Tracker showed the application with no resume
    # attached and no email actually sent, exactly matching this
    # endpoint's own docstring promise that it never kept. Now sends
    # synchronously, right here, the same way phase5.py's consultant
    # apply endpoint already does, and reports the real outcome instead
    # of a blind "queued".
    await process_single_email_queue_item(db, item)
    await db.refresh(item)

    if item.status != "SENT":
        # BUG FIX: this used to raise 502. 502 is a gateway/proxy status —
        # it tells Cloudflare (sitting in front of this API) "the origin
        # is malfunctioning", so Cloudflare swallows the real body/detail
        # here and replaces the whole response with its own generic
        # "origin returned an invalid or incomplete response" interstitial.
        # The actual reason (e.g. "No OAuth token found for candidate/
        # consultant and not a Savantis sender" — a completely ordinary,
        # user-actionable failure, not a server malfunction) never made it
        # to the browser at all. 422 is a normal client-facing error
        # Cloudflare passes straight through untouched, so the frontend
        # can actually surface item.status_text to the user.
        raise HTTPException(
            status_code=422,
            detail=item.status_text or "Failed to send application email.",
        )

    return {
        "success": True,
        "id": str(item.id),
        "status": item.status,
        "scheduled_at": (item.scheduled_at or scheduled_at or now_utc).isoformat(),
        "message": "Email sent successfully.",
        # Lets the frontend show a clear "sent as you, not as the
        # consultant" notice instead of the send silently looking
        # identical to a normal consultant-sent application.
        "sent_from": effective_from_email,
        "fallback_sender_used": False,
    }

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
    safe_original = sanitize_attachment_filename(file.filename)
    unique_name = f"{uuid.uuid4()}__{safe_original}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)
    contents = await file.read()
    with open(file_path, "wb") as f:
        f.write(contents)

    from s3_service import upload_file_to_s3
    import io
    import asyncio

    s3_key = f"{EMAIL_ATTACHMENT_S3_PREFIX}{unique_name}"
    # PERFORMANCE: upload_file_to_s3 is a blocking/synchronous call; running
    # it directly inside this async endpoint blocks the whole event loop
    # for the duration of the upload. Offload to a worker thread instead.
    uploaded = await asyncio.to_thread(
        upload_file_to_s3, io.BytesIO(contents), s3_key, file.content_type or "application/octet-stream"
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


@router.get("/api/consultant/email-queue/download-attachment")
async def download_queue_attachment(
    ref: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """
    Get a downloadable URL or serve attachment file for email queue item.

    Serves the real attachment file as-is (DOCX stays DOCX — no
    on-demand PDF conversion). Used by EmailDetailModal,
    ApplicationEmailPreviewModal, EmailPreviewModal, and the Email Queue
    list pages.

    BUG FIX ("view directly downloads instead of previewing" for local
    attachments): this endpoint is view-only for every caller (none of
    it is a "download the original file" button), and the frontend's
    openViewableAttachment only gets an actual in-browser preview for a
    non-natively-viewable type like DOCX when it receives the
    {url, filename, mimeType} JSON wrapper — that URL is a real, publicly
    fetchable presigned link it can hand to Google's Docs Viewer, whose
    own servers fetch it (browser CORS never applies there). A local
    (non-Spaces) attachment used to skip straight to FileResponse —
    real file bytes with no external-viewer routing at all — and a
    browser has no built-in renderer for DOCX regardless of headers, so
    "viewing" one just triggered a save/open-with-Word download prompt.
    Upload the local bytes to Spaces on the fly (mirroring the same
    self-heal-by-uploading pattern used elsewhere in this codebase) so
    every attachment, local or not, gets a real presigned URL and goes
    through the same in-browser preview path.
    """
    from s3_service import generate_presigned_url, download_file_from_s3, upload_file_to_s3
    import io

    clean_ref = ref.strip()
    if not clean_ref:
        raise HTTPException(status_code=400, detail="ref is required")

    import mimetypes as _mimetypes

    # 1. Try Spaces S3 presigned URL directly if ref is an S3 key
    if clean_ref.startswith(EMAIL_ATTACHMENT_S3_PREFIX) or clean_ref.startswith("resumes/") or "/" in clean_ref:
        url = generate_presigned_url(clean_ref)
        if url:
            basename = os.path.basename(clean_ref)
            mime_type = _mimetypes.guess_type(basename)[0] or "application/octet-stream"
            return {"url": url, "filename": basename, "mimeType": mime_type}

    # 2. Try local file path if present — upload to Spaces on the fly so
    # it gets a real presigned URL (see BUG FIX above) instead of being
    # served as raw bytes with no viewer routing.
    filename = os.path.basename(clean_ref)
    local_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(local_path):
        mime_type = _mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(local_path, "rb") as f:
            local_bytes = f.read()
        temp_s3_key = f"{EMAIL_ATTACHMENT_S3_PREFIX}view-temp/{uuid.uuid4().hex}/{filename}"
        uploaded = await asyncio.to_thread(upload_file_to_s3, io.BytesIO(local_bytes), temp_s3_key, mime_type)
        if uploaded:
            url = generate_presigned_url(temp_s3_key)
            if url:
                return {"url": url, "filename": filename, "mimeType": mime_type}
        # Upload failed for some reason — fall back to serving the raw
        # file directly so viewing still degrades to a download rather
        # than breaking outright.
        from fastapi.responses import FileResponse
        return FileResponse(local_path, filename=filename, media_type=mime_type)

    # 3. Try fallback with S3 prefix
    s3_key = f"{EMAIL_ATTACHMENT_S3_PREFIX}{filename}"
    url = generate_presigned_url(s3_key)
    if url:
        mime_type = _mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return {"url": url, "filename": filename, "mimeType": mime_type}

    raise HTTPException(status_code=404, detail="Attachment file not found.")


# ---------------------------------------------------------------------------
# BUG FIX (routing): these three routes take a bare numeric id
# (/{item_id}), but were previously registered ahead of literal-path
# routes like /upload-attachment, /download-attachment, and /send-now.
# Starlette matches routes by path *shape* in registration order before
# FastAPI validates each path parameter's type — with no type converter,
# "/{item_id}" matches ANY single path segment string, including
# "download-attachment". That meant GET
# /api/consultant/email-queue/download-attachment was being captured by
# get_email_queue_item first, and failed with a 422 (int conversion
# failure on "download-attachment") before ever reaching the real
# endpoint below. Fixed two ways together: the literal-path routes above
# are now registered first, and `{item_id:int}` makes the path itself
# only match numeric segments, so non-numeric literal paths never route
# here regardless of declaration order.
# ---------------------------------------------------------------------------

@router.get("/api/consultant/email-queue/{item_id:int}")
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


@router.patch("/api/consultant/email-queue/{item_id:int}/status")
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


@router.delete("/api/consultant/email-queue/{item_id:int}")
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

# BUG FIX: attachment refs were stored as bare "<uuid><ext>" with the
# person's original filename discarded entirely at upload time (see
# upload_attachment below) — nothing anywhere recorded it, so every screen
# that later displays or downloads the attachment (Email Preview modal,
# resume-download endpoint) had no choice but to show the meaningless
# UUID/S3-key as the "filename". New uploads now embed the sanitized
# original name in the stored key itself ("<uuid>__<original-name>"), and
# original_filename_from_ref() strips the UUID prefix back off for display.
# Refs uploaded before this change has no original name recorded anywhere
# and will still show as their raw basename — that's unrecoverable without
# re-uploading, but everything sent from here on will show correctly.
import re as _re

_ATTACHMENT_UUID_PREFIX_RE = _re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}__'
)


def sanitize_attachment_filename(name: str) -> str:
    """Strip path components and characters unsafe in an S3 key / HTTP
    Content-Disposition header, while keeping the name human-readable."""
    name = os.path.basename(name or "").strip()
    name = _re.sub(r'[\\/:*?"<>|\r\n]', "_", name)
    return name[:150] or "attachment"


def original_filename_from_ref(ref: str) -> str:
    """Recover the human-readable filename from a stored attachment
    reference. See note above — only works for refs uploaded after this
    fix; older ones fall back to their raw basename, same as before."""
    base = os.path.basename(ref or "")
    return _ATTACHMENT_UUID_PREFIX_RE.sub("", base) or base

# TESTING GUARD: while we validate the email queue pipeline, only allow sends
# to this domain. Lives here (not main.py) so both the background worker and
# the send-now endpoint below read the exact same value.
#
# BUG FIX (well, guard fix): this was hardcoded to "@savantisintelli.com"
# with no way to turn it off short of a code change — and it was silently
# rejecting every real vendor/client send (decisionsix.com, insyncstaffing.
# com, itech-us.com, etc.) the whole time. It went unnoticed because the
# send-now endpoint used to just queue and report fake success without ever
# actually running this check in real time (see the earlier BUG FIX on
# send_email_now below); once that was fixed to send synchronously, this
# guard's rejection became immediately visible as "Failed to send
# application email." Now reads from an env var and is OFF by default (real
# sends to any address allowed) — set EMAIL_QUEUE_TEST_DOMAIN_SUFFIX in the
# environment (e.g. to "@savantisintelli.com") to restrict sends again for
# a staging pipeline test, without touching code.
EMAIL_QUEUE_TEST_DOMAIN_SUFFIX = os.getenv("EMAIL_QUEUE_TEST_DOMAIN_SUFFIX", "")

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
    # BUG FIX: these imports used to sit outside the try block below — if
    # ANY of them failed (a broken import in gmail_send_service.py, a
    # missing dependency, etc.), the exception propagated out of this
    # function before a single print() ran and before the except block
    # below ever got a chance to record a real status_text. The caller
    # (send_email_now) saw item.status still at its default "QUEUED" and
    # fell back to a completely generic "Failed to send application
    # email." with nothing in the backend terminal to explain why — the
    # worst possible failure mode to debug. Moved inside try/except so an
    # import failure here now behaves exactly like any other failure:
    # logged with a full traceback and a real status_text.
    try:
        from gmail_send_service import send_application_email_async, decrypt_token
        from models import EmailQueue, Application
        from datetime import datetime, timezone, timedelta
    except Exception as import_err:
        import traceback
        print(f"[email-queue debug {item.id}] Failed to import send dependencies: {import_err}\nTraceback:\n{traceback.format_exc()}")
        from error_logger import log_db_error
        await log_db_error(stage="email_queue_worker_item_import", error=import_err, source_type="email_queue", source_id=item.id)
        item.status = "FAILED"
        item.status_text = f"{type(import_err).__name__}: {import_err}" or f"{type(import_err).__name__} (no error message)"
        await session.commit()
        return

    try:
        # SCHEDULE TIME GUARD: ensure email is only triggered when scheduled_at <= now()
        now_check = datetime.now(timezone.utc)
        item_sched = item.scheduled_at
        if item_sched:
            if item_sched.tzinfo is None:
                item_sched = item_sched.replace(tzinfo=timezone.utc)
            if item_sched > now_check:
                print(f"[email-queue] item {item.id} skipped: scheduled_at ({item_sched.isoformat()}) is in the future")
                return

        print(f"[email-queue debug {item.id}] Starting send processing for from_email={item.from_email} to_email={item.to_email}", flush=True)

        async def mark_app_failed(err_msg: str):
            if item.requirement_id:
                app_res = await session.execute(
                    select(Application).where(
                        Application.consultant_id == item.consultant_id,
                        Application.requirement_id == item.requirement_id,
                    )
                )
                app = app_res.scalars().first()
                if app:
                    app.status = "FAILED"
                    app.error_message = err_msg

        import re
        if not item.to_email or not re.match(r"[^@]+@[^@]+\.[^@]+", item.to_email):
            print(f"[email-queue] item {item.id} failed: Invalid to_email '{item.to_email}'")
            item.status = "FAILED"
            item.status_text = f"Invalid to_email '{item.to_email}'"
            await mark_app_failed(item.status_text)
            await session.commit()
            return

        # TESTING GUARD / Per-user override: only enforced when
        # EMAIL_QUEUE_TEST_DOMAIN_SUFFIX is actually set (see the
        # module-level guard-fix comment above) — off by default, so real
        # sends to any address go through normally. A user with
        # allowed_to_send=True bypasses this even when it IS set, letting
        # admin roll out real sending to individual users first.
        from models import User, Consultant, ConsultantEmailToken

        sender_allowed = False
        if item.sent_by_user_id:
            sender_result = await session.execute(
                select(User.allowed_to_send).where(User.id == item.sent_by_user_id)
            )
            sender_allowed = sender_result.scalar_one_or_none() or False

        if (
            EMAIL_QUEUE_TEST_DOMAIN_SUFFIX
            and not sender_allowed
            and not item.to_email.lower().endswith(EMAIL_QUEUE_TEST_DOMAIN_SUFFIX)
        ):
            print(f"[email-queue] item {item.id} skipped: '{item.to_email}' is not a test recipient ({EMAIL_QUEUE_TEST_DOMAIN_SUFFIX})")
            item.status = "FAILED"
            item.status_text = "not test domain for now"
            await mark_app_failed(item.status_text)
            await session.commit()
            return

        print(f"[email-queue debug {item.id}] Passed testing guards. Resolving token...")

        from gmail_send_service import get_service_account_access_token, decrypt_token

        access_token = None

        # BUG FIX ("No OAuth token found ... and not a Savantis sender" —
        # blocked ANY non-@savantis address from sending, even when that
        # consultant had genuinely connected their own Gmail account):
        # this used to gate the two send methods by a hardcoded
        # "savantis" substring check on item.from_email — service-account
        # domain delegation ONLY for savantis addresses, OAuth-token
        # lookup ONLY for everything else, with no fallback either way.
        # That's backwards from how confirm_send (phase7.py) already
        # handles the exact same choice: try the consultant's own
        # OAuth-connected token first (the correct "send as" mechanism
        # for any address, not just @savantis ones), and only fall back
        # to service-account delegation if no token exists. Removed the
        # domain gate entirely — every sender now gets both attempts, in
        # that order. Google's own domain-wide-delegation JWT (sub:
        # impersonate_email, checked against the service account's
        # authorized Workspace domain) already rejects impersonation for
        # any address outside that Workspace on its own, so this doesn't
        # let anything through that Google wouldn't otherwise allow — it
        # just stops OUR code from pre-blocking legitimate senders (any
        # Workspace-domain address, not only ones with "savantis"
        # literally in them, plus any consultant who's connected their
        # own Gmail) before Google ever gets a say.
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
                    candidate_tok = tok_res.scalars().first()
                    # BUG FIX ("email sent from an unrelated staff
                    # member's real Gmail account instead of the
                    # intended consultant" — e.g. Ram Babu's
                    # application arriving from Jashwanth's inbox):
                    # this fallback used to trust whatever token row
                    # was linked to consultant_id with no check that
                    # its own email_address actually matches
                    # item.from_email. A mislinked/corrupted token row
                    # (saved under the wrong consultant_id at
                    # OAuth-connect time) was silently accepted and
                    # used to authenticate the send — Gmail then sends
                    # as whoever that token's real account is,
                    # regardless of the requested From header, since
                    # it has no "send as" alias for item.from_email.
                    # Only use this fallback token when its own
                    # email_address genuinely matches (or is unset,
                    # for older rows saved before this column
                    # existed) — otherwise treat it the same as "no
                    # token found" rather than sending under the
                    # wrong identity.
                    if candidate_tok and (
                        not candidate_tok.email_address
                        or candidate_tok.email_address.strip().lower() == (item.from_email or "").strip().lower()
                    ):
                        email_tok = candidate_tok
                    elif candidate_tok:
                        print(
                            f"[email-queue debug {item.id}] Refusing mismatched token: "
                            f"consultant_id={cons.id} token.email_address={candidate_tok.email_address!r} "
                            f"but item.from_email={item.from_email!r}"
                        )

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
                        # BUG FIX (Cloudflare "invalid or incomplete
                        # response" / origin timeout): this had no
                        # explicit timeout at all, relying on httpx's
                        # default — under load, or if Google's token
                        # endpoint is slow, this call (plus the PDF
                        # conversion and Gmail API call that follow it,
                        # all now synchronous within a single request —
                        # see the BUG FIX above this function) could run
                        # long enough to blow past the proxy's timeout,
                        # which drops the connection before this server
                        # ever sends a response. A short, explicit
                        # timeout means a hung/slow OAuth call fails
                        # fast and cleanly instead.
                        async with httpx.AsyncClient(timeout=15.0) as client:
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
                                # BUG FIX ("reactivated the user, then
                                # applying auto-deactivates them again"):
                                # this ValueError's message is what the
                                # auto-deauthorize check further below
                                # matches against ("invalid_grant",
                                # "unauthorized", "401", etc.) — a
                                # revoked/expired refresh token (which
                                # can happen while a user sits
                                # deactivated for a while, independent
                                # of the User Management authorized/
                                # unauthorized flag) reliably reproduces
                                # Google's real "invalid_grant" error
                                # here. Re-authorizing the USER record
                                # doesn't reconnect Gmail, so every
                                # subsequent apply attempt hits this
                                # same dead token and gets deauthorized
                                # again — an admin needs a message that
                                # actually says so, not a bare status
                                # code and response body.
                                raise ValueError(
                                    f"Gmail authorization has expired or was revoked for this consultant "
                                    f"(refresh_token rejected: status={res.status_code} body={res.text}). "
                                    f"Reactivating the user account alone will not fix this — the consultant "
                                    f"needs to reconnect their Gmail account before applying again."
                                )
            else:
                access_token = decrypt_token(email_tok.access_token_encrypted)

        if not access_token:
            # No per-consultant OAuth token (or it had no send scope) —
            # fall back to service-account domain delegation for ANY
            # sender, not just @savantis addresses. This only actually
            # succeeds for an address within our verified Google
            # Workspace domain (Google's own token endpoint enforces
            # that via the JWT "sub" claim), so a genuinely external
            # personal address still fails here exactly as it should —
            # it just now gets Google's real rejection reason instead of
            # being blocked pre-emptively by our own narrow domain check.
            try:
                sa_path = os.path.join(os.path.dirname(__file__), "service-account-key.json")
                access_token = await asyncio.to_thread(get_service_account_access_token, sa_path, item.from_email)
            except Exception as sa_exc:
                print(f"[email-queue debug {item.id}] Service-account fallback failed for {item.from_email!r}: {sa_exc}")

        if not access_token:
            raise ValueError(
                f"No OAuth token found for candidate/consultant ({item.from_email}), and this address "
                f"isn't part of the company's Google Workspace domain either, so no automatic send method "
                f"is available. The consultant needs to connect their Gmail account before applying."
            )

        print(f"[email-queue debug {item.id}] Token resolved successfully. Resolving attachments...")

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
        #
        # BUG FIX (filename): a Spaces attachment is downloaded to a
        # tempfile.mkstemp() path (e.g. "/tmp/email_queue_attach_8rjcse9l.pdf")
        # so it can be handed to send_application_email_async as a real
        # path on disk. That randomly-generated basename was never mapped
        # back to the person's actual filename anywhere, so recipients saw
        # "email_queue_attach_8rjcse9l.pdf" in Gmail instead of, e.g.,
        # "Anusha_Resume.pdf". attachment_names now records, for every
        # resolved path, the human-readable name recovered via
        # original_filename_from_ref(ref) — the same helper the Email
        # Preview modal already uses to display attachment names — and
        # that map is passed through to send_application_email_async so
        # build_mime_message uses it for the Content-Disposition filename
        # instead of falling back to os.path.basename(attachment_path).
        attachment_paths = []
        attachment_names = {}
        missing_attachments = []
        tmp_cleanup_paths = []
        if item.attachments:
            # NOTE: os/asyncio are imported at module level (top of this
            # file) — deliberately NOT re-imported locally here. A local
            # import of either name anywhere in this function's body
            # makes Python treat that name as local for the function's
            # ENTIRE execution, which would break the earlier
            # os.path.join(...)/asyncio.to_thread(...) calls in the
            # "Fallback to Domain Delegation" block above with
            # UnboundLocalError — see the note on that block. `tempfile`
            # is not used elsewhere in this function, so it's safe to
            # import locally right here.
            import tempfile
            from s3_service import download_file_from_s3

            for ref in item.attachments:
                display_name = original_filename_from_ref(ref)
                local_candidate = os.path.join("/tmp/email_attachments", ref)
                if ref.startswith(EMAIL_ATTACHMENT_S3_PREFIX):
                    # PERFORMANCE: download_file_from_s3 is a blocking call;
                    # offload to a worker thread so it doesn't stall the
                    # event loop while other requests are in flight.
                    body_bytes, _ = await asyncio.to_thread(download_file_from_s3, ref)
                    if body_bytes:
                        fd, tmp_path = tempfile.mkstemp(
                            suffix=os.path.splitext(ref)[1] or ".pdf",
                            prefix="email_queue_attach_",
                        )
                        with os.fdopen(fd, "wb") as f:
                            f.write(body_bytes)
                        attachment_paths.append(tmp_path)
                        attachment_names[tmp_path] = display_name
                        tmp_cleanup_paths.append(tmp_path)
                    elif os.path.exists(local_candidate):
                        # Spaces fetch failed but the /tmp copy
                        # from this same server session is
                        # still there — use it rather than fail.
                        attachment_paths.append(local_candidate)
                        attachment_names[local_candidate] = display_name
                    else:
                        missing_attachments.append(ref)
                elif os.path.exists(local_candidate):
                    attachment_paths.append(local_candidate)
                    attachment_names[local_candidate] = display_name
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
            print(f"[email-queue debug {item.id}] Attachments resolved successfully. Sending via Gmail API...")
            # BUG FIX: this only ever sent item.content (plain text) — the
            # rich HTML signature + company banner built at queue-creation
            # time (send_email_now above) and stored in item.html_content
            # was never actually passed through to the real send. Now
            # attaches the banner inline via Content-ID when an HTML body
            # is present; rows queued before html_content existed (NULL)
            # still send as plain text only, same as before.
            from email_template import COMPANY_BANNER_CID, rewrite_signature_images_for_send

            send_html_body = item.html_content or None
            # BUG FIX (this session): previously attached the banner
            # unconditionally whenever any html_content existed, even
            # after build_signature_html stopped emitting the <img
            # src="cid:..."> tag entirely (logo removed from the
            # signature per updated requirement) — the image bytes were
            # still being attached with Content-Disposition: inline and
            # no matching cid: reference anywhere in the body, which most
            # clients (Gmail included) then show as a stray regular file
            # attachment instead of silently dropping it. Only attach it
            # when the HTML body genuinely references that Content-ID.
            inline_images = (
                [{"cid": COMPANY_BANNER_CID}]
                if send_html_body and f"cid:{COMPANY_BANNER_CID}" in send_html_body
                else None
            )

            # Custom signature images: rewrite each stored
            # .../signature-image/<key> reference in the HTML to a cid:,
            # download the matching bytes from Spaces to a tmp file, and
            # queue it up for build_mime_message the exact same way an
            # explicit-path inline image already works there. Cleaned up
            # in the same tmp_cleanup_paths pass below as attachments.
            if send_html_body:
                import tempfile as _tempfile_sig
                from s3_service import download_file_from_s3 as _download_sig_img

                send_html_body, sig_images = rewrite_signature_images_for_send(send_html_body)
                for sig_img in sig_images:
                    try:
                        img_bytes, _ct = await asyncio.to_thread(_download_sig_img, sig_img["key"])
                        if not img_bytes:
                            continue
                        fd, tmp_path = _tempfile_sig.mkstemp(
                            suffix=os.path.splitext(sig_img["key"])[1] or ".png",
                            prefix="email_sig_img_",
                        )
                        with os.fdopen(fd, "wb") as f:
                            f.write(img_bytes)
                        inline_images = (inline_images or []) + [{"cid": sig_img["cid"], "path": tmp_path}]
                        tmp_cleanup_paths.append(tmp_path)
                    except Exception as sig_img_err:
                        print(f"[email-queue] skipping signature image {sig_img['key']}: {sig_img_err}")

            send_result = await send_application_email_async(
                access_token=access_token,
                from_email=item.from_email,
                to_email=item.to_email,
                cc_email=item.cc_email or "",
                subject=item.subject,
                body=item.content or "",
                attachment_paths=attachment_paths,
                attachment_names=attachment_names,
                html_body=send_html_body,
                inline_images=inline_images,
            )
            print(f"[email-queue debug {item.id}] Gmail API returned successfully. Marking status as SENT...")
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
                from models import Application, Requirement

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
                    existing_app.email_body_preview = (item.content or "")[:500]

                    # BUG FIX ("Sent Applications shows the wrong/old resume
                    # after a fresh send"): download_application_resume
                    # (phase7.py) checks app.generated_resume_id FIRST and
                    # only falls back to resume_attachment_path if that's
                    # unset. If this Application row already had a
                    # generated_resume_id from an EARLIER, unrelated send to
                    # the same requirement, it lingered here forever — every
                    # later send that attached something different (like
                    # the base resume) still showed that old generated
                    # resume when viewed, since resume_attachment_path was
                    # never even reached. Whichever attachment a send
                    # actually used should be the one shown; if this send
                    # used a raw attachment ref, it's no longer represented
                    # by a generated_resume_id at all, so clear it here in
                    # the same place resume_attachment_path gets set.
                    if item.attachments:
                        existing_app.resume_attachment_path = item.attachments[0]
                        existing_app.attachments_sent = item.attachments
                        existing_app.generated_resume_id = None

                    # BUG FIX: sender was never recorded on this path.
                    # Only fill in if not already set, so a real recruiter
                    # confirm-send attribution from phase7.py is never
                    # silently overwritten.
                    if item.sent_by_user_id and not existing_app.recruiter_id:
                        existing_app.recruiter_id = item.sent_by_user_id
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
                        email_body_preview=(item.content or "")[:500],
                        sent_at=now,
                        applied_at=now,
                        recruiter_id=item.sent_by_user_id,
                        resume_attachment_path=item.attachments[0] if item.attachments else None,
                        attachments_sent=item.attachments if item.attachments else None,
                    ))

                # Move the requirement itself into the Submitted list on a
                # successful application send. Only advances NEW -> SUBMITTED
                # — never overwrites a requirement that's already further
                # along (REVIEWING/INTERVIEWING/CLOSED/REJECTED), so a second
                # consultant applying to an already-submitted requirement
                # doesn't silently regress its status.
                req_result = await session.execute(
                    select(Requirement).where(Requirement.id == item.requirement_id)
                )
                requirement = req_result.scalars().first()
                if requirement and requirement.status == "NEW":
                    requirement.status = "SUBMITTED"

            await reschedule_remaining_queued_emails(session, item.from_email, now)
            await session.commit()
        finally:
            for p in tmp_cleanup_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception as e:
        item_id = item.id
        import traceback
        tb_str = traceback.format_exc()
        print(f"[email-queue debug {item_id}] Failed to send item: {e}\nTraceback:\n{tb_str}", flush=True)
        from error_logger import log_db_error
        await log_db_error(stage="email_queue_worker_item", error=e, source_type="email_queue", source_id=item_id)
        await session.rollback()
        # Re-fetch item to update status safely after rollback
        result = await session.execute(select(EmailQueue).where(EmailQueue.id == item_id))
        failed_item = result.scalars().first()
        if failed_item:
            failed_item.status = "FAILED"
            # BUG FIX: str(e) is empty for some exception types (certain
            # HTTP client errors raised with no message), which made
            # status_text == "" — falsy, so send_email_now's `item.
            # status_text or "Failed to send application email."` fell
            # back to that generic message, hiding the real cause even in
            # this row's own status_text field (the traceback above still
            # printed to the console, but the actual error type/class
            # wasn't visible anywhere the frontend or a status lookup
            # could see it). Always include the exception's class name so
            # there's a real signal here even when the message itself is
            # blank.
            failed_item.status_text = str(e) or f"{type(e).__name__} (no error message)"

            # Deauthorize and remove token if OAuth/token refresh failed
            err_msg = str(e).lower()
            if (isinstance(e, ValueError) and ("token" in err_msg or "credential" in err_msg)) or "unauthorized" in err_msg or "invalid_grant" in err_msg or "401" in err_msg:
                try:
                    from models import Consultant, ConsultantEmailToken
                    cons_res = await session.execute(
                        select(Consultant).where(Consultant.id == failed_item.consultant_id)
                    )
                    consultant = cons_res.scalars().first()
                    if consultant:
                        consultant.gmail_connected = False

                        # BUG FIX ("authorized several users, most reverted
                        # on their own soon after — only two stayed
                        # Authorized"): this used to also set
                        # user.is_authorized = False here, conflating two
                        # genuinely separate things — whether this Gmail
                        # OAuth connection works, and whether this person
                        # is allowed to use the platform at all. A broken/
                        # expired Gmail token is common and expected (OAuth
                        # tokens do lapse), but it doesn't mean the account
                        # itself should be locked out of logging in — that
                        # over-broad reaction is exactly why authorizing a
                        # batch of users kept silently undoing itself: the
                        # background worker just needs to find ONE stale
                        # queued item for someone with a dead token, and
                        # their whole account got deauthorized along with
                        # it, even though only Gmail sending was actually
                        # broken. gmail_connected=False (above) and the
                        # token deletion (below) already fully capture
                        # "this consultant needs to reconnect Gmail before
                        # applying" — that's the correct, narrower signal.
                        # is_authorized now stays exactly what an admin set
                        # it to, only ever changed by an explicit admin
                        # action in User Management.

                        tok_res = await session.execute(
                            select(ConsultantEmailToken).where(
                                ConsultantEmailToken.consultant_id == consultant.id
                            )
                        )
                        tok = tok_res.scalars().first()
                        if tok:
                            await session.delete(tok)

                        # BUG FIX ("authorize a user, it reverts later on
                        # its own"): this only ever touched the ONE item
                        # that just failed. If this consultant has other
                        # QUEUED items sitting from earlier attempts (all
                        # doomed to hit this exact same dead token), the
                        # background worker loop picks each one up on a
                        # later pass, independently fails the same OAuth
                        # refresh, and re-fires this whole deauthorize
                        # block again — silently undoing an admin's manual
                        # re-authorization sometime after the fact, with no
                        # single moment where anything visibly "failed" to
                        # explain why. Since every other QUEUED item for
                        # this consultant would fail for the identical
                        # reason, mark them FAILED here too instead of
                        # leaving them to trigger the same repeat
                        # deauthorization one-by-one over time.
                        other_queued_result = await session.execute(
                            select(EmailQueue).where(
                                EmailQueue.consultant_id == failed_item.consultant_id,
                                EmailQueue.status == "QUEUED",
                                EmailQueue.id != failed_item.id,
                            )
                        )
                        other_queued_items = other_queued_result.scalars().all()
                        for other_item in other_queued_items:
                            other_item.status = "FAILED"
                            other_item.status_text = (
                                "Skipped — this consultant's Gmail authorization was already "
                                "found invalid while processing another queued item. "
                                "Reconnect Gmail before retrying."
                            )
                except Exception as deauth_err:
                    print(f"[email-queue] Failed to auto-deauthorize invalid token: {deauth_err}")

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
                from error_logger import log_db_error
                await log_db_error(
                    stage="email_queue_item_status_commit",
                    error=inner_e,
                    source_type="email_queue",
                    source_id=str(item_id),
                )
                await session.rollback()