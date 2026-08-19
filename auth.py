# auth.py
# ---------------------------------------------------------------------------
# Shared authentication utilities.
# Extracted from main.py so routers (phase3.py, future phases) can import
# without creating a circular dependency with main.py.
#
# main.py  → imports from auth.py
# phase3.py → imports from auth.py
# No circular dependency.
# ---------------------------------------------------------------------------

import logging
import os
import warnings
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from database import get_db
from models import User, Consultant

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — identical to what was inline in main.py
# ---------------------------------------------------------------------------

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__truncate_error=False)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = "dev-only-insecure-secret-key-change-in-production"
    warnings.warn(
        "SECRET_KEY env var not set. Using insecure default — DO NOT use in production.",
        UserWarning,
        stacklevel=2,
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", "24"))
IS_PRODUCTION = os.getenv("NODE_ENV", "").lower() == "production"

# ---------------------------------------------------------------------------
# Auth utility functions — copied verbatim from main.py
# ---------------------------------------------------------------------------

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and verify a JWT; raises HTTPException on failure."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def _request_is_https(request: Request | None) -> bool:
    """Best-effort detection of whether this request actually arrived over TLS.

    request.url.scheme reflects what Starlette/uvicorn sees directly, which
    is "http" unless TLS terminates at uvicorn itself. If a reverse proxy
    (nginx, ALB, Cloudflare, etc.) terminates TLS in front of this process,
    that proxy MUST forward `X-Forwarded-Proto: https` or this will keep
    reporting False even in production — check the proxy config if so.
    """
    if request is None:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto:
        return forwarded_proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def set_session_cookies(response, token: str, request: Request | None = None) -> None:
    """No-op: Authentication is handled entirely via Authorization headers on the frontend."""
    pass


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dependency: extract and verify the JWT token from the Authorization header, return the User."""
    token = None

    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_access_token(token)
    email: str = payload.get("sub")
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # BUG FIX ("deactivating a consultant doesn't stop them from logging
    # in" / "should automatically kick a currently-logged-in consultant
    # out"): this used to only check `is_authorized` for ADMIN/RECRUITER
    # (`user.role in ("ADMIN", "RECRUITER") and not user.is_authorized`),
    # excluding CONSULTANT entirely. Worse, it never checked
    # Consultant.status at all, for any role. A JWT is a bearer token —
    # once issued it stays valid for ACCESS_TOKEN_EXPIRE_HOURS regardless
    # of anything that happens afterward, UNLESS something re-validates
    # the account on every request. get_current_user runs on every single
    # protected endpoint, so it's the one place that can actually catch
    # this: an admin deactivating a consultant (either via User
    # Management, which sets is_authorized=False, or via the Consultants
    # page, which sets Consultant.status="INACTIVE") had zero effect on a
    # consultant who was already logged in — they kept full access with
    # their existing token until it expired, up to 24h later. The login
    # endpoints (/auth/login, /auth/google/callback) already correctly
    # block a *fresh* login attempt for a deactivated consultant, but
    # that's not enough on its own to enforce "deactivated == no access."
    # Check is_authorized for every role now, and additionally check
    # Consultant.status for CONSULTANT — the same two checks
    # /auth/login and /auth/google/callback already perform at login
    # time — so deactivation takes effect on this user's very next
    # request, not just their next login attempt.
    #
    # DETAIL STRING: kept as the exact "User not found or inactive" text
    # (not the friendlier "Account is deactivated..." wording used at
    # /auth/login) on purpose — api.ts's global response interceptor
    # already matches this exact 401 + detail combo, clears the stored
    # token, and redirects to /login?error=account_deactivated. Frontend
    # dashboards already poll on a timer regardless of user activity
    # (useNotifications.ts every 30s, useConsultantDashboard.ts every
    # 60s), so a consultant who's just sitting on their dashboard when an
    # admin deactivates them gets auto-logged-out within well under a
    # minute — no new frontend code, new endpoint, or websocket needed;
    # this reuses that existing interceptor exactly as originally wired.
    if not user.is_authorized:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    if user.role == "CONSULTANT":
        consultant_status_result = await db.execute(
            select(Consultant.status).where(Consultant.user_id == user.id)
        )
        if consultant_status_result.scalar_one_or_none() == "INACTIVE":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user