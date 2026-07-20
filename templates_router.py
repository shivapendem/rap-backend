from fastapi import APIRouter, Depends, HTTPException
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

class MessageTemplateCreate(BaseModel):
    name: str
    content: str

class MessageTemplateUpdate(BaseModel):
    name: str
    content: str

@router.get("/", response_model=list[MessageTemplateResponse])
async def get_templates(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    result = await db.execute(select(MessageTemplate).order_by(MessageTemplate.id))
    templates = result.scalars().all()
    return templates

@router.post("/", response_model=MessageTemplateResponse)
async def create_template(
    body: MessageTemplateCreate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Only admins can manage templates")
        
    template = MessageTemplate(name=body.name, content=body.content)
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template

@router.put("/{template_id}", response_model=MessageTemplateResponse)
async def update_template(
    template_id: int,
    body: MessageTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Only admins can manage templates")
        
    result = await db.execute(select(MessageTemplate).where(MessageTemplate.id == template_id))
    template = result.scalars().first()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
        
    template.name = body.name
    template.content = body.content
    await db.commit()
    await db.refresh(template)
    return template

@router.delete("/{template_id}")
async def delete_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Only admins can manage templates")
        
    result = await db.execute(select(MessageTemplate).where(MessageTemplate.id == template_id))
    template = result.scalars().first()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
        
    await db.delete(template)
    await db.commit()
    return {"success": True, "message": "Template deleted"}
