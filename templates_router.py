from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from database import get_db
from models import MessageTemplate
from auth import get_current_user
from pydantic import BaseModel

router = APIRouter(prefix="/api/templates", tags=["Templates"])

class MessageTemplateResponse(BaseModel):
    id: int
    name: str
    content: str

    model_config = {"from_attributes": True}

@router.get("/", response_model=list[MessageTemplateResponse])
async def get_templates(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    result = await db.execute(select(MessageTemplate).order_by(MessageTemplate.id))
    templates = result.scalars().all()
    return templates
