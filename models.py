import os
from sqlalchemy import Column, BigInteger, Integer, Text, Boolean, Numeric, ForeignKey, UniqueConstraint, String
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
