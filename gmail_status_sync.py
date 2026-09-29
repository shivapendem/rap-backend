# gmail_status_sync.py
# ---------------------------------------------------------------------------
# Gmail connection lifecycle for consultants.
#
#   Consultant logs in        -> token saved / refreshed, is_active = TRUE
#                                -> roster shows "Connected"
#   Consultant logs out       -> is_active = FALSE (token kept, NOT deleted)
#   Consultant made INACTIVE  -> is_active = FALSE     -> roster "Not Connected",
#   User deauthorized         -> is_active = FALSE        sending is blocked
#   Consultant logs back in   -> token refreshed, is_active = TRUE -> "Connected"
#   Google rejects refresh    -> row deleted (existing email-queue behaviour)
#
# consultants.gmail_connected is kept equal to
#     EXISTS(token row for this consultant WHERE is_active)
# by PostgreSQL triggers, so it updates immediately no matter who changes the
# data: backend, cron worker, or a manual edit in pgAdmin. No restart needed.
#
# Why pause instead of delete on logout: the refresh token is what lets a
# consultant's access come back on re-login (including password login)
# without going through Google consent again. Deleting it would force a
# fresh "Sign in with Google" every single time.
#
# models.py registers ORM listeners doing the same work, as a fallback for
# SQLite dev or a DB user without permission to create triggers.
# ---------------------------------------------------------------------------

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

# Shown on failed queue items / blocked sends. Deliberately contains none of
# the words the email-queue failure handler matches on ("token",
# "credential", "unauthorized", "invalid_grant", "401") and no email address,
# so a paused connection is never mistaken for a dead one and deleted.
GMAIL_PAUSED_MESSAGE = (
    "Gmail sending is paused for this consultant because they are logged out "
    "or marked inactive. It resumes automatically when the consultant logs in again."
)

# ---------------------------------------------------------------------------
# Schema: consultant_email_tokens.is_active
# ---------------------------------------------------------------------------

async def ensure_gmail_token_schema(engine) -> None:
    """Add consultant_email_tokens.is_active if missing. Idempotent; never raises."""
    try:
        async with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                await conn.execute(text(
                    "ALTER TABLE consultant_email_tokens "
                    "ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE"
                ))
            else:
                cols = (await conn.execute(text("PRAGMA table_info(consultant_email_tokens)"))).fetchall()
                if cols and "is_active" not in {c[1] for c in cols}:
                    await conn.execute(text(
                        "ALTER TABLE consultant_email_tokens "
                        "ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"
                    ))
    except Exception as exc:
        print(f"[gmail-sync] Could not ensure is_active column: {exc!r}")


async def pause_tokens_of_inactive_consultants(engine) -> None:
    """One-time/boot reconciliation: consultants already INACTIVE (or whose user
    is deauthorized) before this fix was deployed get their access paused."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                UPDATE consultant_email_tokens SET is_active = FALSE
                WHERE is_active = TRUE AND consultant_id IN (
                    SELECT c.id FROM consultants c
                    LEFT JOIN users u ON u.id = c.user_id
                    WHERE c.status = 'INACTIVE' OR u.is_authorized = FALSE
                )
            """))
            await conn.execute(text("""
                UPDATE consultants SET gmail_connected = CASE
                    WHEN id IN (SELECT consultant_id FROM consultant_email_tokens WHERE is_active = TRUE)
                    THEN TRUE ELSE FALSE END
            """))
    except Exception as exc:
        print(f"[gmail-sync] Could not pause tokens of inactive consultants: {exc!r}")

# ---------------------------------------------------------------------------
# PostgreSQL triggers
# ---------------------------------------------------------------------------

