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
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user