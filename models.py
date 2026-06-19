import os
from sqlalchemy import Column, BigInteger, Integer, Text, Boolean, Numeric, ForeignKey, Date, UniqueConstraint, String
from sqlalchemy.sql import func
from sqlalchemy import TIMESTAMP
from sqlalchemy.orm import validates
from database import Base, DATABASE_URL

# SQLite does not support BigInteger autoincrement; use Integer for SQLite
_is_postgres = DATABASE_URL.startswith("postgresql")
PK_TYPE = BigInteger if _is_postgres else Integer
FK_TYPE = BigInteger if _is_postgres else Integer

# Use JSONB + ARRAY only when on PostgreSQL; fall back to Text for SQLite
_is_postgres = DATABASE_URL.startswith("postgresql")

if _is_postgres:
    from sqlalchemy.dialects.postgresql import JSONB, ARRAY as PG_ARRAY

    def JSONBColumn(**kwargs):
        return Column(JSONB, **kwargs)

    def ArrayTextColumn(**kwargs):
        return Column(PG_ARRAY(Text), **kwargs)
else:
    import json
    from sqlalchemy import Text as _Text
    from sqlalchemy.types import TypeDecorator

    class JSONType(TypeDecorator):
        """Stores JSON as text for SQLite compatibility."""
        impl = _Text
        cache_ok = True

        def process_bind_param(self, value, dialect):
            if value is not None:
                return json.dumps(value)
            return value

        def process_result_value(self, value, dialect):
            if value is not None:
                return json.loads(value)
            return value

    def JSONBColumn(**kwargs):
        return Column(JSONType, **kwargs)

    def ArrayTextColumn(**kwargs):
        return Column(JSONType, **kwargs)  # Store list as JSON string for SQLite


class User(Base):
    __tablename__ = "users"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    full_name = Column(Text, nullable=False)
    email = Column(Text, nullable=False, unique=True, index=True)
    password_hash = Column(Text, nullable=True)
    role = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_ROLES = {"ADMIN", "RECRUITER", "CONSULTANT"}

    @validates("role")
    def validate_role(self, key, value):
        if value not in self.VALID_ROLES:
            raise ValueError(f"role must be one of {self.VALID_ROLES}, got '{value}'")
        return value

    @validates("email")
    def validate_email(self, key, value):
        if not value or "@" not in value:
            raise ValueError("Invalid email address")
        return value.lower().strip()


class Consultant(Base):
    __tablename__ = "consultants"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    user_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    full_name = Column(Text, nullable=True)
    email = Column(Text, unique=True, nullable=True, index=True)
    phone = Column(Text, nullable=True)
    sales_recruiter_user_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    current_location = Column(Text, nullable=True)
    preferred_locations = Column(Text, nullable=True)
    work_authorization = Column(Text, nullable=True)
    availability_status = Column(Text, nullable=True)
    total_experience_years = Column(Numeric, nullable=True)
    primary_skills = Column(Text, nullable=True)
    secondary_skills = Column(Text, nullable=True)
    preferred_roles = Column(Text, nullable=True)
    preferred_employment_types = ArrayTextColumn(nullable=False, default=lambda: ["C2C"])
    base_resume_file_path = Column(Text, nullable=True)
    base_resume_text = Column(Text, nullable=True)
    gmail_connected = Column(Boolean, nullable=False, default=False)
    ats_score = Column(Numeric(5, 2), default=0)
    status = Column(Text, nullable=False, default="ACTIVE")
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_STATUSES = {"ACTIVE", "INACTIVE", "BENCH", "ON_PROJECT"}

    @validates("status")
    def validate_status(self, key, value):
        if value not in self.VALID_STATUSES:
            raise ValueError(f"status must be one of {self.VALID_STATUSES}, got '{value}'")
        return value


class RecruiterConsultant(Base):
    __tablename__ = "recruiter_consultants"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    recruiter_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    consultant_id = Column(FK_TYPE, ForeignKey("consultants.id", ondelete="CASCADE"), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("recruiter_id", "consultant_id", name="uq_recruiter_consultant"),
    )


class EmailAccount(Base):
    __tablename__ = "email_accounts"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password = Column(String(255), nullable=False)  # Should be encrypted at app level before storing
    label = Column(String(100), nullable=True)
    imap_host = Column(String(255), nullable=True, default="imap.gmail.com")
    imap_port = Column(Integer, nullable=True, default=993)
    active = Column(Boolean, nullable=True, default=True)
    last_synced = Column(TIMESTAMP(timezone=True), nullable=True)
    last_uid = Column(FK_TYPE, nullable=True, default=0)
    sync_errors = Column(Integer, nullable=True, default=0)
    created_at = Column(TIMESTAMP(timezone=True), nullable=True, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=True, server_default=func.now(), onupdate=func.now())

    @validates("imap_port")
    def validate_port(self, key, value):
        if value is not None and not (1 <= value <= 65535):
            raise ValueError(f"imap_port must be between 1 and 65535, got {value}")
        return value


