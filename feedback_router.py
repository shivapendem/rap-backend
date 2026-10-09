# ---------------------------------------------------------------------------
# Feedback API — every logged-in role (ADMIN / RECRUITER / CONSULTANT) can
# submit feedback and see their own; only ADMIN sees everything and changes
# status.
#
#   POST   /api/feedback                 submit (multipart, up to 5 images)
#   GET    /api/feedback/mine            my feedback
#   GET    /api/feedback                 ADMIN: all feedback + counts
#   GET    /api/feedback/{id}            owner or ADMIN
#   PATCH  /api/feedback/{id}/status     ADMIN: OPEN | PENDING | RESOLVED
#   GET    /api/feedback/{id}/image      owner or ADMIN (streams the first/legacy image)
#   GET    /api/feedback/{id}/images/{image_id}  owner or ADMIN (streams one image; 0 = legacy)
#
# Images go to DO Spaces when configured, else uploads/feedback/ on disk.
# ---------------------------------------------------------------------------

from __future__ import annotations

import io
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import Feedback, FeedbackImage, User

router = APIRouter(prefix="/api/feedback", tags=["Feedback"])

TYPES = {"BUG", "IMPROVEMENT", "FEEDBACK", "OTHER"}
IMPACTS = {"MINOR", "SLOW", "STUCK"}
STATUSES = {"OPEN", "PENDING", "RESOLVED"}
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 5

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


async def _image_list(db: AsyncSession, f: Feedback) -> list:
    """All images of a feedback as [{id, name, content_type}]. Rows from
    before multi-image support (single image in feedback.image_*) are
    returned as one entry with id 0."""
    rows = (await db.execute(
        select(FeedbackImage).where(FeedbackImage.feedback_id == f.id)
        .order_by(FeedbackImage.position, FeedbackImage.id)
    )).scalars().all()
    if rows:
        return [{"id": r.id, "name": r.image_name, "content_type": r.image_content_type} for r in rows]
    if f.image_key:
        return [{"id": 0, "name": f.image_name, "content_type": f.image_content_type}]
    return []


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
    images: List[UploadFile] = File(default=[]),
    image: Optional[UploadFile] = File(None),  # legacy single-image field
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

    # Snapshot the reporter, then release the pooled DB connection before the
    # (possibly slow) image upload so other requests aren't kept waiting.
    reporter_id = current_user.id
    reporter_name = current_user.full_name or current_user.email
    reporter_role = current_user.role
    await db.rollback()

    impact_v = None
    if ftype == "BUG":
        impact_v = (impact or "SLOW").strip().upper()
        if impact_v not in IMPACTS:
            impact_v = "SLOW"

    # Validate every image BEFORE storing any, so a bad 4th file doesn't
    # leave 3 orphaned uploads behind.
    uploads = [u for u in (images or []) if u is not None and u.filename]
    if image is not None and image.filename:
        uploads.append(image)
    if len(uploads) > MAX_IMAGES:
        raise HTTPException(status_code=422, detail=f"You can attach up to {MAX_IMAGES} images.")
    prepared = []  # (data, ext, content_type, name)
    for u in uploads:
        ct = (u.content_type or "").lower()
        if ct not in IMAGE_TYPES:
            raise HTTPException(status_code=422, detail="Only PNG, JPG, WEBP or GIF images are allowed.")
        data = await u.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=422, detail="Each image must be 10 MB or smaller.")
        if data:
            prepared.append((data, IMAGE_TYPES[ct], ct, os.path.basename(u.filename)[:200]))

    stored = []  # (key, name, content_type)
    for data, ext, ct, name in prepared:
        key = await run_in_threadpool(_store_image, data, ext, ct)
        stored.append((key, name, ct))
    # The first image is also kept in the legacy columns (cover image /
    # has_image flag / older clients).
    image_key, image_name, image_ct = stored[0] if stored else (None, None, None)

    f = Feedback(
        user_id=reporter_id,
        reporter_name=reporter_name,
        reporter_role=reporter_role,
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
    await db.flush()  # assigns f.id for the image rows
    for pos, (key, name, ct) in enumerate(stored):
        db.add(FeedbackImage(
            feedback_id=f.id, image_key=key, image_name=name,
            image_content_type=ct, position=pos,
        ))
    await db.commit()
    await db.refresh(f)
    out = _to_dict(f, full=True)
    out["images"] = await _image_list(db, f)
    return out


@router.get("/mine")
async def my_feedback(
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The signed-in user's own feedback, newest first, 50 per page."""
    conds = [Feedback.user_id == current_user.id]
    if status_filter and status_filter.upper() in STATUSES:
        conds.append(Feedback.status == status_filter.upper())
    total = (await db.execute(select(func.count()).select_from(Feedback).where(*conds))).scalar_one()
    rows = (await db.execute(
        select(Feedback).where(*conds)
        .order_by(Feedback.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {"items": [_to_dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("")
async def all_feedback(
    search: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
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
        total = counts[status_filter.upper()]
    else:
        total = sum(counts.values())
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
    out = _to_dict(f, full=True)
    out["images"] = await _image_list(db, f)
    return out


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
    key, ctype = f.image_key, f.image_content_type
    # Free the pooled DB connection before the storage download.
    await db.rollback()
    data = await run_in_threadpool(_load_image, key)
    if not data:
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(
        content=data,
        media_type=ctype or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/{feedback_id}/images/{image_id}")
async def get_feedback_image_by_id(
    feedback_id: int,
    image_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    f = await _get_visible(db, feedback_id, current_user)
    if image_id == 0:
        # Feedback submitted before multi-image support.
        key, ctype = f.image_key, f.image_content_type
    else:
        row = await db.get(FeedbackImage, image_id)
        if not row or row.feedback_id != f.id:
            raise HTTPException(status_code=404, detail="No image")
        key, ctype = row.image_key, row.image_content_type
    if not key:
        raise HTTPException(status_code=404, detail="No image")
    # Free the pooled DB connection before the storage download.
    await db.rollback()
    data = await run_in_threadpool(_load_image, key)
    if not data:
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(
        content=data,
        media_type=ctype or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=86400"},
    )