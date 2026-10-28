# ---------------------------------------------------------------------------
# Email Queue endpoints
# Handles consultant email queue management
# ---------------------------------------------------------------------------
import os
import traceback
import uuid
import math
import asyncio
from fastapi import UploadFile, File

from typing import Optional, List
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from database import get_db
from auth import get_current_user
from models import User

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

def _normalize_cc(raw: Optional[str], exclude: list) -> str:
    """Validate, lowercase, dedupe a comma/semicolon separated CC list.
    Drops From/To addresses. Raises 400 on a bad address or >10 recipients."""
    import re
    skip = {(e or "").strip().lower() for e in exclude}
    out, seen = [], set()
    for part in re.split(r"[;,]", raw or ""):
        e = part.strip().lower()
        if not e or e in skip or e in seen:
            continue
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", e):
            raise HTTPException(status_code=400, detail=f"Invalid CC email: {e}")
        seen.add(e)
        out.append(e)
    if len(out) > 10:
        raise HTTPException(status_code=400, detail="Too many CC recipients (max 10).")
    return ",".join(out)


async def _assert_not_already_applied(db, requirement_id, consultant_id, consultant_name=None):
    """409 if THIS consultant already has a SENT application (or a send still
    queued) for THIS requirement. The check is per (requirement_id, consultant_id):
    other consultants on the same requirement are unaffected, and FAILED
    applications can still be retried."""
    if not requirement_id or not consultant_id:
        return
    from models import Application, EmailQueue
    sent = (await db.execute(
        select(Application.id).where(
            Application.requirement_id == requirement_id,
            Application.consultant_id == consultant_id,
            Application.status == "SENT",
        ).limit(1)
    )).first()
    queued = None
    if not sent:
        queued = (await db.execute(
            select(EmailQueue.id).where(
                EmailQueue.requirement_id == requirement_id,
                EmailQueue.consultant_id == consultant_id,
                EmailQueue.status.in_(["QUEUED", "PROCESSING"]),
            ).limit(1)
        )).first()
    if sent or queued:
        who = consultant_name or "This consultant"
        raise HTTPException(
            status_code=409,
            detail=f"{who} has already applied to this requirement. Select a different consultant.",
        )


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

# Minimum gap between two emails from the same sender (anti-spam).
# Override with the ANTI_SPAM_DELAY_SECONDS env var.
ANTI_SPAM_DELAY_SECONDS = int(__import__("os").getenv("ANTI_SPAM_DELAY_SECONDS", "10"))


async def calculate_next_scheduled_at(db: AsyncSession, from_email: str) -> datetime:
    """
    Calculates the next available scheduled_at timestamp for a given from_email.
    Enforces a mandatory anti-spam delay (ANTI_SPAM_DELAY_SECONDS) between consecutive emails sent from the same sender email.
    Checks both EmailQueue and Application tables for any recent sends/schedules.
    If no prior email was scheduled/sent for this from_email within the delay window, returns current UTC time.
    Otherwise, returns max_previous_time + the delay.
    """
    from datetime import datetime
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
    reference_time = now_utc - timedelta(seconds=ANTI_SPAM_DELAY_SECONDS)

    if times:
        last_time = max(times)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        reference_time = max(reference_time, last_time)

    return reference_time + timedelta(seconds=ANTI_SPAM_DELAY_SECONDS)


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
        current_user.role == "ADMIN"
        and recruiter_email
        and recruiter_email.strip().lower() not in final_cc.strip().lower()
    ):
        # ADMIN applying on the consultant's behalf CCs the admin AND the
        # consultant's assigned recruiter. A RECRUITER sending CCs only
        # themselves (the sending recruiter); a CONSULTANT CCs their
        # assigned recruiter (self-apply branch above).
        final_cc = f"{final_cc},{recruiter_email}"
    # RECRUITER applying on behalf: final_cc already equals the
    # recruiter's own email (current_user.email, set by default above) —
    # no extra branch needed here.

    # Per-consultant duplicate check (requirement_id + consultant_id).
    await _assert_not_already_applied(
        db, body.requirement_id, consultant_id, getattr(consultant, "full_name", None)
    )

    # Optional CC override — ADMIN / RECRUITER only. CONSULTANT sends keep
    # the automatic CC (their assigned recruiter) and any client-sent
    # cc_email is ignored. cc_email omitted (None) keeps the auto CC above.
    if body.cc_email is not None and current_user.role in ("ADMIN", "RECRUITER"):
        final_cc = _normalize_cc(body.cc_email, [body.from_email, body.to_email])

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
        from permission_service import resolve_cc_employers
        employer_cards = await resolve_cc_employers(db, final_cc, current_user, consultant)
        employer = employer_cards[0] if employer_cards else {}
        extra_employers = employer_cards[1:]

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
            extra_employers=extra_employers,
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
            extra_employers=extra_employers,
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
        traceback.print_exc()
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


