# =============================================================
# Phase 2 - Task 5: Exact Deduplication Engine
# Detects duplicate requirements using vendor_email + role + jd_hash
# =============================================================

import hashlib
import re
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from models import Requirement


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
) -> bool:
    """
    Check if requirement already exists in database.
    Returns True if duplicate, False if new.
    """
    dedup_key = build_dedup_key(vendor_email, role, jd_hash)

    result = await db.execute(
        select(Requirement).where(Requirement.dedup_key == dedup_key)
    )
    return result.scalars().first() is not None


async def save_requirement(
    db: AsyncSession,
    parsed: dict,
    cleaned_jd: str,
    raw_email_id: Optional[int] = None,
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

    # Check for duplicate
    duplicate = await is_duplicate(db, vendor_email, role, jd_hash)
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
        parsed_fields=parsed,
        parse_confidence=parsed.get("parse_confidence", 0.0),
        status="NEW",
    )

    db.add(new_req)
    await db.commit()
    await db.refresh(new_req)

    return {"status": "saved", "id": new_req.id}
