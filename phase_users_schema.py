# phase_users_schema.py
# ---------------------------------------------------------------------------
# Admin User Management — Pydantic schemas
# Adapted from the standalone user_mgmt_backend to use your real User/
# Consultant field names (full_name, password_hash, etc.)
#
# MIGRATION REQUIRED: users.experience_years (Numeric, nullable) was added
# to the User model to back the Recruiter "Experience (Years)" field. Run
# this against the real Postgres database before deploying this change:
#     ALTER TABLE users ADD COLUMN experience_years NUMERIC;
# Until that's applied, any query touching users will fail with
# "column users.experience_years does not exist".
# ---------------------------------------------------------------------------

from typing import Optional, List, Any
from pydantic import BaseModel, EmailStr, field_validator, Field, ConfigDict

VALID_ROLES = {"ADMIN", "RECRUITER", "CONSULTANT"}
VALID_STATUSES = {"Active", "Inactive"}
VALID_CONSULTANT_STATUSES = {"ACTIVE", "INACTIVE", "BENCH", "ON_PROJECT"}  # matches your Consultant.VALID_STATUSES


class EducationEntryDTO(BaseModel):
    degree: Optional[str] = None
    institution: Optional[str] = None
    year: Optional[str] = None


# ---------------------------------------------------------------------------
# GET /admin/users — row shape
# ---------------------------------------------------------------------------

class UserAdminRowDTO(BaseModel):
    id: str
    full_name: str
    email: str
    role: str
    status: str          # "Authorized" | "Unauthorized" — derived from is_authorized
    is_authorized: bool
    created_at: str
    updated_at: str = ""
    skills: Optional[List[str]] = None
    needsto_fetch_mail: bool = False
    experience_years: Optional[float] = None
    resume_info: Optional[Any] = None
    # Admin/Recruiter contact fields — see MIGRATION REQUIRED note on
    # models.py's User.mobile_number/extension/linkedin_url.
    mobile_number: Optional[str] = None
    extension: Optional[str] = None
    linkedin_url: Optional[str] = None
    designation: Optional[str] = None

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
# NOTE: this DTO has no work_authorization field — work authorization is
# a consultant-profile attribute set later via EditUserRequestDTO /
# UpdateConsultantRequestDTO (see those classes below), not at user
# creation time. A @field_validator("work_authorization") does NOT belong
# here: Pydantic validates decorator field references at class-definition
# time, so attaching one to a class without that field crashes on import
# with "PydanticUserError: Decorators defined with incorrect fields".

class CreateUserRequestDTO(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8)
    role: str
    experience_years: Optional[float] = Field(None, ge=0, le=60)
    resume_info: Optional[Any] = None
    # Admin/Recruiter only — Consultant profiles carry their own
    # linkedin_url on ConsultantAdminRowDTO/UpdateConsultantRequestDTO
    # instead (see AdminConsultantCreateRequest in phase3.py).
    mobile_number: Optional[str] = Field(None, max_length=30)
    # BUG FIX: max_length was 10, sized for just the 3-digit extension
    # digits — now that this field holds the whole free-text value (e.g.
    # "+1 469-392-4030 EXT 123"), 10 chars would silently reject it.
    extension: Optional[str] = Field(None, max_length=60)
    linkedin_url: Optional[str] = None
    designation: Optional[str] = Field(None, max_length=100)

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
    is_authorized: bool
    # consultant-only optional fields — applied only when role == CONSULTANT
    work_authorization: Optional[str] = None
    preferred_employment_types: Optional[List[str]] = None
    primary_skills: Optional[str] = None
    recruiter_id: Optional[str] = None
    # user-level optional fields — apply regardless of role
    skills: Optional[List[str]] = None
    needsto_fetch_mail: Optional[bool] = None
    experience_years: Optional[float] = Field(None, ge=0, le=60)
    resume_info: Optional[Any] = None
    # Admin/Recruiter contact fields — meaningless for CONSULTANT (they use
    # the separate consultant-profile PUT /admin/consultants/{id} instead,
    # via UpdateConsultantRequestDTO.linkedin_url).
    mobile_number: Optional[str] = Field(None, max_length=30)
    extension: Optional[str] = Field(None, max_length=60)
    linkedin_url: Optional[str] = None
    designation: Optional[str] = Field(None, max_length=100)

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

    # BUG FIX: this write model had no validation on work_authorization at
    # all — any string saved successfully and then silently failed to
    # match any batch in phase4.py's WORK_AUTH_BATCH_1/2/3 during
    # matching, with only a logged warning and no visible error anywhere.
    # Mirrors phase3.py's consultant self-service validate_work_auth, but
    # Optional-aware since this field isn't required here.
    @field_validator("work_authorization")
    @classmethod
    def validate_work_authorization(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        valid = {"F1", "STEM OPT", "H1B", "USC", "GC", "GC EAD", "L1", "TN", "U Visa"}
        if v not in valid:
            raise ValueError(f"work_authorization must be one of {', '.join(sorted(valid))}")
        return v


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
    # Full profile fields — added so the admin detail page can show
    # everything on the consultants table besides id/user_id.
    phone: Optional[str] = None
    sales_recruiter_user_id: Optional[str] = None
    current_location: Optional[str] = None
    preferred_locations: Optional[str] = None
    availability_status: Optional[str] = None
    total_experience_years: Optional[float] = None
    secondary_skills: Optional[str] = None
    preferred_roles: Optional[str] = None
    ats_score: Optional[float] = None
    linkedin_url: Optional[str] = None
    # Same fix shape as linkedin_url: previously only stored in
    # User.resume_info["education"] (consultant's own profile), invisible
    # to and un-editable from admin. Now backed by Consultant.education.
    education: List["EducationEntryDTO"] = []
    resume_info: Optional[Any] = None
    resume_rich_text: Optional[str] = None
    updated_at: str = ""
    has_resume: bool = False  # base_resume_file_path/base_resume_text can be large — expose presence, not raw content
    last_login_at: Optional[str] = None
    total_applications_sent: int = 0
    total_resumes_generated: int = 0
    completeness_pct: int = 0  # profile completeness %, mirrors the consultant-side formula

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
    phone: Optional[str] = None
    current_location: Optional[str] = None
    preferred_locations: Optional[str] = None
    total_experience_years: Optional[float] = None
    secondary_skills: Optional[str] = None
    preferred_roles: Optional[str] = None
    linkedin_url: Optional[str] = None
    education: Optional[List[EducationEntryDTO]] = None
    resume_info: Optional[Any] = None
    resume_rich_text: Optional[str] = None

    # BUG FIX: same gap as EditUserRequestDTO above — this is the write
    # model UserDetailPage.tsx / ConsultantDetailPage.tsx's Work Auth
    # field actually saves through. Without this, any string was accepted
    # and silently broke matching later instead of failing loudly here.
    @field_validator("work_authorization")
    @classmethod
    def validate_work_authorization(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        valid = {"F1", "STEM OPT", "H1B", "USC", "GC", "GC EAD", "L1", "TN", "U Visa"}
        if v not in valid:
            raise ValueError(f"work_authorization must be one of {', '.join(sorted(valid))}")
        return v

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


class UpdateResumeRichTextRequestDTO(BaseModel):
    resume_rich_text: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class UpdateResumeRichTextResponseDTO(BaseModel):
    success: bool
    message: str
    consultant_id: str