class Email(Base):
    __tablename__ = "emails"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    recruiter_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    recruiter_email = Column(Text, nullable=False)
    gmail_message_id = Column(Text, nullable=False, unique=True, index=True)
    gmail_thread_id = Column(Text, nullable=True)
    gmail_uid = Column(FK_TYPE, nullable=True)
    gmail_folder = Column(Text, nullable=True)
    sender_email = Column(Text, nullable=False)
    sender_name = Column(Text, nullable=True)
    raw_headers = JSONBColumn(nullable=True)
    to_addresses = JSONBColumn(nullable=True)
    cc_addresses = JSONBColumn(nullable=True)
    bcc_addresses = JSONBColumn(nullable=True)
    reply_to_address = Column(Text, nullable=True)
    subject = Column(Text, nullable=True)
    body_text = Column(Text, nullable=True)
    body_html = Column(Text, nullable=True)
    has_attachments = Column(Boolean, nullable=True, default=False)
    attachment_details = JSONBColumn(nullable=True)
    gmail_labels = ArrayTextColumn(nullable=True)
    is_read = Column(Boolean, nullable=True, default=False)
    is_starred = Column(Boolean, nullable=True, default=False)
    parse_status = Column(Text, nullable=False, default="NEW")
    parse_attempts = Column(Integer, nullable=False, default=0)
    received_at = Column(TIMESTAMP(timezone=True), nullable=True)
    fetched_at = Column(TIMESTAMP(timezone=True), nullable=True, server_default=func.now())
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_PARSE_STATUSES = {"NEW", "PROCESSING", "PARSED", "FAILED", "SKIPPED"}

    @validates("parse_status")
    def validate_parse_status(self, key, value):
        if value not in self.VALID_PARSE_STATUSES:
            raise ValueError(f"parse_status must be one of {self.VALID_PARSE_STATUSES}, got '{value}'")
        return value


class Requirement(Base):
    __tablename__ = "requirements"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    recruiter_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    raw_email_id = Column(FK_TYPE, ForeignKey("emails.id", ondelete="SET NULL"), nullable=True)
    role = Column(Text, nullable=False)
    vendor = Column(Text, nullable=True)
    vendor_email = Column(Text, nullable=True)
    vendor_contact = Column(Text, nullable=True)  # BUG FIX: was dict/JSONB in seed but Text in model — keep as Text
    client = Column(Text, nullable=True)
    location = Column(Text, nullable=True)
    work_mode = Column(Text, nullable=True)
    employment_types = ArrayTextColumn(nullable=True)
    rate = Column(Text, nullable=True)
    duration = Column(Text, nullable=True)
    job_description = Column(Text, nullable=True)
    jd_hash = Column(Text, nullable=True, index=True)          # Phase 2: SHA-256 of normalized cleaned JD
    dedup_key = Column(Text, nullable=True, unique=True, index=True)  # Phase 2: vendor_email|role|jd_hash
    parsed_fields = JSONBColumn(nullable=True)
    parse_confidence = Column(Numeric(5, 2), default=0)
    ats_match_count = Column(Integer, default=0)
    status = Column(Text, nullable=False, default="NEW")
    received_date = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_STATUSES = {"NEW", "REVIEWING", "SUBMITTED", "INTERVIEWING", "CLOSED", "REJECTED"}

    @validates("role")
    def validate_role(self, key, value):
        if not value or not value.strip():
            raise ValueError("role cannot be empty")
        return value.strip()

    @validates("status")
    def validate_status(self, key, value):
        if value not in self.VALID_STATUSES:
            raise ValueError(f"status must be one of {self.VALID_STATUSES}, got '{value}'")
        return value

class ConsultantExperience(Base):
    __tablename__ = "consultant_experience"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    consultant_id = Column(FK_TYPE, ForeignKey("consultants.id", ondelete="CASCADE"), nullable=False, index=True)
    client_name = Column(Text, nullable=False)
    project_title = Column(Text, nullable=True)
    role_title = Column(Text, nullable=False)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)          # NULL when is_present=True
    is_present = Column(Boolean, nullable=False, default=False)
    location = Column(Text, nullable=True)           # city, state e.g. "Austin, TX"
    work_mode = Column(Text, nullable=True)          # see VALID_WORK_MODES below
    work_mode_detail = Column(Text, nullable=True)   # e.g. "3 days onsite per week"
    technologies = ArrayTextColumn(nullable=True)
    responsibilities = Column(Text, nullable=True)
    achievements = Column(Text, nullable=True)
    implementation_partner = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_WORK_MODES = {
        "REMOTE",            # fully remote, no office presence
        "ONSITE",            # fully onsite at client location
        "HYBRID",            # mix of remote and onsite
    }

    @validates("work_mode")
    def validate_work_mode(self, key, value):
        if value is not None and value not in self.VALID_WORK_MODES:
            raise ValueError(
                f"work_mode must be one of {sorted(self.VALID_WORK_MODES)}, got '{value}'"
            )
        return value

    @validates("end_date")
    def validate_end_date(self, key, value):
        if self.is_present:
            return None  # is_present=True means currently working here, no end date
        return value

