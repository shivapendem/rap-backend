# auth.py
# ---------------------------------------------------------------------------
# Shared authentication utilities.
# Extracted from main.py so routers (phase3.py, future phases) can import
# without creating a circular dependency with main.py.
#
# main.py  → imports from auth.py
# phase3.py → imports from auth.py
# No circular dependency.
#
# NOTHING in this file has been modified from the original main.py logic.
# Pure extraction — identical behaviour guaranteed.
# ---------------------------------------------------------------------------

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


def set_session_cookies(response, token: str) -> None:
    """Set both auth cookies consistently.

    The frontend now runs on a different origin than this API (no more
    same-origin Express BFF sitting in front of it), so the browser treats
    every request as cross-site. Cross-site cookies REQUIRE
    SameSite=None + Secure=True — Lax/False (the old default) is silently
    dropped by the browser on a cross-origin response, even though curl
    (which doesn't enforce SameSite/Secure at all) shows the cookie was
    sent just fine. That's why `curl` "works" but the browser stays
    logged out.

    IMPORTANT: Secure=True cookies are only ever set by browsers over
    HTTPS. Until this API is served over HTTPS, SameSite=None cookies
    will NOT be set by the browser no matter what this function does —
    there is no code-only fix for that half. Once HTTPS is live, this
    already defaults to the correct cross-site-safe settings.

    Override via env if needed:
      COOKIE_SAMESITE=lax|strict|none   (default: none)
      COOKIE_SECURE=true|false          (default: true)
    """
    samesite = os.getenv("COOKIE_SAMESITE", "none").lower()
    secure_env = os.getenv("COOKIE_SECURE")
    secure = (secure_env.lower() == "true") if secure_env is not None else True

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
    """Dependency: extract and verify the session cookie, return the User."""
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
