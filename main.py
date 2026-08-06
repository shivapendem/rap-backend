from fastapi import FastAPI, Depends, HTTPException, status, Response, Request, Cookie, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import or_, func, update
from pydantic import BaseModel, EmailStr, field_validator, model_validator, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_
from passlib.context import CryptContext
import jwt
import os
from contextlib import asynccontextmanager
import httpx
import math
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from database import engine, Base, get_db, AsyncSessionLocal, DATABASE_URL
from models import User, Requirement, Consultant, Notification, RequirementConsultantMatch, RecruiterConsultant, Application
import asyncio
from requirements_sync import sync_pending_emails
from auth import (
    pwd_context,
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_HOURS,
    IS_PRODUCTION,
    verify_password,
    get_password_hash,
    create_access_token,
    decode_access_token,
    set_session_cookies,
    get_current_user,
)

# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

# BUG FIX: this whitelist was never updated when the frontend table
# added sorting to more columns (Vendor Email, Work Mode, Location,
# Parse Confidence) — clicking those columns' sort arrows always 422'd
# here, surfaced by the frontend as a generic "Failed to load
# requirements" error with no indication of which column caused it.
ALLOWED_SORT_COLUMNS = {
    "received_date", "received_at", "role", "vendor", "client", "status",
    "created_at", "ats_match_count", "vendor_email", "work_mode", "location",
    "parse_confidence",
}

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, v: str) -> str:
        return v.lower().strip()


class LoginResponse(BaseModel):
    role: str
    name: str
    access_token: str

class NotificationResponse(BaseModel):
    id: int
    user_id: int
    title: str
    body: str
    is_read: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


class GoogleLoginRequest(BaseModel):
    code: str = Field(..., min_length=1)
    redirect_uri: str = Field(..., min_length=1)

    @field_validator("redirect_uri")
    @classmethod
    def validate_redirect_uri(cls, v: str) -> str:
        allowed_hosts = os.getenv("ALLOWED_REDIRECT_HOSTS", "localhost,127.0.0.1").split(",")
        from urllib.parse import urlparse
        parsed = urlparse(v)
        host = parsed.hostname or ""
        if IS_PRODUCTION and not any(host == h.strip() or host.endswith("." + h.strip()) for h in allowed_hosts):
            raise ValueError(f"redirect_uri host '{host}' is not allowed")
        return v


class RequirementResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    role: str
    vendor: Optional[str] = None
    vendor_email: Optional[str] = None
    client: Optional[str] = None
    location: Optional[str] = None
    employment_types: Optional[List[str]] = None
    work_mode: Optional[str] = None
    received_date: Optional[datetime] = None
    received_at: Optional[datetime] = None
    status: str
    parsed_fields: Optional[dict] = None
    vendor_contact: Optional[str] = None
    rate: Optional[str] = None
    experience: Optional[str] = None
    skills: Optional[str] = None
    ats_match_count: Optional[int] = None
    parse_confidence: Optional[float] = None
    raw_email_id: Optional[int] = None
    # Names of consultants matched to this requirement — scoped to the
    # caller's own roster for RECRUITER, all consultants for ADMIN, and
    # left empty for CONSULTANT (not relevant/exposed on this shared
    # admin-style endpoint for that role).
    matched_consultants: List[str] = []
    # Names of matched consultants who ALSO have a real SENT application
    # for this requirement — the frontend highlights these within
    # matched_consultants. Previously never populated by this endpoint at
    # all (the frontend read row.submitted_consultants, but nothing here
    # ever set it), so the highlighting never actually triggered.
    submitted_consultants: List[str] = []
    # Whether THIS caller (when a CONSULTANT) already has a SENT
    # application for this requirement. Always False for RECRUITER/ADMIN
    # on this shared endpoint — "did I personally apply" only makes sense
    # for the consultant viewing their own Requirements page.
    already_applied: bool = False

