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
import re
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

    # BUG FIX (consistency with consultant's own "My Profile" screen):
    # ProfileFormSchema (features/consultant/profile/schemas) requires
    # non-empty values for these fields, but nothing on the backend
    # enforced that — the API accepted an empty string for any of them,
    # so a request that bypassed the admin UI's own client-side checks
    # (a different screen, a script, a direct API call) could still blank
    # out required consultant data.
    #
    # Every field here is Optional with a None default because this PUT
    # is used for PARTIAL updates — each inline edit on the admin screens
    # sends only the one field being changed, leaving every other field
    # absent from the request body entirely. Pydantic v2 does not run a
    # field's validator when that field is omitted and falls back to its
    # declared default (validate_default=False, the default setting) — it
    # only runs when the field is explicitly present in the request, even
    # if the explicit value is empty. That is exactly the behavior these
    # validators rely on: "field omitted" (this save didn't touch it)
    # silently passes through as None untouched, while "field explicitly
    # sent as empty" (someone tried to clear a required value) is
    # rejected. No model_fields_set bookkeeping needed for that reason.
    @field_validator(
        "primary_skills", "secondary_skills", "current_location",
        "preferred_locations", "preferred_roles",
    )
    @classmethod
    def validate_required_text_fields(cls, v: Optional[str], info) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError(f"{info.field_name} is required and cannot be cleared")
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("phone is required and cannot be cleared")
        # Same pattern as ProfileFormSchema's phone regex.
        if not re.match(r"^\+?[\d\s\-().]{7,20}$", v.strip()):
            raise ValueError("Enter a valid phone number")
        return v

    @field_validator("linkedin_url")
    @classmethod
    def validate_linkedin_url_required(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("linkedin_url is required and cannot be cleared")
        if "linkedin.com" not in v.strip().lower():
            raise ValueError("linkedin_url must include linkedin.com")
        return v

    @field_validator("total_experience_years")
    @classmethod
    def validate_total_experience_years(cls, v: Optional[float]) -> Optional[float]:
        # An explicit `null` here (field present in the request body with
        # a null value) means "clear it" — reject that the same as an
        # empty string on a text field. Omitting the key entirely (the
        # normal case for every save that isn't touching this field)
        # never reaches this validator at all, per the class docstring
        # above.
        if v is None:
            raise ValueError("total_experience_years is required and cannot be cleared")
        if v < 0 or v > 60:
            raise ValueError("total_experience_years must be between 0 and 60")
        return v

    @field_validator("preferred_employment_types")
    @classmethod
    def validate_preferred_employment_types(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is not None and len(v) == 0:
            raise ValueError("Select at least one employment preference")
        return v

    @field_validator("education")
    @classmethod
    def validate_education(cls, v: Optional[List[EducationEntryDTO]]) -> Optional[List[EducationEntryDTO]]:
        if v is None:
            return v
        if len(v) == 0:
            raise ValueError("At least one education entry is required")
        for entry in v:
            if not (entry.degree or "").strip() or not (entry.institution or "").strip() or not (entry.year or "").strip():
                raise ValueError("Degree, institution, and year are all required for each education entry")
        return v

    # BUG FIX: same gap as EditUserRequestDTO above — this is the write
    # model UserDetailPage.tsx / ConsultantDetailPage.tsx's Work Auth
    # field actually saves through. Without this, any string was accepted
    # and silently broke matching later instead of failing loudly here.
    #
    # BUG FIX (consistency with "My Profile"): previously allowed v == ""
    # through unchanged, so an explicit clear of Work Authorization
    # silently succeeded — now rejected the same way an empty string on
    # any other required field above is.
    @field_validator("work_authorization")
    @classmethod
    def validate_work_authorization(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v == "":
            raise ValueError("work_authorization is required and cannot be cleared")
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