class SyncLog(Base):
    __tablename__ = "sync_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("email_accounts.id", ondelete="SET NULL"), nullable=True)
    account_email = Column(String(255), nullable=True)
    started_at = Column(TIMESTAMP(timezone=True), nullable=True, server_default=func.now())
    finished_at = Column(TIMESTAMP(timezone=True), nullable=True)
    emails_found = Column(Integer, nullable=True, default=0)
    emails_saved = Column(Integer, nullable=True, default=0)
    status = Column(String(20), nullable=True, default="running")
    error_msg = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    VALID_STATUSES = {"running", "success", "failed", "partial"}

    @validates("status")
    def validate_status(self, key, value):
        if value is not None and value not in self.VALID_STATUSES:
            raise ValueError(f"status must be one of {self.VALID_STATUSES}, got '{value}'")
        return value
    
class RequirementConsultantMatch(Base):
    __tablename__ = "requirement_consultant_matches"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    requirement_id = Column(FK_TYPE, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    consultant_id = Column(FK_TYPE, ForeignKey("consultants.id", ondelete="CASCADE"), nullable=False, index=True)
    match_score = Column(Numeric(5, 2), nullable=False, default=0)
    skill_score = Column(Numeric(5, 2), nullable=True)
    role_score = Column(Numeric(5, 2), nullable=True)
    experience_score = Column(Numeric(5, 2), nullable=True)
    employment_score = Column(Numeric(5, 2), nullable=True)
    location_score = Column(Numeric(5, 2), nullable=True)
    auth_score = Column(Numeric(5, 2), nullable=True)
    matched_skills = ArrayTextColumn(nullable=True)
    missing_skills = ArrayTextColumn(nullable=True)
    match_reason = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="ASSIGNED")
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_STATUSES = {"ASSIGNED", "RESUME_GENERATED", "READY_TO_APPLY", "APPLIED", "REJECTED"}

    __table_args__ = (
        UniqueConstraint("requirement_id", "consultant_id", name="uq_requirement_consultant_match"),
    )

    @validates("status")
    def validate_status(self, key, value):
        if value not in self.VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(self.VALID_STATUSES)}, got '{value}'")
        return value
    
class GeneratedResume(Base):
    __tablename__ = "generated_resumes"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    consultant_id = Column(FK_TYPE, ForeignKey("consultants.id", ondelete="CASCADE"), nullable=False, index=True)
    requirement_id = Column(FK_TYPE, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_user_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ai_model = Column(Text, nullable=False, default="gpt-4o")
    generation_notes = Column(Text, nullable=True)
    generation_attempt = Column(Integer, nullable=False, default=1)
    resume_content = JSONBColumn(nullable=True)
    ats_score = Column(Numeric(5, 2), nullable=True)
    ats_keyword_score = Column(Numeric(5, 2), nullable=True)
    ats_role_score = Column(Numeric(5, 2), nullable=True)
    ats_format_score = Column(Numeric(5, 2), nullable=True)
    ats_matched_keywords = ArrayTextColumn(nullable=True)
    ats_missing_keywords = ArrayTextColumn(nullable=True)
    docx_path = Column(Text, nullable=True)
    pdf_path = Column(Text, nullable=True)
    filename = Column(Text, nullable=True)
    pdf_url = Column(Text, nullable=True)             # servable download URL, mirrors pdf_path
    generation_status = Column(Text, nullable=True)   # mirrors status, Phase 5 dashboard naming
    status = Column(Text, nullable=False, default="GENERATING")
    is_final = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_STATUSES = {
        "GENERATING",
        "READY",
        "NEEDS_REVIEW",
        "FAILED",
    }

    @validates("status")
    def validate_status(self, key, value):
        if value not in self.VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(self.VALID_STATUSES)}, got '{value}'")
        return value    
    
