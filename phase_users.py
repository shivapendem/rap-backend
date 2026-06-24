# phase_users.py
# ---------------------------------------------------------------------------
# Admin User Management — Router
#
# Matches the original user_mgmt_backend's 14 endpoints exactly, but reads/
# writes your REAL User/Consultant/RecruiterConsultant tables and logs to
# your REAL Phase 8 audit_logs table — no separate database, no duplicate
# schema. Admin-only — every route requires role == "ADMIN".
# ---------------------------------------------------------------------------

from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from phase_users_schema import (
    PaginatedUsersDTO, UserAdminRowDTO,
    CreateUserRequestDTO, CreateUserResponseDTO,
    EditUserRequestDTO,
    UpdateUserStatusRequestDTO, UpdateStatusResponseDTO,
    ConsultantAdminRowDTO,
    AssignConsultantRequestDTO, AssignConsultantResponseDTO,
    UpdateRecruiterConsultantsRequestDTO, UpdateRecruiterConsultantsResponseDTO,
    UpdateConsultantRequestDTO, UpdateConsultantResponseDTO,
)
from phase_users_service import UserService, ConsultantAssignmentService

# Reuse the exact same require_admin dependency defined in phase8.py
from phase8 import require_admin

router = APIRouter(prefix="/api/v1/admin", tags=["Admin User Management"])


# ===========================================================================
# USER CRUD
# ===========================================================================

@router.get("/users", response_model=PaginatedUsersDTO)
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    sort_by: str = Query("full_name"),
    sort_dir: str = Query("asc"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    if role and role not in {"ADMIN", "RECRUITER", "CONSULTANT"}:
        raise HTTPException(status_code=422, detail="Invalid role filter")
    if status and status not in {"Active", "Inactive"}:
        raise HTTPException(status_code=422, detail="Invalid status filter")
    if sort_dir not in {"asc", "desc"}:
        raise HTTPException(status_code=422, detail="sort_dir must be 'asc' or 'desc'")

    return await UserService.list_users(
        db, page=page, page_size=page_size, sort_by=sort_by, sort_dir=sort_dir,
        search=search, role=role, status=status,
    )


@router.post("/users", response_model=CreateUserResponseDTO, status_code=201)
async def create_user(
    body: CreateUserRequestDTO,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    user = await UserService.create_user(db, body, admin_id=current_user.get("sub"))
    return CreateUserResponseDTO(
        success=True, user=user,
        message=f"{body.role.capitalize()} created successfully.",
    )


@router.get("/users/{user_id}", response_model=UserAdminRowDTO)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return await UserService.get_user(db, user_id)


@router.put("/users/{user_id}", response_model=CreateUserResponseDTO)
async def update_user(
    user_id: int,
    body: EditUserRequestDTO,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    user = await UserService.update_user(db, user_id, body, admin_id=current_user.get("sub"))
    return CreateUserResponseDTO(success=True, user=user, message="User updated successfully.")


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    await UserService.delete_user(db, user_id, admin_id=current_user.get("sub"))
    return {"success": True, "message": "User deleted successfully."}


# ===========================================================================
# STATUS MANAGEMENT
# ===========================================================================

@router.put("/users/{user_id}/status", response_model=UpdateStatusResponseDTO)
async def update_user_status(
    user_id: int,
    body: UpdateUserStatusRequestDTO,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    uid, new_status = await UserService.update_status(
        db, user_id, body.status, admin_id=current_user.get("sub"),
    )
    return UpdateStatusResponseDTO(
        success=True, message=f"User status updated to {new_status}.",
        user_id=uid, new_status=new_status,
    )


@router.post("/users/{user_id}/deactivate", response_model=UpdateStatusResponseDTO)
async def deactivate_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    uid, new_status = await UserService.deactivate(db, user_id, admin_id=current_user.get("sub"))
    return UpdateStatusResponseDTO(
        success=True, message="User deactivated.", user_id=uid, new_status=new_status,
    )


@router.post("/users/{user_id}/activate", response_model=UpdateStatusResponseDTO)
async def activate_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    uid, new_status = await UserService.activate(db, user_id, admin_id=current_user.get("sub"))
    return UpdateStatusResponseDTO(
        success=True, message="User reactivated.", user_id=uid, new_status=new_status,
    )


# ===========================================================================
# CONSULTANT ASSIGNMENT
# ===========================================================================

@router.post("/users/{user_id}/assign-consultant", response_model=AssignConsultantResponseDTO)
async def assign_consultant(
    user_id: int,
    body: AssignConsultantRequestDTO,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    consultant_id = int(body.consultant_id)
    await ConsultantAssignmentService.assign_consultant(
        db, recruiter_user_id=user_id, consultant_id=consultant_id,
        admin_id=current_user.get("sub"),
    )
    return AssignConsultantResponseDTO(
        success=True, message="Consultant assigned.", consultant_id=str(consultant_id),
    )


@router.get("/consultants", response_model=List[ConsultantAdminRowDTO])
async def list_all_consultants(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return await ConsultantAssignmentService.list_consultants(db)


@router.delete("/users/{user_id}/consultant")
async def unassign_consultant(
    user_id: int,
    consultant_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    await ConsultantAssignmentService.unassign_consultant(
        db, recruiter_user_id=user_id, consultant_id=consultant_id,
        admin_id=current_user.get("sub"),
    )
    return {"success": True, "message": "Consultant unassigned."}


@router.put("/recruiters/{recruiter_id}/consultants", response_model=UpdateRecruiterConsultantsResponseDTO)
async def update_recruiter_consultants(
    recruiter_id: int,
    body: UpdateRecruiterConsultantsRequestDTO,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from sqlalchemy.future import select
    from models import Consultant
    raw_ids = [int(c) for c in body.consultant_ids]
    result = await db.execute(
        select(Consultant.id).where(Consultant.user_id.in_(raw_ids))
    )
    consultant_ids = [row[0] for row in result.fetchall()]
    await ConsultantAssignmentService.replace_assignments(
        db, recruiter_user_id=recruiter_id, consultant_ids=consultant_ids,
        admin_id=current_user.get("sub"),
    )
    return UpdateRecruiterConsultantsResponseDTO(
        success=True, message="Consultant assignments updated.",
    )


# ===========================================================================
# MANAGE CONSULTANTS
# ===========================================================================

@router.get("/consultants/{consultant_id}", response_model=ConsultantAdminRowDTO)
async def get_consultant(
    consultant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return await ConsultantAssignmentService.get_consultant(db, consultant_id)


@router.put("/consultants/{consultant_id}", response_model=UpdateConsultantResponseDTO)
async def update_consultant(
    consultant_id: int,
    body: UpdateConsultantRequestDTO,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    consultant = await ConsultantAssignmentService.update_consultant(
        db, consultant_id,
        primary_skills=body.primary_skills,
        availability_status=body.availability_status,
        status=body.status,
        admin_id=current_user.get("sub"),
    )
    return UpdateConsultantResponseDTO(
        success=True, message="Consultant profile updated.", consultant=consultant,
    )