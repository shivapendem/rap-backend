# email_queue.py
# ---------------------------------------------------------------------------
# Email Queue endpoints
# Handles consultant email queue management
# ---------------------------------------------------------------------------
import os
import uuid
from fastapi import UploadFile, File

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from auth import get_current_user
from models import User

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class EmailQueueCreateRequest(BaseModel):
    consultant_id: Optional[int] = None
    requirement_id: Optional[int] = None
    from_email: str
    to_email: str
    subject: str
    content: Optional[str] = None
    attachments: Optional[List[str]] = None

    @field_validator('requirement_id', 'consultant_id', mode='before')
    @classmethod
    def zero_to_none(cls, v):
        """Frontend sends Number('') = 0 for unset IDs — treat 0 as None."""
        if v == 0 or v == '' or v is None:
            return None
        return v


class EmailQueueStatusUpdate(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _assert_email_queue_access(db: AsyncSession, current_user: User, item) -> None:
    """
    BUG FIX: get/update-status/delete on a single email-queue item had NO
    ownership or role check at all — any authenticated user could view,
    change the status of, or delete ANY other consultant's queued
    application email just by knowing the item id. list/create already
    scoped correctly by role; this brings the single-item endpoints in
    line with that same scoping.
    """
    from models import Consultant
    if current_user.role in ("ADMIN", "RECRUITER"):
        return
    if current_user.role == "CONSULTANT":
        result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        consultant = result.scalars().first()
        if consultant and item.consultant_id == consultant.id:
            return
    raise HTTPException(status_code=403, detail="Insufficient permissions for this email queue item.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/api/consultant/email-queue")
async def create_email_queue(
    body: EmailQueueCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add email to queue."""
    from models import EmailQueue
    from models import Consultant
    from sqlalchemy import select as sa_select

    consultant_id = None

    if current_user.role == "ADMIN":
        # Admin must provide consultant_id explicitly
        if not body.consultant_id:
            raise HTTPException(status_code=400, detail="Admin must specify consultant_id.")
        # Verify the consultant exists
        cons_result = await db.execute(
            sa_select(Consultant).where(Consultant.id == body.consultant_id)
        )
        consultant = cons_result.scalars().first()
        if not consultant:
            raise HTTPException(status_code=404, detail="Consultant not found.")
        consultant_id = consultant.id
    else:
        # Consultant: resolve from logged-in user
        cons_result = await db.execute(
            sa_select(Consultant).where(Consultant.user_id == current_user.id)
        )
        consultant = cons_result.scalars().first()
        consultant_id = consultant.id if consultant else body.consultant_id
        if not consultant_id:
            raise HTTPException(status_code=400, detail="Consultant profile not found.")

    item = EmailQueue(
        consultant_id=consultant_id,
        requirement_id=body.requirement_id,
        from_email=body.from_email,
        to_email=body.to_email,
        subject=body.subject,
        content=body.content,
        attachments=body.attachments,
        status="QUEUED",
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return {
        "success": True,
        "id": str(item.id),
        "status": item.status,
    }


@router.get("/api/consultant/email-queue")
async def list_email_queue(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all emails in queue."""
    from models import EmailQueue, Consultant
    
    query = select(EmailQueue)
    
    if current_user.role == "CONSULTANT":
        result = await db.execute(select(Consultant).where(Consultant.user_id == current_user.id))
        consultant = result.scalars().first()
        if not consultant:
            return {"data": [], "total": 0}
        query = query.where(EmailQueue.consultant_id == consultant.id)
    elif current_user.role != "ADMIN" and current_user.role != "RECRUITER":
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    result = await db.execute(query.order_by(EmailQueue.created_at.desc()))
    items = result.scalars().all()
    return {
        "data": [
            {
                "id": str(item.id),
                "consultant_id": str(item.consultant_id),
                "requirement_id": str(item.requirement_id) if item.requirement_id else None,
                "from_email": item.from_email,
                "to_email": item.to_email,
                "subject": item.subject,
                "content": item.content,
                "attachments": item.attachments,
                "status": item.status,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            }
            for item in items
        ],
        "total": len(items),
    }


@router.get("/api/consultant/email-queue/{item_id}")
async def get_email_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get single email queue item."""
    from models import EmailQueue
    result = await db.execute(
        select(EmailQueue).where(EmailQueue.id == item_id)
    )
    item = result.scalars().first()
    if not item:
        raise HTTPException(status_code=404, detail="Email queue item not found")
    await _assert_email_queue_access(db, current_user, item)
    return {
        "id": str(item.id),
        "consultant_id": str(item.consultant_id),
        "requirement_id": str(item.requirement_id) if item.requirement_id else None,
        "from_email": item.from_email,
        "to_email": item.to_email,
        "subject": item.subject,
        "content": item.content,
        "attachments": item.attachments,
        "status": item.status,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


@router.patch("/api/consultant/email-queue/{item_id}/status")
async def update_email_queue_status(
    item_id: int,
    body: EmailQueueStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update email queue item status."""
    from models import EmailQueue
    result = await db.execute(
        select(EmailQueue).where(EmailQueue.id == item_id)
    )
    item = result.scalars().first()
    if not item:
        raise HTTPException(status_code=404, detail="Email queue item not found")
    await _assert_email_queue_access(db, current_user, item)

    valid_statuses = {"QUEUED", "SENT", "FAILED"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {sorted(valid_statuses)}"
        )

    item.status = body.status
    await db.commit()
    return {"success": True, "id": str(item.id), "status": item.status}


@router.delete("/api/consultant/email-queue/{item_id}")
async def delete_email_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete email queue item."""
    from models import EmailQueue
    result = await db.execute(
        select(EmailQueue).where(EmailQueue.id == item_id)
    )
    item = result.scalars().first()
    if not item:
        raise HTTPException(status_code=404, detail="Email queue item not found")
    await _assert_email_queue_access(db, current_user, item)
    await db.delete(item)
    await db.commit()
    return {"success": True, "message": f"Email queue item {item_id} deleted"}

UPLOAD_DIR = "/tmp/email_attachments"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@router.post("/api/consultant/email-queue/upload-attachment")
async def upload_attachment(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload attachment file and return file reference."""
    ext = os.path.splitext(file.filename)[1]
    unique_name = f"{uuid.uuid4()}{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)
    contents = await file.read()
    with open(file_path, "wb") as f:
        f.write(contents)
    return {
        "success": True,
        "filename": file.filename,
        "stored_name": unique_name,
        "path": file_path,
        "size_bytes": len(contents),
        "content_type": file.content_type,
    }