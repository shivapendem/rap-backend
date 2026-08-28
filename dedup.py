# =============================================================
# Phase 2 - Task 5: Exact Deduplication Engine
# Detects duplicate requirements using vendor_email + role + jd_hash
# =============================================================

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from models import Requirement

# BUG FIX (duplicate requirements not caught by exact dedup_key match):
# jd_hash is an exact SHA256 of the JD text, so it only catches
# byte-for-byte identical re-sends. In practice, vendors very commonly
# "bump"/resend the same job with a tiny wording change (an added
# "URGENT", an updated line, a fresh timestamp in the signature) — the
# hash changes completely even though it's the same job, so it silently
# created a second Requirement row. Two independent match rows then get
# scored against every candidate, and rejecting one match has zero
# effect on the other since they belong to unrelated requirement_ids.
# This window catches "same vendor + same role, arrived close together
# in time" as a duplicate too, even when the JD text isn't identical.
NEAR_DUPLICATE_WINDOW_HOURS = 72


def normalize_text(value: str) -> str:
    """Normalize text for comparison."""
    value = (value or "").lower().strip()
    value = re.sub(r'\s+', ' ', value)
    return value


def create_jd_hash(cleaned_jd: str) -> str:
    """
    Create SHA256 hash of cleaned JD text.
    Same JD always produces same hash.
    """
    normalized = normalize_text(cleaned_jd)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_dedup_key(vendor_email: str, role: str, jd_hash: str) -> str:
    """
    Build unique deduplication key.
    Format: vendor_email|role|jd_hash
    """
    return f"{normalize_text(vendor_email)}|{normalize_text(role)}|{jd_hash}"


async def is_duplicate(
    db: AsyncSession,
    vendor_email: str,
    role: str,
    jd_hash: str,
    received_date=None,
) -> bool:
    """
    Check if requirement already exists in database.
    Returns True if duplicate, False if new.

    Two checks, either of which counts as a duplicate:
      1. Exact match: identical vendor_email + role + jd_hash (byte-for-byte
         same JD text). Catches true re-sends/forwards.
      2. Near match: same vendor_email + role arriving within
         NEAR_DUPLICATE_WINDOW_HOURS of an existing requirement, even if
         the JD text differs slightly. Catches the much more common case
         of a vendor bumping/resending the same job with minor wording
         changes.
    """
    dedup_key = build_dedup_key(vendor_email, role, jd_hash)

    result = await db.execute(
        select(Requirement).where(Requirement.dedup_key == dedup_key)
    )
    if result.scalars().first() is not None:
        return True

    norm_vendor = normalize_text(vendor_email)
    norm_role = normalize_text(role)
    # Don't near-match on missing/placeholder values — that would risk
    # merging genuinely different jobs that both failed to parse a
    # vendor or role, which is a worse outcome than an occasional
    # uncaught duplicate.
    if not norm_vendor or norm_vendor == "unknown@unknown.com" or not norm_role or norm_role == "unknown":
        return False

    anchor_time = received_date if isinstance(received_date, datetime) else datetime.now(timezone.utc)
    if anchor_time.tzinfo is None:
        anchor_time = anchor_time.replace(tzinfo=timezone.utc)
    window_start = anchor_time - timedelta(hours=NEAR_DUPLICATE_WINDOW_HOURS)
    window_end = anchor_time + timedelta(hours=NEAR_DUPLICATE_WINDOW_HOURS)

    near_result = await db.execute(
        select(Requirement.id).where(
            func.lower(func.trim(Requirement.vendor_email)) == norm_vendor,
            func.lower(func.trim(Requirement.role)) == norm_role,
            Requirement.created_at >= window_start,
            Requirement.created_at <= window_end,
        ).limit(1)
    )
    return near_result.scalars().first() is not None


async def save_requirement(
    db: AsyncSession,
    parsed: dict,
    cleaned_jd: str,
    raw_email_id: Optional[int] = None,
    received_date=None,  # BUG FIX: this was accepted nowhere before — column stayed NULL forever
) -> dict:
    """
    Save requirement to database if not duplicate.
    Returns {'status': 'saved'|'duplicate', 'id': ...}
    """
    vendor_email = parsed.get("vendor_email", "unknown@unknown.com")
    role = parsed.get("role", "UNKNOWN")

    # Create JD hash
    jd_hash = create_jd_hash(cleaned_jd)

    # Build dedup key
    dedup_key = build_dedup_key(vendor_email, role, jd_hash)

    # received_date may arrive as an ISO string (from JSON payloads) or a
    # datetime already (from gmail_emails.date) — normalize to datetime or
    # None. Moved ahead of the duplicate check (was previously done after)
    # so is_duplicate's near-duplicate time-window check gets a real
    # datetime to anchor on instead of always falling back to "now".
    if isinstance(received_date, str):
        try:
            received_date = datetime.fromisoformat(received_date.replace("Z", "+00:00"))
        except ValueError:
            received_date = None

    # Check for duplicate
    duplicate = await is_duplicate(db, vendor_email, role, jd_hash, received_date=received_date)
    if duplicate:
        return {"status": "duplicate", "id": None}

    # Save new requirement — persist jd_hash and dedup_key so future duplicate checks work
    new_req = Requirement(
        raw_email_id=raw_email_id,
        role=role,
        vendor=parsed.get("vendor"),
        vendor_email=vendor_email,
        vendor_contact=parsed.get("vendor_contact"),
        client=parsed.get("client"),
        location=parsed.get("location"),
        work_mode=parsed.get("work_mode"),
        employment_types=parsed.get("employment_types", ["UNKNOWN"]),
        rate=parsed.get("rate"),
        duration=parsed.get("duration"),
        job_description=cleaned_jd,
        jd_hash=jd_hash,
        dedup_key=dedup_key,
        parsed_fields={
            # NOTE: parsed.get("parsed_fields") used to be spread in here,
            # but parse_requirement() never actually returns a nested
            # "parsed_fields" key — that always resolved to {}, silently
            # dropping "skills" from every newly-synced requirement (it
            # only ever showed up after a manual Reparse, which builds
            # this dict differently). Pull both fields explicitly instead.
            "experience": parsed.get("experience"),
            "skills": parsed.get("skills"),
        },
        # BUG FIX: parser.py's parse_requirement() always computes a real
        # parse_confidence (see calculate_confidence()), but this
        # constructor never read it out of `parsed` — every auto-synced
        # requirement (the only path that creates new rows on an ongoing
        # basis) silently fell back to the parse_confidence column's
        # default of 0, showing 0% for every row regardless of actual
        # parse quality. Only a manual Reparse (phase2.py) ever set the
        # real value, since that path writes it explicitly.
        parse_confidence=parsed.get("parse_confidence", 0.0),
        received_date=received_date,
        status="NEW",
    )

    db.add(new_req)
    try:
        await db.commit()
        await db.refresh(new_req)
        return {"status": "saved", "id": new_req.id}
    except Exception as e:
        await db.rollback()
        import sqlalchemy.exc
        if isinstance(e, sqlalchemy.exc.IntegrityError) and "dedup_key" in str(e):
            return {"status": "duplicate", "id": None}
        raise