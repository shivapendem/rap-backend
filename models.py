from sqlalchemy import Column, BigInteger, Integer, Text, Boolean, Numeric, ARRAY, ForeignKey, UniqueConstraint, String
from sqlalchemy.sql import func
from sqlalchemy import TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, index=True)
    full_name = Column(Text, nullable=False)
    email = Column(Text, nullable=False, unique=True)

    password_hash = Column(Text, nullable=True)
    role = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

class Consultant(Base):
    __tablename__ = "consultants"

    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    full_name = Column(Text, nullable=True)
    email = Column(Text, unique=True, nullable=True)
    phone = Column(Text, nullable=True)
    sales_recruiter_user_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    current_location = Column(Text, nullable=True)
    preferred_locations = Column(Text, nullable=True)
    work_authorization = Column(Text, nullable=True)
    availability_status = Column(Text, nullable=True)
    total_experience_years = Column(Numeric, nullable=True)
    primary_skills = Column(Text, nullable=True)
    secondary_skills = Column(Text, nullable=True)
    preferred_roles = Column(Text, nullable=True)
    preferred_employment_types = Column(ARRAY(Text), nullable=False, default=["C2C"])
    base_resume_file_path = Column(Text, nullable=True)
    base_resume_text = Column(Text, nullable=True)
    gmail_connected = Column(Boolean, nullable=False, default=False)
    ats_score = Column(Numeric(5, 2), default=0)
    status = Column(Text, nullable=False, default="ACTIVE")
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

class RecruiterConsultant(Base):
    __tablename__ = "recruiter_consultants"

    id = Column(BigInteger, primary_key=True, index=True)
    recruiter_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    consultant_id = Column(BigInteger, ForeignKey("consultants.id"), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint('recruiter_id', 'consultant_id'),
    )

class EmailAccount(Base):
    __tablename__ = "email_accounts"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, unique=True)
    password = Column(String(255), nullable=False)
    label = Column(String(100), nullable=True)
    imap_host = Column(String(255), nullable=True, default="imap.gmail.com")
    imap_port = Column(Integer, nullable=True, default=993)
    active = Column(Boolean, nullable=True, default=True)
    last_synced = Column(TIMESTAMP(timezone=True), nullable=True)
    last_uid = Column(BigInteger, nullable=True, default=0)
    sync_errors = Column(Integer, nullable=True, default=0)
    created_at = Column(TIMESTAMP(timezone=True), nullable=True, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=True, server_default=func.now())

class Email(Base):
    __tablename__ = "emails"

    id = Column(BigInteger, primary_key=True, index=True)
    recruiter_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    recruiter_email = Column(Text, nullable=False)
    gmail_message_id = Column(Text, nullable=False, unique=True)
    gmail_thread_id = Column(Text, nullable=True)
    gmail_uid = Column(BigInteger, nullable=True)
    gmail_folder = Column(Text, nullable=True)
    sender_email = Column(Text, nullable=False)
    sender_name = Column(Text, nullable=True)
    raw_headers = Column(JSONB, nullable=True)
    to_addresses = Column(JSONB, nullable=True)
    cc_addresses = Column(JSONB, nullable=True)
    bcc_addresses = Column(JSONB, nullable=True)
    reply_to_address = Column(Text, nullable=True)
    subject = Column(Text, nullable=True)
    body_text = Column(Text, nullable=True)
    body_html = Column(Text, nullable=True)
    has_attachments = Column(Boolean, nullable=True, default=False)
    attachment_details = Column(JSONB, nullable=True)
    gmail_labels = Column(ARRAY(Text), nullable=True)
    is_read = Column(Boolean, nullable=True, default=False)
    is_starred = Column(Boolean, nullable=True, default=False)
    parse_status = Column(Text, nullable=False, default="NEW")
    parse_attempts = Column(Integer, nullable=False, default=0)
    received_at = Column(TIMESTAMP(timezone=True), nullable=True)
    fetched_at = Column(TIMESTAMP(timezone=True), nullable=True, server_default=func.now())
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

class Requirement(Base):
    __tablename__ = "requirements"

    id = Column(BigInteger, primary_key=True, index=True)
    recruiter_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    raw_email_id = Column(BigInteger, ForeignKey("emails.id"), nullable=True)
    role = Column(Text, nullable=False)
    vendor = Column(Text, nullable=True)
    vendor_email = Column(Text, nullable=True)
    vendor_contact = Column(Text, nullable=True)
    client = Column(Text, nullable=True)
    location = Column(Text, nullable=True)
    work_mode = Column(Text, nullable=True)
    employment_types = Column(ARRAY(Text), nullable=True)
    rate = Column(Text, nullable=True)
    duration = Column(Text, nullable=True)
    job_description = Column(Text, nullable=True)
    parsed_fields = Column(JSONB, nullable=True)
    parse_confidence = Column(Numeric(5, 2), default=0)
    ats_match_count = Column(Integer, default=0)
    status = Column(Text, nullable=False, default="NEW")
    received_date = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

class SyncLog(Base):
    __tablename__ = "sync_logs"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("email_accounts.id"), nullable=True)
    account_email = Column(String(255), nullable=True)
    started_at = Column(TIMESTAMP(timezone=True), nullable=True, server_default=func.now())
    finished_at = Column(TIMESTAMP(timezone=True), nullable=True)
    emails_found = Column(Integer, nullable=True, default=0)
    emails_saved = Column(Integer, nullable=True, default=0)
    status = Column(String(20), nullable=True, default="running")
    error_msg = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)