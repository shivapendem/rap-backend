# email_queue.py
# ---------------------------------------------------------------------------
# Email Queue endpoints
# Handles consultant email queue management
# ---------------------------------------------------------------------------

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
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
    consultant_id: int
    requirement_id: Optional[int] = None
    from_email: str
    to_email: str
    subject: str
    content: Optional[str] = None
    attachments: Optional[List[str]] = None


class EmailQueueStatusUpdate(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/consultant/email-queue")
async def create_email_queue(
    body: EmailQueueCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add email to queue."""
    from models import EmailQueue
    item = EmailQueue(
        consultant_id=body.consultant_id,
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


@router.get("/consultant/email-queue")
async def list_email_queue(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all emails in queue."""
    from models import EmailQueue
    result = await db.execute(
        select(EmailQueue).order_by(EmailQueue.created_at.desc())
    )
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


@router.get("/consultant/email-queue/{item_id}")
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


@router.patch("/consultant/email-queue/{item_id}/status")
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

    valid_statuses = {"QUEUED", "SENT", "FAILED"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {sorted(valid_statuses)}"
        )

    item.status = body.status
    await db.commit()
    return {"success": True, "id": str(item.id), "status": item.status}


@router.delete("/consultant/email-queue/{item_id}")
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
    await db.delete(item)
    await db.commit()
    return {"success": True, "message": f"Email queue item {item_id} deleted"}