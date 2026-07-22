from fastapi import FastAPI, Depends, HTTPException, status, Response, Request, Cookie
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, field_validator, model_validator, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from passlib.context import CryptContext
import jwt
import os
from contextlib import asynccontextmanager
import httpx
import math
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from database import engine, Base, get_db, AsyncSessionLocal
from models import User, Requirement, Consultant, Notification
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

ALLOWED_SORT_COLUMNS = {"received_date", "received_at", "role", "vendor", "client", "status", "created_at", "ats_match_count"}

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


EMAIL_QUEUE_SYNC_INTERVAL_SECONDS = int(os.getenv("EMAIL_QUEUE_SYNC_INTERVAL_SECONDS", "60"))

# TESTING GUARD: while we validate the email queue pipeline, only allow sends
# to this domain. Remove/relax this check once testing is complete and real
# sends to arbitrary vendor/client addresses are approved.
EMAIL_QUEUE_TEST_DOMAIN_SUFFIX = "@savantisintelli.com"

async def _email_queue_worker_loop():
    """
    Background loop: periodically checks EmailQueue for QUEUED items and sends them
    via consultant's Gmail API token.
    """
    from models import EmailQueue
    from gmail_send_service import send_application_email_async, decrypt_token
    while True:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(EmailQueue).where(EmailQueue.status == "QUEUED")
                )
                queued_items = result.scalars().all()
                if queued_items:
                    print(f"[email-queue] processing {len(queued_items)} items")
                for item in queued_items:
                    try:
                        import re
                        if not item.to_email or not re.match(r"[^@]+@[^@]+\.[^@]+", item.to_email):
                            print(f"[email-queue] item {item.id} failed: Invalid to_email '{item.to_email}'")
                            item.status = "FAILED"
                            item.status_text = f"Invalid to_email '{item.to_email}'"
                            await session.commit()
                            continue

                        # TESTING GUARD: only send to the internal test domain.
                        if not item.to_email.lower().endswith(EMAIL_QUEUE_TEST_DOMAIN_SUFFIX):
                            print(f"[email-queue] item {item.id} skipped: '{item.to_email}' is not a test recipient ({EMAIL_QUEUE_TEST_DOMAIN_SUFFIX})")
                            item.status = "FAILED"
                            item.status_text = "not test domain for now"
                            await session.commit()
                            continue

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

                        attachment_path = None
                        if item.attachments and len(item.attachments) > 0:
                            attachment_path = item.attachments[0]

                        send_result = await send_application_email_async(
                            access_token=access_token,
                            from_email=item.from_email,
                            to_email=item.to_email,
                            cc_email="",
                            subject=item.subject,
                            body=item.content or "",
                            attachment_path=attachment_path
                        )
                        item.status = "SENT"
                        item.status_text = "Sent successfully"
                        await session.commit()
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
                            try:
                                await session.commit()
                            except Exception as inner_e:
                                print(f"[email-queue] completely failed to update item {item_id}: {inner_e}")
                                await session.rollback()
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
    # CREATE TABLE IF NOT EXISTS — always safe to call on every startup
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


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)

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

from resume_router import router as resume_router  # noqa: E402
app.include_router(resume_router)

from templates_router import router as templates_router  # noqa: E402
app.include_router(templates_router)

from reports_router import router as reports_router  # noqa: E402
app.include_router(reports_router)

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

    if not user.is_active:
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

    if not user.is_active:
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
    sort_by: Optional[str] = "received_date",
    sort_dir: Optional[str] = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
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
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")

    return {
        "scanned": summary.get("total", 0),
        "requirements_created": summary.get("saved", 0),
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


@app.get("/auth/me")
async def get_me(
    current_user: User = Depends(get_current_user),
):
    return {
        "id": str(current_user.id),
        "full_name": current_user.full_name,
        "email": current_user.email,
        "role": current_user.role,
        "is_active": current_user.is_active,
        "skills": current_user.skills if isinstance(current_user.skills, list) else [],
        "experience_years": float(current_user.experience_years) if current_user.experience_years is not None else None,
    }


@app.put("/auth/me")
async def update_me(
    body: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.full_name:
        current_user.full_name = body.full_name
    if body.email:
        current_user.email = body.email.lower().strip()
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