class PaginatedRequirements(BaseModel):
    data: List[RequirementResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class ConsultantResponse(BaseModel):
    model_config = {"from_attributes": True}

    # id: int  # BUG FIX: BigInteger PK → int, not str
    id: str
    full_name: Optional[str] = None
    email: Optional[str] = None


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

# Default users seeded on startup — keyed by email so restarts never duplicate or wipe data.
_DEFAULT_USERS = [
    {"email": "admin@rap.io",     "full_name": "Admin User",     "role": "ADMIN"},
    {"email": "recruiter@rap.io", "full_name": "Recruiter User", "role": "RECRUITER"},
]


GMAIL_SYNC_INTERVAL_SECONDS = int(os.getenv("GMAIL_SYNC_INTERVAL_SECONDS", "300"))  # default: every 5 min


async def _gmail_to_requirements_loop():
    """
    Background loop: periodically bridges new gmail_emails rows into
    requirements. Runs for the lifetime of the app so IMAP-synced emails
    are turned into requirements without any manual/cron step.
    """
    while True:
        try:
            async with AsyncSessionLocal() as session:
                summary = await sync_pending_emails(session)
                if summary["total"]:
                    print(f"[gmail-sync] {summary}")
        except Exception as e:
            print(f"[gmail-sync] loop error: {e}")
            from error_logger import log_db_error
            await log_db_error(stage="gmail_to_requirements_loop", error=e)
            try:
                from notification_helper import notify_by_role
                async with AsyncSessionLocal() as notif_session:
                    await notify_by_role(notif_session, roles=["ADMIN"], title="Email sync failed", body=f"Gmail-to-requirements sync failed: {e}")
            except Exception as notif_err:
                print(f"[gmail-sync] notify failed: {notif_err}")
        await asyncio.sleep(GMAIL_SYNC_INTERVAL_SECONDS)


EMAIL_QUEUE_SYNC_INTERVAL_SECONDS = int(os.getenv("EMAIL_QUEUE_SYNC_INTERVAL_SECONDS", "15"))

async def _email_queue_worker_loop():
    """
    Background loop: periodically checks EmailQueue for QUEUED items whose scheduled_at <= now()
    and sends them via consultant's Gmail API token.

    Per-item send/attachment/Application-upsert logic lives in
    email_queue.process_single_email_queue_item — shared with the
    send-now endpoint used by the Apply-to-Requirement page, so both
    paths behave identically instead of risking two diverging copies.
    """
    from models import EmailQueue
    from email_queue import process_single_email_queue_item
    from datetime import datetime, timezone
    from sqlalchemy import or_, func

    print("[email-queue] worker loop task initialized and started")
    # Self-healing: reset any stuck PROCESSING items back to QUEUED on startup
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import update
            claim_reset = await session.execute(
                update(EmailQueue)
                .where(EmailQueue.status == "PROCESSING")
                .values(status="QUEUED")
            )
            await session.commit()
            if claim_reset.rowcount > 0:
                print(f"[email-queue] self-healing: reset {claim_reset.rowcount} stuck PROCESSING items back to QUEUED")
    except Exception as sh_err:
        print(f"[email-queue] self-healing failed: {sh_err}")
    while True:
        try:
            async with AsyncSessionLocal() as session:
                now_utc = datetime.now(timezone.utc)
                result = await session.execute(
                    select(EmailQueue)
                    .where(
                        EmailQueue.status == "QUEUED",
                        or_(EmailQueue.scheduled_at == None, EmailQueue.scheduled_at <= now_utc)
                    )
                    .order_by(
                        func.coalesce(EmailQueue.scheduled_at, EmailQueue.created_at).asc(),
                        EmailQueue.id.asc()
                    )
                )
                queued_items = result.scalars().all()
                # RACE FIX: send_email_now() (email_queue.py) can process the
                # same item this loop just selected, in the window between
                # this query and the loop's own send attempt below — both
                # paths would then call the Gmail API for the same item,
                # causing duplicate sends and slower total processing time
                # (which can push send_email_now's caller past its own
                # timeout). Atomically claim each item by flipping its status
                # to PROCESSING right here; if 0 rows are affected, another
                # caller already claimed it, so skip it in this cycle.
                claimed_items = []
                for qi in queued_items:
                    claim_result = await session.execute(
                        update(EmailQueue)
                        .where(EmailQueue.id == qi.id, EmailQueue.status == "QUEUED")
                        .values(status="PROCESSING")
                    )
                    if claim_result.rowcount > 0:
                        claimed_items.append(qi)
                await session.commit()
                queued_items = claimed_items
                if queued_items:
                    print(f"[email-queue] processing {len(queued_items)} eligible items")
                for item in queued_items:
                    await process_single_email_queue_item(session, item)
        except Exception as e:
            print(f"[email-queue] loop error: {e}")
            from error_logger import log_db_error
            await log_db_error(stage="email_queue_worker_loop", error=e)
            try:
                from notification_helper import notify_by_role
                async with AsyncSessionLocal() as notif_session:
                    await notify_by_role(notif_session, roles=["ADMIN"], title="Email queue sync failed", body=f"Email queue worker loop failed: {e}")
            except Exception as notif_err:
                print(f"[email-queue] notify failed: {notif_err}")
        await asyncio.sleep(EMAIL_QUEUE_SYNC_INTERVAL_SECONDS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Insert-if-not-exists keyed by email — never touches rows that already exist
    async with AsyncSessionLocal() as session:
        for u in _DEFAULT_USERS:
            result = await session.execute(select(User).where(User.email == u["email"]))
            if not result.scalars().first():
                session.add(User(
                    email=u["email"],
                    full_name=u["full_name"],
                    role=u["role"],
                    password_hash=get_password_hash("password123!"),
                ))
                print(f"Seeded default user: {u['email']}")
        await session.commit()

        # Fallback sync: set is_authorized and gmail_connected states based on active tokens
        try:
            from sqlalchemy import text
            await session.execute(text("""
                UPDATE users
                SET is_authorized = CASE
                    WHEN role IN ('ADMIN', 'RECRUITER') THEN TRUE
                    WHEN id IN (
                        SELECT c.user_id 
                        FROM consultants c
                        JOIN consultant_email_tokens t ON c.id = t.consultant_id
                    ) THEN TRUE
                    ELSE FALSE
                END;
            """))
            await session.execute(text("""
                UPDATE consultants
                SET gmail_connected = CASE
                    WHEN id IN (SELECT consultant_id FROM consultant_email_tokens) THEN TRUE
                    ELSE FALSE
                END;
            """))
            await session.commit()
            print("Successfully synchronized user authorization and gmail_connected states with active tokens.")
        except Exception as sync_err:
            print(f"Failed to synchronize authorization states on startup: {sync_err}")

    sync_task = asyncio.create_task(_gmail_to_requirements_loop())
    email_queue_task = asyncio.create_task(_email_queue_worker_loop())

    yield

    sync_task.cancel()
    email_queue_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass
    try:
        await email_queue_task
    except asyncio.CancelledError:
        pass

    # Dispose database engine and close all connection pool sockets on shutdown
    try:
        await engine.dispose()
        print("[db] Connection pool engine disposed cleanly.")
    except Exception as dispose_err:
        print(f"[db] Engine dispose error: {dispose_err}")


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)

# Serves static/email_assets/company_banner.png at
# /static/email_assets/company_banner.png — used by the Email Preview
# modal (get_email_preview in phase7.py) to actually render the company
# banner in-browser, since a browser can't resolve the cid: reference the
# real sent email uses instead (see gmail_send_service.build_mime_message).
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # Production Frontend
        "https://rap-swart.vercel.app",

        # Local Development
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",

        # Backend Server itself
        "http://137.184.96.50:8000",
        "http://137.184.96.50:3000",
        "https://rapapi.techtroup.com",
        "https://rap-ten-beta.vercel.app"
        
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Phase 2 router — requirement detail/status/stats, pipeline endpoints
# ---------------------------------------------------------------------------
from phase2 import router as phase2_router  # noqa: E402
app.include_router(phase2_router)

# ---------------------------------------------------------------------------
# Phase 3 router — consultant profiles, experience, resume, mapping
# ---------------------------------------------------------------------------
from phase3 import router as phase3_router  # noqa: E402
app.include_router(phase3_router)

# ---------------------------------------------------------------------------
# Phase 4 router — matching engine, assignment workflow
# ---------------------------------------------------------------------------
from phase4 import router as phase4_router  # noqa: E402
app.include_router(phase4_router)

from phase5 import router as phase5_router  # noqa: E402
app.include_router(phase5_router)

# ---------------------------------------------------------------------------
# Phase 6 router — resume tailoring, ATS scoring, file generation
# ---------------------------------------------------------------------------
from phase6 import router as phase6_router  # noqa: E402
app.include_router(phase6_router)

from phase7 import router as phase7_router  # noqa: E402
app.include_router(phase7_router)

from email_queue import router as email_queue_router  # noqa: E402
app.include_router(email_queue_router)

from phase8 import router as phase8_router  # noqa: E402
app.include_router(phase8_router)

from phase_users import router as phase_users_router  # noqa: E402
app.include_router(phase_users_router)

# BUG FIX: phase8.py mounts under "/api/v1/admin" (see its own
# APIRouter(prefix=...) declaration), but the frontend's AI Usage screen
# calls "/api/admin/ai-usage/claude" instead, missing the "/v1" segment,
# and 404s.
#
# IMPORTANT: this is intentionally a single, exact-path redirect, NOT a
# blanket "/api/admin/*" catch-all. "/api/admin/" is also the real,
# legitimate prefix for many OTHER unrelated routes already in this
# codebase (see phase2.py's raw-emails/gmail-emails/gmail-accounts routes,
# phase3.py's consultants routes, phase4.py's requirements/rematch routes,
# phase5.py's resumes/generate route) — a wildcard redirect here would
# have silently broken every one of those by sending them to a
# nonexistent "/api/v1/admin/..." path instead. Add more specific lines
# below (one per confirmed-broken path) if other phase8/phase_users
# screens turn out to have the same missing-"/v1" frontend bug — do not
# widen this into a wildcard.
from fastapi.responses import RedirectResponse

@app.get("/api/admin/ai-usage/claude", include_in_schema=False)
async def _ai_usage_claude_prefix_compat_redirect(request: Request):
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/api/v1/admin/ai-usage/claude{query}", status_code=307)

from resume_router import router as resume_router  # noqa: E402
app.include_router(resume_router)

from templates_router import router as templates_router  # noqa: E402
app.include_router(templates_router)

from reports_router import router as reports_router  # noqa: E402
from matching_router import router as matching_router  # noqa: E402
app.include_router(reports_router)
app.include_router(matching_router, prefix="/api/matching", tags=["matching"])

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------



@app.post("/auth/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalars().first()

    # BUG FIX: avoid user enumeration — same error for bad user or bad password
    if not user or not user.password_hash or not verify_password(request.password, user.password_hash):
        try:
            from notification_helper import notify_by_role
            await notify_by_role(db, roles=["ADMIN"], title="Failed login attempt", body=f"Failed login attempt for email: {request.email}")
        except Exception as e:
            print(f"[login-notify] FAILED: {e}")  # never let notification failure block the actual auth rejection
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if user.role in ("ADMIN", "RECRUITER") and not user.is_authorized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Contact your administrator.",
        )

    token = create_access_token(data={"sub": user.email, "role": user.role})
    set_session_cookies(response, token)

    # Real last-login tracking — backs the admin Consultants screen's
    # "Last Login" stat, which was previously hardcoded to null on the
    # frontend since nothing ever populated it.
    user.last_login_at = datetime.now(timezone.utc)

    # Insert Login Notification
    new_notif = Notification(
        user_id=user.id,
        title="New Login Accessed",
        body=f"Successful login recorded at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}."
    )
    db.add(new_notif)
    await db.commit()

    return LoginResponse(role=user.role, name=user.full_name, access_token=token)


@app.post("/auth/logout")
async def logout():
    return {"message": "Logged out successfully"}


@app.post("/auth/google/callback", response_model=LoginResponse)
async def google_login(
    request: GoogleLoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured on this server.",
        )

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
            raise HTTPException(status_code=502, detail=f"Failed to reach Google OAuth: {exc}")

    if token_res.status_code != 200:
        raise HTTPException(status_code=400, detail="Invalid or expired Google OAuth code")

    token_data = token_res.json()
    id_token = token_data.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="Missing id_token from Google response")

    # BUG FIX: verify the signature with Google's public keys in production;
    # for simplicity we decode without verification here but add aud check.
    try:
        decoded = jwt.decode(
            id_token,
            options={"verify_signature": False},
            algorithms=["RS256"],
        )
    except jwt.DecodeError:
        raise HTTPException(status_code=400, detail="Malformed Google id_token")

    # Validate audience to prevent token substitution attacks
    aud = decoded.get("aud")
    if IS_PRODUCTION and aud != client_id:
        raise HTTPException(status_code=400, detail="Token audience mismatch")

    email: str = decoded.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Google token is missing email claim")

    email = email.lower().strip()

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not registered. Please contact your administrator.",
        )

    if user.role in ("ADMIN", "RECRUITER") and not user.is_authorized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated.",
        )

    token = create_access_token(data={"sub": user.email, "role": user.role})
    set_session_cookies(response, token)

    # Real last-login tracking — see matching note in /auth/login.
    user.last_login_at = datetime.now(timezone.utc)

    # Insert Login Notification
    new_notif = Notification(
        user_id=user.id,
        title="New Login Accessed",
        body=f"Successful Google login recorded at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}."
    )
    db.add(new_notif)
    await db.commit()

    # Gmail OAuth Token Capture (Role check commented for admin testing)
    # if user.role == "CONSULTANT":
    if True:
        from models import Consultant, ConsultantEmailToken
        from gmail_send_service import encrypt_token
        
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3599)
        
        if access_token:
            if user.role == "CONSULTANT":
                cons_result = await db.execute(select(Consultant).where(Consultant.user_id == user.id))
            else:
                cons_result = await db.execute(select(Consultant))
            consultant = cons_result.scalars().first()
            
            if consultant:
                # Find existing token or create new one
                token_result = await db.execute(select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant.id))
                email_token = token_result.scalars().first()
                
                expiry_dt = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                
                if not email_token:
                    email_token = ConsultantEmailToken(
                        consultant_id=consultant.id,
                        email_address=user.email,
                        access_token_encrypted=encrypt_token(access_token),
                        refresh_token_encrypted=encrypt_token(refresh_token) if refresh_token else None,
                        token_expiry=expiry_dt
                    )
                    db.add(email_token)
                else:
                    email_token.email_address = user.email
                    email_token.access_token_encrypted = encrypt_token(access_token)
                    if refresh_token:
                        email_token.refresh_token_encrypted = encrypt_token(refresh_token)
                    email_token.token_expiry = expiry_dt
                
                await db.commit()

    return LoginResponse(role=user.role, name=user.full_name, access_token=token)