_FN_SYNC_FLAG = """
CREATE OR REPLACE FUNCTION rap_sync_consultant_gmail_connected()
RETURNS trigger AS $$
BEGIN
    IF TG_OP <> 'DELETE' THEN
        UPDATE consultants c
           SET gmail_connected = EXISTS (
                   SELECT 1 FROM consultant_email_tokens t
                    WHERE t.consultant_id = NEW.consultant_id AND t.is_active)
         WHERE c.id = NEW.consultant_id
           AND c.gmail_connected IS DISTINCT FROM EXISTS (
                   SELECT 1 FROM consultant_email_tokens t
                    WHERE t.consultant_id = NEW.consultant_id AND t.is_active);
    END IF;

    IF TG_OP = 'DELETE'
       OR (TG_OP = 'UPDATE' AND OLD.consultant_id IS DISTINCT FROM NEW.consultant_id) THEN
        UPDATE consultants c
           SET gmail_connected = EXISTS (
                   SELECT 1 FROM consultant_email_tokens t
                    WHERE t.consultant_id = OLD.consultant_id AND t.is_active)
         WHERE c.id = OLD.consultant_id;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_FN_PAUSE_ON_CONSULTANT_INACTIVE = """
CREATE OR REPLACE FUNCTION rap_pause_gmail_on_consultant_inactive()
RETURNS trigger AS $$
BEGIN
    UPDATE consultant_email_tokens SET is_active = FALSE
     WHERE consultant_id = NEW.id AND is_active = TRUE;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_FN_PAUSE_ON_USER_DEAUTHORIZED = """
CREATE OR REPLACE FUNCTION rap_pause_gmail_on_user_deauthorized()
RETURNS trigger AS $$
BEGIN
    UPDATE consultant_email_tokens SET is_active = FALSE
     WHERE is_active = TRUE
       AND consultant_id IN (SELECT id FROM consultants WHERE user_id = NEW.id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_TRIGGERS = [
    ("trg_rap_sync_consultant_gmail_connected", "consultant_email_tokens",
     "AFTER INSERT OR UPDATE OR DELETE ON consultant_email_tokens FOR EACH ROW "
     "EXECUTE FUNCTION rap_sync_consultant_gmail_connected()"),
    ("trg_rap_pause_gmail_on_consultant_inactive", "consultants",
     "AFTER UPDATE OF status ON consultants FOR EACH ROW "
     "WHEN (NEW.status = 'INACTIVE' AND OLD.status IS DISTINCT FROM NEW.status) "
     "EXECUTE FUNCTION rap_pause_gmail_on_consultant_inactive()"),
    ("trg_rap_pause_gmail_on_user_deauthorized", "users",
     "AFTER UPDATE OF is_authorized ON users FOR EACH ROW "
     "WHEN (NEW.is_authorized = FALSE AND OLD.is_authorized IS DISTINCT FROM NEW.is_authorized) "
     "EXECUTE FUNCTION rap_pause_gmail_on_user_deauthorized()"),
]

# Constant key so several workers (backend + cron) booting at once don't race.
_ADVISORY_LOCK_KEY = 815_204_771


async def install_gmail_connected_trigger(engine) -> None:
    """Idempotently (re)create all Gmail-status triggers. PostgreSQL only; never raises."""
    if engine.dialect.name != "postgresql":
        print("[gmail-sync] Non-PostgreSQL database — skipping triggers (ORM listeners still active).")
        return
    try:
        async with engine.begin() as conn:
            # asyncpg runs one statement per call, so execute them separately.
            await conn.execute(text(f"SELECT pg_advisory_xact_lock({_ADVISORY_LOCK_KEY})"))
            for fn_sql in (_FN_SYNC_FLAG, _FN_PAUSE_ON_CONSULTANT_INACTIVE, _FN_PAUSE_ON_USER_DEAUTHORIZED):
                await conn.execute(text(fn_sql))
            for name, table, body in _TRIGGERS:
                await conn.execute(text(f"DROP TRIGGER IF EXISTS {name} ON {table}"))
                await conn.execute(text(f"CREATE TRIGGER {name} {body}"))
        print("[gmail-sync] Gmail status triggers installed.")
    except Exception as exc:
        print(f"[gmail-sync] Could not install Gmail status triggers: {exc!r}")


async def prepare_gmail_status_sync(engine) -> None:
    """Everything needed at process start, in the right order."""
    await ensure_gmail_token_schema(engine)
    await install_gmail_connected_trigger(engine)
    await pause_tokens_of_inactive_consultants(engine)

# ---------------------------------------------------------------------------
# Logout: pause access
# ---------------------------------------------------------------------------

async def pause_consultant_gmail(db, user_email: str) -> None:
    """Called on logout. Pauses (does not delete) the consultant's Gmail token."""
    from sqlalchemy.future import select
    from models import User, Consultant, ConsultantEmailToken

    user = (await db.execute(select(User).where(User.email == user_email))).scalars().first()
    if not user or user.role != "CONSULTANT":
        return
    consultant = (await db.execute(
        select(Consultant).where(Consultant.user_id == user.id)
    )).scalars().first()
    if not consultant:
        return
    token = (await db.execute(
        select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant.id)
    )).scalars().first()
    if token and token.is_active:
        token.is_active = False
    consultant.gmail_connected = False
    await db.commit()
    print(f"[gmail-sync] Gmail access paused on logout for consultant {consultant.id}.")

