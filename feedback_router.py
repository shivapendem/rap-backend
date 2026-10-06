# ---------------------------------------------------------------------------
# Feedback API — every logged-in role (ADMIN / RECRUITER / CONSULTANT) can
# submit feedback and see their own; only ADMIN sees everything and changes
# status.
#
#   POST   /api/feedback                 submit (multipart, optional image)
#   GET    /api/feedback/mine            my feedback
#   GET    /api/feedback                 ADMIN: all feedback + counts
#   GET    /api/feedback/{id}            owner or ADMIN
#   PATCH  /api/feedback/{id}/status     ADMIN: OPEN | PENDING | RESOLVED
#   GET    /api/feedback/{id}/image      owner or ADMIN (streams the image)
#
# Images go to DO Spaces when configured, else uploads/feedback/ on disk.
# ---------------------------------------------------------------------------

from __future__ import annotations

import io
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import Feedback, User

router = APIRouter(prefix="/api/feedback", tags=["Feedback"])

TYPES = {"BUG", "IMPROVEMENT", "FEEDBACK", "OTHER"}
IMPACTS = {"MINOR", "SLOW", "STUCK"}
STATUSES = {"OPEN", "PENDING", "RESOLVED"}
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024

LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "feedback")


# ── helpers ────────────────────────────────────────────────────────────────

def _ref(fid: int) -> str:
    return f"FB-{fid:04d}"


def _clean(v: Optional[str], limit: int = 0) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    return v[:limit] if limit else v


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _to_dict(f: Feedback, full: bool = False) -> dict:
    d = {
        "id": f.id,
        "ref": _ref(f.id),
        "type": f.type,
        "title": f.title,
        "location": f.location,
        "status": f.status,
        "reporter_name": f.reporter_name,
        "reporter_role": f.reporter_role,
        "has_image": bool(f.image_key),
        "created_at": _iso(f.created_at),
        "updated_at": _iso(f.updated_at),
    }
    if full:
        d.update({
            "description": f.description,
            "impact": f.impact,
            "steps": f.steps,
            "expected": f.expected,
            "image_name": f.image_name,
            "page_url": f.page_url,
            "user_agent": f.user_agent,
            "screen": f.screen,
        })
    return d


def _require_admin(user: User) -> None:
    if user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")


async def _get_visible(db: AsyncSession, fid: int, user: User) -> Feedback:
    f = await db.get(Feedback, fid)
    if not f:
        raise HTTPException(status_code=404, detail="Feedback not found")
    if user.role != "ADMIN" and f.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not allowed")
    return f


def _store_image(data: bytes, ext: str, content_type: str) -> str:
    """Returns the stored key ('s3:...' or 'local:...'). Raises on failure."""
    name = f"{uuid.uuid4().hex}.{ext}"
    try:
        from s3_service import DO_SPACES_BUCKET, upload_file_to_s3
        if DO_SPACES_BUCKET:
            key = f"uploads/feedback/{name}"
            if upload_file_to_s3(io.BytesIO(data), key, content_type=content_type):
                return f"s3:{key}"
    except Exception as e:  # fall back to disk
        print(f"[feedback] S3 upload unavailable, saving locally: {e}")
    os.makedirs(LOCAL_DIR, exist_ok=True)
    with open(os.path.join(LOCAL_DIR, name), "wb") as fh:
        fh.write(data)
    return f"local:{name}"


