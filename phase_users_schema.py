# phase_users_schema.py
# ---------------------------------------------------------------------------
# Admin User Management — Pydantic schemas
# Adapted from the standalone user_mgmt_backend to use your real User/
# Consultant field names (full_name, password_hash, etc.)
# ---------------------------------------------------------------------------

from typing import Optional, List
from pydantic import BaseModel, EmailStr, field_validator, Field

VALID_ROLES = {"ADMIN", "RECRUITER", "CONSULTANT"}
VALID_STATUSES = {"Active", "Inactive"}
VALID_CONSULTANT_STATUSES = {"ACTIVE", "INACTIVE", "BENCH", "ON_PROJECT"}  # matches your Consultant.VALID_STATUSES


# ---------------------------------------------------------------------------
# GET /admin/users — row shape
# ---------------------------------------------------------------------------

class UserAdminRowDTO(BaseModel):
    id: str
    full_name: str
    email: str
    role: str
    status: str          # "Active" | "Inactive" — derived from is_active
    is_active: bool
    created_at: str

    model_config = {"from_attributes": True}


class PaginatedUsersDTO(BaseModel):
    data: List[UserAdminRowDTO]
    total: int
    page: int
    page_size: int
    total_pages: int


# ---------------------------------------------------------------------------
# POST /admin/users
# ---------------------------------------------------------------------------

class CreateUserRequestDTO(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8)
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        return v

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower().strip()

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        import re
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one number.")
        if not re.search(r"[!@#$%^&*?]", v):
            raise ValueError("Password must contain at least one special character (!@#$%^&*?).")
        return v


class CreateUserResponseDTO(BaseModel):
    success: bool
    user: UserAdminRowDTO
    message: str


# ---------------------------------------------------------------------------
# PUT /admin/users/{id}
# ---------------------------------------------------------------------------

class EditUserRequestDTO(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    role: str
    is_active: bool
    # consultant-only optional fields — applied only when role == CONSULTANT
    work_authorization: Optional[str] = None
    preferred_employment_types: Optional[List[str]] = None
    primary_skills: Optional[str] = None
    recruiter_id: Optional[str] = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        return v

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower().strip()


# ---------------------------------------------------------------------------
# Status management
# ---------------------------------------------------------------------------

class UpdateUserStatusRequestDTO(BaseModel):
    status: str  # ACTIVE | INACTIVE | BLOCKED

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"ACTIVE", "INACTIVE", "BLOCKED"}
        if v.upper() not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        return v.upper()


class UpdateStatusResponseDTO(BaseModel):
    success: bool
    message: str
    user_id: str
    new_status: str


# ---------------------------------------------------------------------------
# Consultant assignment
# ---------------------------------------------------------------------------

class RecruiterRefDTO(BaseModel):
    id: str
    name: str
    email: str


class ConsultantAdminRowDTO(BaseModel):
    id: str
    user_id: str = ""
    name: str
    email: str
    status: str
    primary_skills: Optional[str] = None
    work_authorization: Optional[str] = None
    preferred_employment_types: List[str] = []
    gmail_connected: bool = False
    assigned_recruiters: List[RecruiterRefDTO] = []
    created_at: str

    model_config = {"from_attributes": True}


class AssignConsultantRequestDTO(BaseModel):
    consultant_id: str


class AssignConsultantResponseDTO(BaseModel):
    success: bool
    message: str
    consultant_id: str


class UpdateRecruiterConsultantsRequestDTO(BaseModel):
    consultant_ids: List[str]


class UpdateRecruiterConsultantsResponseDTO(BaseModel):
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Manage Consultants
# ---------------------------------------------------------------------------

class UpdateConsultantRequestDTO(BaseModel):
    primary_skills: Optional[str] = None
    availability_status: Optional[str] = None
    status: Optional[str] = None
    work_authorization: Optional[str] = None
    preferred_employment_types: Optional[List[str]] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.upper() not in VALID_CONSULTANT_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_CONSULTANT_STATUSES)}")
        return v.upper() if v else v


class UpdateConsultantResponseDTO(BaseModel):
    success: bool
    message: str
    consultant: ConsultantAdminRowDTO