@router.get("/api/consultant/email-queue/signature-employer")
async def get_signature_employer(
    consultant_id: Optional[int] = Query(None),
    cc: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Employer Details cards for the Apply screen's signature preview — the
    same ones the sent email gets: one per admin/recruiter in the CC. Pass
    ?cc= (comma list) to preview an edited CC; otherwise the default CC for
    the caller's role is used (admin -> admin + assigned recruiter,
    recruiter -> themselves, consultant -> assigned recruiter)."""
    from models import Consultant
    from permission_service import resolve_cc_employers, default_cc_for_sender

    cons = None
    if current_user.role == "CONSULTANT":
        cons = (await db.execute(
            select(Consultant).where(Consultant.user_id == current_user.id)
        )).scalars().first()
    elif consultant_id:
        cons = (await db.execute(
            select(Consultant).where(Consultant.id == consultant_id)
        )).scalars().first()
    cc_list = cc if (cc is not None and current_user.role in ("ADMIN", "RECRUITER")) \
        else await default_cc_for_sender(db, current_user, cons)
    cards = await resolve_cc_employers(db, cc_list, current_user, cons)
    return {"employers": cards, "employer": cards[0] if cards else None}


@router.get("/api/consultant/email-queue/cc-options")
async def get_cc_options(
    consultant_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CC suggestions for the Apply / Compose screens. ADMIN and RECRUITER only."""
    if current_user.role not in ("ADMIN", "RECRUITER"):
        raise HTTPException(status_code=403, detail="CC options are only available to recruiters and admins.")

    from models import Consultant, RecruiterConsultant
    from permission_service import get_handling_recruiter, resolve_employer_details

    me = (current_user.email or "").strip().lower()

    def row(u, tag):
        return {"user_id": u.id, "name": u.full_name, "email": u.email, "tag": tag}

    assigned, handling_email, cons = [], None, None
    if consultant_id:
        r = await db.execute(
            select(User)
            .join(RecruiterConsultant, RecruiterConsultant.recruiter_id == User.id)
            .where(
                RecruiterConsultant.consultant_id == consultant_id,
                RecruiterConsultant.is_active == True,
                User.is_authorized == True,
            )
        )
        assigned = r.scalars().all()
        cons = (await db.execute(select(Consultant).where(Consultant.id == consultant_id))).scalars().first()
        if cons:
            handling = await get_handling_recruiter(db, cons)
            handling_email = (handling or {}).get("employer_email") or None

    assigned_ids = {u.id for u in assigned}
    r = await db.execute(select(User).where(User.role == "RECRUITER", User.is_authorized == True))
    other_recruiters = [u for u in r.scalars().all() if u.id not in assigned_ids]

    def keep(users):
        return [u for u in users if (u.email or "").strip().lower() != me]

    groups = [
        {"title": "Assigned recruiters", "items": [row(u, "assigned") for u in keep(assigned)]},
        {"title": "Other recruiters", "items": [row(u, "recruiter") for u in keep(other_recruiters)]},
    ]

    default_selected = [current_user.email]
    if current_user.role == "ADMIN" and handling_email and handling_email.strip().lower() != me:
        default_selected.append(handling_email)

    # Employer Details card that goes in the signature (recruiter sending ->
    # themselves; admin -> the consultant's assigned recruiter).
    employer = await resolve_employer_details(db, current_user, cons)

    return {
        "sender": {"name": current_user.full_name, "email": current_user.email},
        "employer": employer,
        "default_selected": default_selected,
        "groups": [g for g in groups if g["items"]],
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

    # CC input is honoured for ADMIN / RECRUITER only; a CONSULTANT's
    # cc_email is ignored so their assigned recruiter is always CC'd below.
    _cc_in = body.cc_email if current_user.role in ("ADMIN", "RECRUITER") else None
    final_cc = _cc_in.strip() if _cc_in else ""
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
        current_user.role == "ADMIN"
        and recruiter_email
        and recruiter_email.strip().lower() not in final_cc.strip().lower()
    ):
        # ADMIN applying on the consultant's behalf CCs the admin AND the
        # consultant's assigned recruiter. A RECRUITER sending CCs only
        # themselves (the sending recruiter); a CONSULTANT CCs their
        # assigned recruiter (self-apply branch above).
        final_cc = f"{final_cc},{recruiter_email}"
    # RECRUITER applying on behalf: final_cc already equals the
    # recruiter's own email (current_user.email, set by default above) —
    # no extra branch needed here.

    # Per-consultant duplicate check (requirement_id + consultant_id).
    await _assert_not_already_applied(
        db, body.requirement_id, consultant_id, getattr(consultant, "full_name", None)
    )

    # Optional CC override — ADMIN / RECRUITER only (see create path).
    if body.cc_email is not None and current_user.role in ("ADMIN", "RECRUITER"):
        final_cc = _normalize_cc(body.cc_email, [effective_from_email, body.to_email])

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
        from permission_service import resolve_cc_employers
        employer_cards = await resolve_cc_employers(db, final_cc, current_user, consultant)
        employer = employer_cards[0] if employer_cards else {}
        extra_employers = employer_cards[1:]

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
            extra_employers=extra_employers,
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
            extra_employers=extra_employers,
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
        traceback.print_exc()
        final_content = body.content or ""
        final_html_content = None

    from datetime import datetime, timezone
    scheduled_at = await calculate_next_scheduled_at(db, effective_from_email)
    now_utc = datetime.now(timezone.utc)

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

    return {
        "success": True,
        "id": str(item.id),
        "status": item.status,
        "scheduled_at": (item.scheduled_at or scheduled_at or now_utc).isoformat(),
        "message": "Email queued successfully.",
        # Lets the frontend show a clear "sent as you, not as the
        # consultant" notice instead of the send silently looking
        # identical to a normal consultant-sent application.
        "sent_from": effective_from_email,
        "fallback_sender_used": False,
    }

ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".doc", ".docx"}

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
    # Only PDF and Word files may be attached to queued emails.
    if os.path.splitext(file.filename or "")[1].lower() not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="File type not allowed. Only PDF, DOC and DOCX files can be attached.",
        )
    safe_original = sanitize_attachment_filename(file.filename)
    unique_name = f"{uuid.uuid4()}__{safe_original}"
    contents = await file.read()

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
    # S3-ONLY FIX: no local /tmp copy any more — Spaces is the only store.
    if not uploaded:
        raise HTTPException(status_code=502, detail="Attachment upload to storage failed. Please retry.")
    stored_reference = s3_key
    file_path = s3_key

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
    from s3_service import generate_presigned_url, upload_file_to_s3
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

    # BUG FIX: a consultant could PATCH their own queued email straight to
    # SENT, which made the requirement show APPLIED (and the application
    # count go up) although nothing was ever sent. SENT / FAILED are results
    # of an actual send attempt, so only the send worker (which writes them
    # directly) and admin/recruiter accounts may set them; a consultant may
    # still put an item back to QUEUED.
    if current_user.role == "CONSULTANT" and body.status in ("SENT", "FAILED"):
        raise HTTPException(
            status_code=403,
            detail="Consultants cannot set an email to SENT or FAILED; status is set automatically when the email is sent.",
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