class Application(Base):
    """
    Tracks application submissions per consultant per requirement.
    Required by Phase 5 doc Task 2 — 'already applied' eligibility check
    and the apply endpoint.
    """
    __tablename__ = "applications"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    consultant_id = Column(FK_TYPE, ForeignKey("consultants.id", ondelete="CASCADE"), nullable=False, index=True)
    requirement_id = Column(FK_TYPE, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    generated_resume_id = Column(FK_TYPE, ForeignKey("generated_resumes.id", ondelete="SET NULL"), nullable=True)
    status = Column(Text, nullable=False, default="PENDING")   # PENDING | SENT | FAILED
    vendor_email = Column(Text, nullable=True)
    recruiter_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    cc_email = Column(Text, nullable=True)
    gmail_message_id = Column(Text, nullable=True, index=True)
    email_subject = Column(Text, nullable=True)
    email_body_preview = Column(Text, nullable=True)
    ats_score_at_send = Column(Numeric(5, 2), nullable=True)
    sent_at = Column(TIMESTAMP(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_STATUSES = {"PENDING", "SENT", "FAILED"}

    __table_args__ = (
        UniqueConstraint("consultant_id", "requirement_id", name="uq_application_cons_req"),
    )

    @validates("status")
    def validate_status(self, key, value):
        if value not in self.VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(self.VALID_STATUSES)}, got '{value}'")
        return value

class ConsultantEmailToken(Base):
    """
    Stores OAuth tokens for consultant Gmail accounts.
    One record per consultant (UNIQUE on consultant_id).
    """
    __tablename__ = "consultant_email_tokens"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    consultant_id = Column(
        FK_TYPE,
        ForeignKey("consultants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    email_provider = Column(Text, nullable=False, default="GMAIL")
    email_address = Column(Text, nullable=True)
    access_token_encrypted = Column(Text, nullable=True)
    refresh_token_encrypted = Column(Text, nullable=True)
    token_expiry = Column(TIMESTAMP(timezone=True), nullable=True)
    send_permission_granted = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

# ---------------------------------------------------------------------------
# Phase 8 — Admin Monitoring Tables
# Uses your existing PK_TYPE/FK_TYPE/JSONBColumn helpers already defined
# at the top of this file. actor_user_id / assigned_admin_id are plain
# columns (no ForeignKey) since Phase 8 was originally built against a
# UUID-based users table — yours is BigInteger, so we keep these as
# loosely-typed reference columns instead of enforced foreign keys.
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """Full audit trail — logins, sends, errors, admin actions."""
    __tablename__ = "audit_logs"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    actor_user_id = Column(Text, nullable=True)
    actor_name = Column(Text, nullable=True)
    actor_role = Column(Text, nullable=True)
    action = Column(Text, nullable=False, index=True)
    entity_type = Column(Text, nullable=True, index=True)
    entity_id = Column(Text, nullable=True)
    meta = JSONBColumn(nullable=True)
    ip_address = Column(Text, nullable=True)
    user_agent = Column(Text, nullable=True)
    request_id = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True)


class ProcessingError(Base):
    """Error queue with retry tracking."""
    __tablename__ = "processing_errors"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    source_type = Column(Text, nullable=True)
    source_id = Column(Text, nullable=True)
    error_stage = Column(Text, nullable=False, index=True)
    error_message = Column(Text, nullable=False)
    stack_trace = Column(Text, nullable=True)
    raw_payload = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="OPEN", index=True)
    retry_count = Column(Integer, nullable=False, default=0)
    last_retry_at = Column(TIMESTAMP(timezone=True), nullable=True)
    raw_email_id = Column(Text, nullable=True)
    requirement_id = Column(Text, nullable=True)
    consultant_id = Column(Text, nullable=True)
    additional_context = JSONBColumn(nullable=True)
    occurred_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True)
    resolved_at = Column(TIMESTAMP(timezone=True), nullable=True)


class ManualReviewQueue(Base):
    """Manual review workflow tied to processing errors."""
    __tablename__ = "manual_review_queue"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    error_id = Column(FK_TYPE, ForeignKey("processing_errors.id"), nullable=False)
    assigned_admin_id = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="OPEN", index=True)  # OPEN|APPROVED|REJECTED|FIXED
    correction_data = JSONBColumn(nullable=True)
    review_notes = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AIUsageLog(Base):
    """AI cost tracking per resume generation / parsing call."""
    __tablename__ = "ai_usage_logs"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    purpose = Column(Text, nullable=False, index=True)
    model = Column(Text, nullable=False)
    input_tokens = Column(Integer, nullable=False)
    output_tokens = Column(Integer, nullable=False)
    estimated_cost = Column(Numeric(10, 6), nullable=False)
    entity_type = Column(Text, nullable=True)
    entity_id = Column(Text, nullable=True)
    consultant_id = Column(Text, nullable=True)
    consultant_name = Column(Text, nullable=True)
    requirement_id = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True)


class AppSetting(Base):
    """Key-value app settings (e.g. AI budget threshold)."""
    __tablename__ = "app_settings"

    key = Column(Text, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    updated_by = Column(Text, nullable=True)


class SystemEvent(Base):
    """WebSocket broadcast event log."""
    __tablename__ = "system_events"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    event_type = Column(Text, nullable=False, index=True)
    payload = JSONBColumn(nullable=True)
    broadcast_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True)