def _load_image(key: str):
    if key.startswith("s3:"):
        from s3_service import download_file_from_s3
        body, _ = download_file_from_s3(key[3:])
        return body
    if key.startswith("local:"):
        name = os.path.basename(key[6:])  # never trust a path
        path = os.path.join(LOCAL_DIR, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read()
    return None


# ── endpoints ──────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_feedback(
    type: str = Form(...),
    title: str = Form(...),
    description: str = Form(...),
    location: Optional[str] = Form(None),
    impact: Optional[str] = Form(None),
    steps: Optional[str] = Form(None),
    expected: Optional[str] = Form(None),
    page_url: Optional[str] = Form(None),
    user_agent: Optional[str] = Form(None),
    screen: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ftype = (type or "").strip().upper()
    if ftype not in TYPES:
        raise HTTPException(status_code=422, detail="Choose Bug, Improvement, Feedback or Other.")
    title_v = _clean(title, 200)
    desc_v = _clean(description)
    if not title_v:
        raise HTTPException(status_code=422, detail="Please give it a short name.")
    if not desc_v:
        raise HTTPException(status_code=422, detail="Please tell us what happened.")

    impact_v = None
    if ftype == "BUG":
        impact_v = (impact or "SLOW").strip().upper()
        if impact_v not in IMPACTS:
            impact_v = "SLOW"

    image_key = image_name = image_ct = None
    if image is not None and image.filename:
        ct = (image.content_type or "").lower()
        if ct not in IMAGE_TYPES:
            raise HTTPException(status_code=422, detail="Only PNG, JPG, WEBP or GIF images are allowed.")
        data = await image.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=422, detail="Image must be 10 MB or smaller.")
        if data:
            image_key = await run_in_threadpool(_store_image, data, IMAGE_TYPES[ct], ct)
            image_name = os.path.basename(image.filename)[:200]
            image_ct = ct

    f = Feedback(
        user_id=current_user.id,
        reporter_name=current_user.full_name or current_user.email,
        reporter_role=current_user.role,
        type=ftype,
        title=title_v,
        location=_clean(location, 300),
        description=desc_v,
        impact=impact_v,
        steps=_clean(steps) if ftype == "BUG" else None,
        expected=_clean(expected) if ftype == "BUG" else None,
        image_key=image_key,
        image_name=image_name,
        image_content_type=image_ct,
        page_url=_clean(page_url, 500),
        user_agent=_clean(user_agent, 500),
        screen=_clean(screen, 50),
        status="OPEN",
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return _to_dict(f, full=True)


@router.get("/mine")
async def my_feedback(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Feedback).where(Feedback.user_id == current_user.id)
    if status_filter and status_filter.upper() in STATUSES:
        q = q.where(Feedback.status == status_filter.upper())
    rows = (await db.execute(q.order_by(Feedback.created_at.desc()).limit(200))).scalars().all()
    return [_to_dict(r) for r in rows]


@router.get("")
async def all_feedback(
    search: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)

    conds = []
    if date_from:
        conds.append(Feedback.created_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        conds.append(Feedback.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc))
    if search and search.strip():
        s = search.strip()
        like = f"%{s.lower()}%"
        sc = [
            func.lower(Feedback.title).like(like),
            func.lower(func.coalesce(Feedback.reporter_name, "")).like(like),
        ]
        digits = s.upper().replace("FB-", "").strip()
        if digits.isdigit():
            sc.append(Feedback.id == int(digits))
        conds.append(or_(*sc))

    # Counts honour search/date but not the status filter (cards show the split).
    count_rows = (await db.execute(
        select(Feedback.status, func.count()).where(*conds).group_by(Feedback.status)
    )).all()
    counts = {"OPEN": 0, "PENDING": 0, "RESOLVED": 0}
    for st, n in count_rows:
        if st in counts:
            counts[st] = n

    list_conds = list(conds)
    if status_filter and status_filter.upper() in STATUSES:
        list_conds.append(Feedback.status == status_filter.upper())

    total = (await db.execute(select(func.count()).select_from(Feedback).where(*list_conds))).scalar_one()
    rows = (await db.execute(
        select(Feedback).where(*list_conds)
        .order_by(Feedback.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    return {
        "items": [_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "counts": {
            "open": counts["OPEN"],
            "pending": counts["PENDING"],
            "resolved": counts["RESOLVED"],
            "total": sum(counts.values()),
        },
    }


@router.get("/{feedback_id}")
async def get_feedback(
    feedback_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    f = await _get_visible(db, feedback_id, current_user)
    return _to_dict(f, full=True)


class StatusUpdate(BaseModel):
    status: str


@router.patch("/{feedback_id}/status")
async def update_status(
    feedback_id: int,
    body: StatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    new_status = (body.status or "").strip().upper()
    if new_status not in STATUSES:
        raise HTTPException(status_code=422, detail="Status must be Open, Pending or Resolved.")
    f = await db.get(Feedback, feedback_id)
    if not f:
        raise HTTPException(status_code=404, detail="Feedback not found")
    f.status = new_status
    await db.commit()
    await db.refresh(f)
    return _to_dict(f)


@router.get("/{feedback_id}/image")
async def get_feedback_image(
    feedback_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    f = await _get_visible(db, feedback_id, current_user)
    if not f.image_key:
        raise HTTPException(status_code=404, detail="No image")
    data = await run_in_threadpool(_load_image, f.image_key)
    if not data:
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(
        content=data,
        media_type=f.image_content_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=300"},
    )