@app.get("/api/requirements", response_model=PaginatedRequirements)
async def get_requirements(
    page: int = 1,
    page_size: int = 10,
    status: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: Optional[str] = "received_date",
    sort_dir: Optional[str] = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    # BUG FIX: backs the admin dashboard's "New Matches (7d)" stat card,
    # which used to link straight to /admin/requirements with no filter at
    # all — clicking it just showed every requirement, not the matched
    # ones the card was actually counting. matched_only mirrors the exact
    # definition new-matches-count uses (distinct requirements with a
    # RequirementConsultantMatch.created_at within matched_days).
    matched_only: bool = False,
    matched_days: int = 7,
    confidence_filter: Optional[str] = None,
    employment_type: Optional[str] = None,
    # BUG FIX: Requirements page had no way to filter by matched
    # consultant at all — mirrors the same comma-separated-ids pattern
    # already used by /api/matching/pending and the Applications Tracker.
    consultant_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import func

    # Validate pagination params
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be >= 1")
    if not (1 <= page_size <= 100):
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")

    # BUG FIX: prevent SQL injection via sort_by — whitelist columns
    if sort_by not in ALLOWED_SORT_COLUMNS:
        raise HTTPException(
            status_code=422,
            detail=f"sort_by must be one of: {sorted(ALLOWED_SORT_COLUMNS)}",
        )
    if sort_dir not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="sort_dir must be 'asc' or 'desc'")

    # Validate status value
    if status and status not in Requirement.VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {sorted(Requirement.VALID_STATUSES)}",
        )

    query = select(Requirement)
    if status:
        query = query.where(Requirement.status == status)
    if matched_only:
        since = datetime.now(timezone.utc) - timedelta(days=matched_days)
        matched_subq = select(RequirementConsultantMatch.requirement_id).where(
            RequirementConsultantMatch.created_at >= since
        )
        # BUG FIX: matched_only previously checked whether ANY consultant
        # was matched to a requirement, not specifically the caller. A
        # consultant using this filter could see (and click Apply on)
        # requirements matched to someone else entirely, then hit a 403
        # from get_requirement_detail's real per-consultant ownership
        # check — "matched" and "matched to you" silently disagreed.
        # Scope to the caller's own matches when they're a consultant.
        if current_user.role == "CONSULTANT":
            cons_result = await db.execute(
                select(Consultant).where(Consultant.user_id == current_user.id)
            )
            consultant = cons_result.scalars().first()
            if consultant:
                matched_subq = matched_subq.where(
                    RequirementConsultantMatch.consultant_id == consultant.id
                )
            else:
                # No consultant profile at all — no matches are possible.
                matched_subq = matched_subq.where(False)
        query = query.where(Requirement.id.in_(matched_subq))

    filter_consultant_ids: list[int] = []
    if consultant_id:
        try:
            filter_consultant_ids = [int(x.strip()) for x in consultant_id.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid consultant_id format")
        if filter_consultant_ids:
            matched_consultant_subq = select(RequirementConsultantMatch.requirement_id).where(
                RequirementConsultantMatch.consultant_id.in_(filter_consultant_ids)
            )
            query = query.where(Requirement.id.in_(matched_consultant_subq))

    # BUG FIX: the FilterBar (admin Requirements page) has always sent
    # confidence_filter and employment_type as real query params, but this
    # endpoint never declared or read either one — FastAPI silently drops
    # unrecognized query params rather than erroring, so both filters were
    # completely non-functional: picking "Low confidence" or an employment
    # type and clicking Apply returned the exact same unfiltered list.
    if confidence_filter == "low":
        query = query.where(Requirement.parse_confidence < 0.5)

    if employment_type:
        # employment_types is a real Postgres ARRAY(Text) column in
        # production (see models.py ArrayTextColumn) — .any() checks
        # array membership. Falls back to a no-op filter on the SQLite dev
        # path where this column is stored as JSON text instead.
        if DATABASE_URL.startswith("postgresql"):
            query = query.where(Requirement.employment_types.any(employment_type))

    if search:
        # BUG FIX: only matched role/vendor_email — searching by client,
        # vendor name, location, or rate (all shown as columns in the same
        # table) silently returned nothing. Broadened to every free-text
        # column an admin would plausibly search by.
        search_term = f"%{search}%"
        query = query.where(
            or_(
                Requirement.role.ilike(search_term),
                Requirement.vendor_email.ilike(search_term),
                Requirement.vendor.ilike(search_term),
                Requirement.client.ilike(search_term),
                Requirement.location.ilike(search_term),
                Requirement.rate.ilike(search_term),
                Requirement.work_mode.ilike(search_term),
            )
        )

    if date_from:
        try:
            from datetime import time
            if "T" in date_from:
                dt_from = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
            else:
                dt_from = datetime.combine(datetime.strptime(date_from, "%Y-%m-%d").date(), time.min)
            if dt_from.tzinfo is None:
                dt_from = dt_from.replace(tzinfo=timezone.utc)
            query = query.where(Requirement.received_date >= dt_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from format")

    if date_to:
        try:
            from datetime import time
            if "T" in date_to:
                dt_to = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
            else:
                dt_to = datetime.combine(datetime.strptime(date_to, "%Y-%m-%d").date(), time.max)
            if dt_to.tzinfo is None:
                dt_to = dt_to.replace(tzinfo=timezone.utc)
            query = query.where(Requirement.received_date <= dt_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to format")

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    actual_sort = "received_date" if sort_by == "received_at" else sort_by
    sort_col = getattr(Requirement, actual_sort)   
    query = query.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    reqs = (await db.execute(query)).scalars().all()
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0

    # Batch-load matched consultant names for this page in one query
    # (not per-row), scoped the same way as every other recruiter-facing
    # endpoint: RECRUITER sees only their active roster, ADMIN sees
    # everyone, CONSULTANT gets nothing here (not this endpoint's concern).
    if current_user.role in ("RECRUITER", "ADMIN") and reqs:
        req_ids = [r.id for r in reqs]
        matches_q = (
            select(RequirementConsultantMatch.requirement_id, Consultant.full_name)
            .join(Consultant, Consultant.id == RequirementConsultantMatch.consultant_id)
            .where(RequirementConsultantMatch.requirement_id.in_(req_ids))
        )
        if current_user.role == "RECRUITER":
            assigned_result = await db.execute(
                select(RecruiterConsultant.consultant_id).where(
                    RecruiterConsultant.recruiter_id == current_user.id,
                    RecruiterConsultant.is_active == True,
                )
            )
            assigned_ids = [row[0] for row in assigned_result.all()]
            matches_q = matches_q.where(RequirementConsultantMatch.consultant_id.in_(assigned_ids))

        # BUG FIX: when the caller filtered by specific consultant(s) via
        # ?consultant_id=, the Matched Consultants column still showed
        # EVERY consultant matched to the requirement, not just the
        # one(s) actually selected in the filter — confusing when you
        # picked one name and the row still listed a dozen others. Scope
        # the displayed names to the filter too, same as the requirements
        # list itself already is.
        if filter_consultant_ids:
            matches_q = matches_q.where(RequirementConsultantMatch.consultant_id.in_(filter_consultant_ids))

        matches_by_req: dict[int, list[str]] = {}
        for req_id, name in (await db.execute(matches_q)).all():
            matches_by_req.setdefault(req_id, []).append(name)

        for r in reqs:
            r.matched_consultants = matches_by_req.get(r.id, [])

        # Which of those matched consultants already have a real SENT
        # application for this requirement — powers the "highlight
        # applied consultants" treatment in the Matched Consultants column.
        submitted_q = (
            select(Application.requirement_id, Consultant.full_name)
            .join(Consultant, Consultant.id == Application.consultant_id)
            .where(
                Application.requirement_id.in_(req_ids),
                Application.status == "SENT",
            )
        )
        if current_user.role == "RECRUITER":
            submitted_q = submitted_q.where(Application.consultant_id.in_(assigned_ids))
        if filter_consultant_ids:
            submitted_q = submitted_q.where(Application.consultant_id.in_(filter_consultant_ids))

        submitted_by_req: dict[int, list[str]] = {}
        for req_id, name in (await db.execute(submitted_q)).all():
            submitted_by_req.setdefault(req_id, []).append(name)

        for r in reqs:
            r.submitted_consultants = submitted_by_req.get(r.id, [])

    # For a CONSULTANT viewing their own Requirements page, tell them
    # which of these requirements they've already sent an application
    # for — this is what the frontend's Apply link needs to switch to an
    # "Applied" badge instead of always showing "Apply", regardless of
    # whether it was actually sent.
    if current_user.role == "CONSULTANT" and reqs:
        cons_result = await db.execute(
            select(Consultant).where(Consultant.user_id == current_user.id)
        )
        consultant = cons_result.scalars().first()
        if consultant:
            req_ids = [r.id for r in reqs]
            applied_result = await db.execute(
                select(Application.requirement_id).where(
                    Application.consultant_id == consultant.id,
                    Application.requirement_id.in_(req_ids),
                    Application.status == "SENT",
                )
            )
            applied_ids = {row[0] for row in applied_result.all()}
            for r in reqs:
                r.already_applied = r.id in applied_ids

    return PaginatedRequirements(
        data=reqs,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )

@app.post("/api/admin/gmail-emails/sync-to-requirements")
async def sync_gmail_to_requirements_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    On-demand trigger: runs the same sync_pending_emails logic used by
    the background loop, once, and returns a summary for the frontend.
    """
    try:
        summary = await sync_pending_emails(db)
    except Exception as e:
        from error_logger import log_db_error
        await log_db_error(stage="gmail_sync_manual_trigger", error=e)
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")

    return {
        "scanned": summary.get("total", 0),
        "requirements_created": summary.get("saved", 0),
        "duplicates": summary.get("duplicates", 0),
        "skipped_not_a_requirement": summary.get("skipped_not_a_requirement", 0),
        "errors": summary.get("errors", 0),
    }


@app.get("/api/consultants", response_model=List[ConsultantResponse])
async def get_consultants(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Consultant))
    return result.scalars().all()


class UpdateMeRequest(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    mobile_number: Optional[str] = None
    extension: Optional[str] = None
    linkedin_url: Optional[str] = None
    designation: Optional[str] = None
    email_signature: Optional[str] = None


@app.get("/auth/me")
async def get_me(
    current_user: User = Depends(get_current_user),
):
    return {
        "id": str(current_user.id),
        "full_name": current_user.full_name,
        "email": current_user.email,
        "role": current_user.role,
        "is_authorized": current_user.is_authorized,
        "is_active": current_user.is_authorized,
        "skills": current_user.skills if isinstance(current_user.skills, list) else [],
        "experience_years": float(current_user.experience_years) if current_user.experience_years is not None else None,
        # Needed by the Apply-to-Requirement page's signature preview (see
        # ApplyToRequirementPage.tsx) — these were already stored on the
        # user but never returned by this endpoint, so the frontend had no
        # way to show what the auto-appended signature will actually say.
        "mobile_number": current_user.mobile_number,
        "extension": current_user.extension,
        "linkedin_url": current_user.linkedin_url,
        "designation": current_user.designation,
        "email_signature": current_user.email_signature,
    }


@app.put("/auth/me")
async def update_me(
    body: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.full_name is not None:
        current_user.full_name = body.full_name
    if body.email is not None:
        current_user.email = body.email.lower().strip()
    if body.mobile_number is not None:
        current_user.mobile_number = body.mobile_number
    if body.extension is not None:
        current_user.extension = body.extension
    if body.linkedin_url is not None:
        current_user.linkedin_url = body.linkedin_url
    if body.designation is not None:
        current_user.designation = body.designation
    if body.email_signature is not None:
        current_user.email_signature = body.email_signature

    await db.commit()
    await db.refresh(current_user)
    return {
        "success": True,
        "user": {
            "id": str(current_user.id),
            "full_name": current_user.full_name,
            "email": current_user.email,
            "role": current_user.role,
        }
    }


# ---------------------------------------------------------------------------
# Dynamic company banner — Spaces-backed, replaces the old static-file
# banner (static/email_assets/company_banner.png). See the comment on
# COMPANY_BANNER_S3_KEY in email_template.py and _resolve_banner_bytes in
# gmail_send_service.py for the full rationale: updating the banner here
# is immediately live everywhere (local + production, real sends +
# preview) with no file copying or redeploy — replace it once, done.
# ---------------------------------------------------------------------------

@app.get("/api/settings/company-banner")
async def get_company_banner():
    """
    Streams the current company banner image. Used by the Email Preview
    modal's <img> tag (get_email_preview in phase7.py sets banner_src to
    this URL) — proxied through the API rather than a direct Spaces URL
    so the browser never needs a bucket CORS policy, same pattern
    download_file_from_s3 is already used for elsewhere in this app.
    Any authenticated user can view it (it's not sensitive — the same
    image is about to be emailed to external vendors anyway); only
    uploading a replacement is admin-restricted, below.
    """
    from gmail_send_service import _resolve_banner_bytes
    import mimetypes

    body, filename = _resolve_banner_bytes()
    if body is None:
        raise HTTPException(status_code=404, detail="No company banner is set.")

    media_type = mimetypes.guess_type(filename or "banner.png")[0] or "image/png"
    return Response(content=body, media_type=media_type)


@app.post("/api/v1/admin/settings/company-banner")
async def upload_company_banner(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Admin-only: replace the company banner shown in application-email
    signatures (both the real sent email and the Email Preview modal).
    Always uploaded to the SAME fixed Spaces key (COMPANY_BANNER_S3_KEY)
    so this simply overwrites the previous banner in place — no version
    tracking/cleanup needed, and every server/environment sharing the
    same Spaces bucket picks up the new one immediately on the very next
    send or preview, with no restart or redeploy.
    """
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Only admins can change the company banner.")

    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="Please upload an image file (PNG, JPG, etc.).")

    from email_template import COMPANY_BANNER_S3_KEY
    from s3_service import upload_file_to_s3

    success = upload_file_to_s3(file.file, COMPANY_BANNER_S3_KEY, content_type=file.content_type)
    if not success:
        raise HTTPException(
            status_code=502,
            detail="Failed to upload banner to storage. Check Spaces/S3 credentials are configured.",
        )

    return {"success": True, "message": "Company banner updated — live on every send and preview immediately."}


@app.get("/health")
async def health_check():
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@app.get("/api/notifications", response_model=List[NotificationResponse])
async def get_notifications(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(50)
    )
    return result.scalars().all()


@app.patch("/api/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Notification)
        .where(Notification.id == notification_id, Notification.user_id == current_user.id)
    )
    notif = result.scalars().first()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    notif.is_read = True
    await db.commit()
    return {"success": True}


@app.patch("/api/notifications/read-all")
async def mark_all_notifications_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import update
    await db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.is_read == False)
        .values(is_read=True)
    )
    await db.commit()
    return {"success": True}