# ---------------------------------------------------------------------------
# Re-login: refresh and resume access
# ---------------------------------------------------------------------------

async def refresh_consultant_gmail_token_on_login(user_id: int) -> None:
    """
    Background task after a consultant logs in. Resumes a paused token and
    refreshes it from the saved refresh token when the access token has
    expired. Uses its own DB session and never raises.

    If Google rejects the refresh token, the connection stays paused
    ("Not Connected") and the consultant must use "Sign in with Google" to
    reconnect. Nothing is deleted here.
    """
    try:
        import httpx
        from database import AsyncSessionLocal
        from sqlalchemy.future import select
        from models import Consultant, ConsultantEmailToken
        from gmail_send_service import encrypt_token, decrypt_token

        async with AsyncSessionLocal() as session:
            consultant = (await session.execute(
                select(Consultant).where(Consultant.user_id == user_id)
            )).scalars().first()
            if not consultant or consultant.status == "INACTIVE":
                return

            token = (await session.execute(
                select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant.id)
            )).scalars().first()
            if not token:
                return  # never connected — needs "Sign in with Google"

            now = datetime.now(timezone.utc)
            expiry = token.token_expiry
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)

            if expiry is not None and now < (expiry - timedelta(minutes=5)):
                # Access token still valid: just resume.
                token.is_active = True
                consultant.gmail_connected = True
                await session.commit()
                print(f"[gmail-sync] Gmail access resumed on login for consultant {consultant.id}.")
                return

            if not token.refresh_token_encrypted:
                print(f"[gmail-sync] Consultant {consultant.id}: access expired and no refresh "
                      f"available — needs 'Sign in with Google'.")
                return

            client_id = os.getenv("GOOGLE_CLIENT_ID")
            client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
            if not client_id or not client_secret:
                return

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": decrypt_token(token.refresh_token_encrypted),
                        "grant_type": "refresh_token",
                    },
                )

            if res.status_code != 200:
                print(f"[gmail-sync] Login-time refresh rejected for consultant {consultant.id} "
                      f"(status={res.status_code}). Left as-is; consultant must 'Sign in with Google'.")
                return

            new_data = res.json()
            token.access_token_encrypted = encrypt_token(new_data["access_token"])
            if new_data.get("refresh_token"):
                token.refresh_token_encrypted = encrypt_token(new_data["refresh_token"])
            token.token_expiry = now + timedelta(seconds=new_data.get("expires_in", 3599))
            token.is_active = True
            consultant.gmail_connected = True
            await session.commit()
            print(f"[gmail-sync] Gmail token refreshed and resumed on login for consultant {consultant.id}.")
    except Exception as exc:
        print(f"[gmail-sync] Login-time Gmail refresh failed (ignored): {exc!r}")