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
from models import User

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
    """Set both auth cookies with the ONLY combination that can actually work.

    The frontend runs on a different origin than this API (no same-origin
    BFF in front of it anymore), so every request is cross-site. Cross-site
    cookies require SameSite=None + Secure=True — there is no other legal
    combination that a modern browser will honor on a cross-site
    fetch/XHR call:

      - SameSite=Lax/Strict cookies are simply not attached to cross-site
        fetch/XHR requests (only to top-level navigations). Setting Lax
        here, even though curl will happily show it "worked", means the
        browser silently drops it — that's the exact symptom that was
        reported (curl succeeds, browser stays logged out).
      - SameSite=None without Secure is rejected by every modern browser
        outright, regardless of curl behavior.
      - Secure cookies are only ever stored/sent by browsers over HTTPS.
        If this request did not arrive over HTTPS, a Secure cookie is
        pointless — the browser will not persist it.

    Net effect: cookie-based auth for a cross-site frontend is only
    possible once this API is served over HTTPS (directly, or via a
    TLS-terminating reverse proxy that forwards X-Forwarded-Proto).
    There is no code-only workaround for that half.

    Until HTTPS is in place, cookies are still set for same-site/local
    dev convenience, but callers MUST also send the raw token back in the
    JSON response body so the frontend can fall back to
    `Authorization: Bearer <token>` (see get_current_user below, which
    already accepts that header). This is the only path that reliably
    works cross-site over plain HTTP.

    Env overrides are intentionally NOT honored for samesite/secure
    anymore — a prior version allowed COOKIE_SAMESITE/COOKIE_SECURE to be
    set independently, which is exactly what produced the broken
    SameSite=Lax, no-Secure cookie seen in production. The only thing you
    can safely control is whether TLS is actually terminating in front of
    this service (via X-Forwarded-Proto).
    """
    is_https = _request_is_https(request)

    if is_https:
        samesite = "none"
        secure = True
    else:
        # Cannot legally set SameSite=None without Secure, and Secure is
        # useless without HTTPS. Fall back to Lax purely so local
        # same-origin/dev setups keep working; cross-site browser auth
        # will NOT work until HTTPS is live — rely on the Bearer token
        # fallback instead.
        samesite = "lax"
        secure = False
        logger.warning(
            "set_session_cookies: request not detected as HTTPS "
            "(no X-Forwarded-Proto: https and scheme != https). "
            "Cookies are being set as SameSite=Lax, Secure=False, which "
            "will NOT be sent on cross-site browser requests. Frontend "
            "must rely on the Authorization: Bearer token fallback until "
            "this API is served over HTTPS."
        )

    cookie_kwargs = dict(
        value=token,
        httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        expires=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        samesite=samesite,
        secure=secure,
    )
    response.set_cookie(key="rap_session", **cookie_kwargs)
    response.set_cookie(key="session", **cookie_kwargs)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dependency: extract and verify the session, return the User.

    Checks, in order:
      1. Authorization: Bearer <token> header — the reliable path for a
         cross-site frontend served over plain HTTP (see set_session_cookies
         docstring for why cookies alone can't do this yet).
      2. rap_session / session cookies — works once this API is behind
         HTTPS, or for same-site/local dev setups.
    """
    token = None

    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    if not token:
        token = request.cookies.get("rap_session") or request.cookies.get("session")

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_access_token(token)
    email: str = payload.get("sub")
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user