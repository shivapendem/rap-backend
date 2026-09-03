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
    is_authorized = Column(Boolean, nullable=False, default=True)
    allowed_to_send = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    last_login_at = Column(TIMESTAMP(timezone=True), nullable=True)
    needsto_fetch_mail = Column("needto_fetch_mail", Boolean, nullable=False, default=False)
    skills = JSONBColumn(nullable=False, default=list)
    experience_years = Column(Numeric, nullable=True)
    resume_info = JSONBColumn(nullable=True)
    mobile_number = Column(Text, nullable=True)
    extension = Column(Text, nullable=True)
    linkedin_url = Column(Text, nullable=True)
    designation = Column(Text, nullable=True)
    email_signature = Column(Text, nullable=True)

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
    # MIGRATION REQUIRED — same as User.mobile_number/extension/linkedin_url
    # above: ALTER TABLE consultants ADD COLUMN linkedin_url TEXT;
    linkedin_url = Column(Text, nullable=True)
    # MIGRATION REQUIRED — ALTER TABLE consultants ADD COLUMN education JSONB;
    # (or ADD COLUMN education TEXT on the SQLite dev path — see JSONBColumn).
    # Was previously ONLY stored in User.resume_info["education"], written/
    # read solely by the consultant's own PUT/GET /api/consultant/profile —
    # the admin screens (this table) had no column for it at all, so admin
    # couldn't see or edit it and a consultant's own edits never showed up
    # there. Same class of bug as linkedin_url above; same fix shape.
    education = JSONBColumn(nullable=True)
    preferred_employment_types = ArrayTextColumn(nullable=False, default=lambda: ["C2C"])
    base_resume_file_path = Column(Text, nullable=True)
    base_resume_text = Column(Text, nullable=True)
    # MIGRATION REQUIRED — ALTER TABLE consultants ADD COLUMN base_resume_content JSONB;
    # (or ADD COLUMN base_resume_content TEXT on the SQLite dev path — see JSONBColumn).
    # Structured counterpart to base_resume_text/base_resume_file_path — powers
    # the rich JSON + preview Base Resume editor (same shape as
    # GeneratedResume.resume_content below, consumed by ResumeRichPreview and
    # phase6.py's _generate_docx). base_resume_text/base_resume_file_path stay
    # in sync automatically on every save so AI tailoring/matching (which
    # reads base_resume_text) and Download (which reads
    # base_resume_file_path) keep working unchanged.
    base_resume_content = JSONBColumn(nullable=True)
    resume_rich_text = Column(Text, nullable=True)
    # MIGRATION REQUIRED — ALTER TABLE consultants ADD COLUMN last_profile_write_seq BIGINT;
    # BUG FIX ("switching quickly between Work Auth options sometimes ends
    # up on the wrong one"): WorkAuthSelect.tsx's AbortController logic
    # (and the identical pattern in ProfileForm/SkillTagInput/
    # EmploymentTypeCheckboxGroup) only cancels the CLIENT's interest in a
    # superseded request's response — it does not, and cannot, guarantee
    # the backend stops processing a request already in flight, or that
    # requests are received/committed in the same order they were sent.
    # Three rapid PUT /api/consultant/profile calls can genuinely finish
    # their DB writes out of order (ordinary network jitter is enough),
    # so the request for an EARLIER click can commit AFTER the request for
    # the LATEST click, silently leaving the DB (and the next profile
    # load) on the wrong value even though the abort logic worked exactly
    # as designed. Each request now carries a client-generated, strictly
    # increasing sequence number (Date.now() at click time); the server
    # only applies a write if it's newer than the last one it committed,
    # so an out-of-order late arrival is dropped instead of clobbering a
    # newer value. See ProfileUpdateRequest.clientWriteSeq / 
    # update_own_profile below.
    last_profile_write_seq = Column(BigInteger, nullable=True)
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
    recruiter_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    consultant_id = Column(FK_TYPE, ForeignKey("consultants.id", ondelete="CASCADE"), nullable=False, index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
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
    # BUG FIX: was ForeignKey("emails.id") — every writer of this column
    # (requirements_sync.py / dedup.py / gmail_reader.py / pipeline.py)
    # actually passes gmail_emails.id, not emails.id. On any environment
    # where this model creates the table fresh (new DB, redeploy, local
    # sqlite fallback), the mismatched constraint rejected every real
    # auto-synced insert with a silent FK violation — new mail parsed fine
    # but never became a Requirement row. Old/seeded requirements were
    # unaffected because they leave raw_email_id NULL.
    # (NOTE: this line reverted once already — if it comes back a second
    # time, check whether something is regenerating models.py from an
    # older source/branch.)
    # BUG FIX: gmail_emails has no SQLAlchemy ORM class anywhere in this
    # codebase — every access to it goes through raw SQL (see phase2.py,
    # dedup.py, requirements_sync.py, pipeline.py). Declaring a real
    # ForeignKey() here works fine for ordinary reads/writes of
    # Requirement in isolation, but the moment any flush needs to compute
    # insert/delete ordering across multiple related tables (e.g.
    # match_requirement committing a RequirementConsultantMatch in the
    # same session as a loaded Requirement), SQLAlchemy tries to resolve
    # every reachable FK target table and crashes with
    # NoReferencedTableError since "gmail_emails" isn't a mapped table.
    # Kept as a plain column — still semantically the gmail_emails.id
    # this row came from, just without an ORM-level constraint pointing
    # at a table SQLAlchemy can't see. Any real FK enforcement should
    # live at the DB/migration level, independent of this.
    raw_email_id = Column(FK_TYPE, nullable=True)
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
    # BUG FIX: matching_engine.py's work-auth stage (see
    # _requirement_work_auth_text()) already reads requirement.work_
    # authorization as its primary signal, and the cron project's copy
    # of dedup.py already writes it — this model was still missing the
    # column, which would crash that write with "'work_authorization' is
    # an invalid keyword argument for Requirement" the moment this
    # backend's own dedup.py/pipeline.py is ever updated to set it too
    # (matching the cron project's write side). Adding proactively so
    # that update doesn't reintroduce this crash here. Doesn't
    # retroactively add the column to an already-existing `requirements`
    # table in a real database — Base.metadata.create_all() only creates
    # tables that don't exist yet — so this also needs, once, against
    # the real database:
    #   ALTER TABLE requirements ADD COLUMN work_authorization TEXT;
    work_authorization = Column(Text, nullable=True)
    job_description = Column(Text, nullable=True)
    jd_hash = Column(Text, nullable=True, index=True)          # Phase 2: SHA-256 of normalized cleaned JD
    dedup_key = Column(Text, nullable=True, unique=True, index=True)  # Phase 2: vendor_email|role|jd_hash
    parsed_fields = JSONBColumn(nullable=True)
    parse_confidence = Column(Numeric(5, 2), default=0)
    ats_match_count = Column(Integer, default=0)
    status = Column(Text, nullable=False, default="NEW")
    received_date = Column(TIMESTAMP(timezone=True), nullable=True, index=True)
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
    # Raw (pre-weight) and weighted percentage per scoring factor — lets the
    # UI show WHY a total came out a certain way (e.g. "Role: 100% raw →
    # 50.0 pts weighted") instead of just one blended total.
    score_breakdown = JSONBColumn(nullable=True)
    status = Column(Text, nullable=False, default="ASSIGNED")
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_STATUSES = {"ASSIGNED", "NEAR_MISS", "INVALIDATED", "RESUME_GENERATED", "READY_TO_APPLY", "APPLIED", "REJECTED"}

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
    # BUG FIX ("tailored resume — after finalize, view/download missing
    # experience style — consider all templates"): this model had NO
    # template column at all, unlike Resume (the admin/manual-resume
    # model, which already went through this exact fix). Every DOCX this
    # pipeline ever produced was hardcoded to _generate_docx()'s "classic"
    # default — there was no way for ANY template's styling (accent-box
    # experience, timeline, modern's tinted table, etc.) to reach the
    # tailored-resume flow, regardless of what a picker might show.
    # MIGRATION REQUIRED — ALTER TABLE generated_resumes ADD COLUMN template VARCHAR(50) DEFAULT 'classic';
    template = Column(String(50), nullable=True, default="classic")
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
    # PERF: filtered on directly by the Applications Tracker's status
    # dropdown (PENDING/SENT/FAILED) — unindexed meant every filtered
    # tracker query did a full table scan.
    #
    # MIGRATION REQUIRED: run this against the real Postgres database
    # before deploying this change:
    #     CREATE INDEX IF NOT EXISTS ix_applications_status ON applications (status);
    status = Column(Text, nullable=False, default="PENDING", index=True)   # PENDING | SENT | FAILED
    vendor_email = Column(Text, nullable=True)
    recruiter_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    cc_email = Column(Text, nullable=True)
    gmail_message_id = Column(Text, nullable=True, index=True)
    email_subject = Column(Text, nullable=True)
    email_body_preview = Column(Text, nullable=True)
    # BUG FIX: applications sent through the email-queue/Apply-to-Requirement
    # flow (admin apply, recruiter apply-on-behalf, consultant self-apply,
    # and Compose) never set generated_resume_id at all — that field only
    # gets populated by the ATS-gated recruiter Email-Preview confirm-send
    # flow. Every other application's "Resume" column was permanently
    # blank even though a real file WAS attached and sent — it just wasn't
    # linked to the GeneratedResume table, only referenced as a raw
    # file path/S3 key on the EmailQueue item. Stores that raw reference
    # so the resume-download endpoint has something to serve.
    #
    # MIGRATION REQUIRED: run this against the real Postgres database
    # before deploying this change:
    #     ALTER TABLE applications ADD COLUMN resume_attachment_path TEXT;
    resume_attachment_path = Column(Text, nullable=True)
    # Full list of attachment refs actually sent with this application
    # (resume + any extras like a cover letter). resume_attachment_path
    # above only ever kept item.attachments[0] for the existing
    # resume-download endpoint's sake — this preserves everything so the
    # Email Preview modal can list and download every attachment sent,
    # not just the first one.
    #
    # MIGRATION REQUIRED: run this against the real Postgres database
    # before deploying this change:
    #     ALTER TABLE applications ADD COLUMN attachments_sent JSONB;
    attachments_sent = JSONBColumn(nullable=True)
    ats_score_at_send = Column(Numeric(5, 2), nullable=True)
    # PERF: this is the ORDER BY column for the Applications Tracker's
    # default sort (both admin and recruiter queries) — unindexed meant
    # every page load did a full sort with no index to lean on.
    #
    # MIGRATION REQUIRED: run this against the real Postgres database
    # before deploying this change:
    #     CREATE INDEX IF NOT EXISTS ix_applications_sent_at ON applications (sent_at);
    sent_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)
    error_message = Column(Text, nullable=True)
    candidate_id = Column(Text, nullable=True)
    job_posting_id = Column(FK_TYPE, nullable=True)
    match_score = Column(Numeric(5, 2), nullable=True)
    applied_at = Column(TIMESTAMP(timezone=True), nullable=True)
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

class JobMatch(Base):
    """
    Stores matches generated by the Matching Engine.
    Used for "Pending Applications" across the dashboards.
    """
    __tablename__ = "job_matches"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    requirement_id = Column(FK_TYPE, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    consultant_id = Column(FK_TYPE, ForeignKey("consultants.id", ondelete="CASCADE"), nullable=False, index=True)
    match_score = Column(Numeric(5, 2), nullable=True)
    matching_info = JSONBColumn(nullable=True)
    match_reasoning = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="PENDING", index=True) # PENDING, NEAR_MISS, APPLIED, REJECTED, INVALIDATED
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    VALID_STATUSES = {"PENDING", "NEAR_MISS", "APPLIED", "REJECTED", "INVALIDATED"}

    __table_args__ = (
        UniqueConstraint("requirement_id", "consultant_id", name="uq_job_match_req_cons"),
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


class EmailQueue(Base):
    """Consultant-composed emails added to an outgoing queue
    (Add to Email Queue feature — dashboard/email-queue screen).
    Not sent immediately; queued for later processing/review.
    """
    __tablename__ = "email_queue"
    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    consultant_id = Column(FK_TYPE, ForeignKey("consultants.id", ondelete="CASCADE"), nullable=False, index=True)
    requirement_id = Column(FK_TYPE, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True, index=True)
    from_email = Column(Text, nullable=False)
    to_email = Column(Text, nullable=False)
    cc_email = Column(Text, nullable=True)
    subject = Column(Text, nullable=False)
    content = Column(Text, nullable=True)
    # Rich HTML version of `content` (signature card + company banner
    # image) — built once at queue-creation time (send_email_now /
    # create_email_queue) and stored here so process_single_email_queue_item
    # can send it later exactly as composed, without needing to re-look-up
    # or re-derive the sender's identity at actual send time. NULL for
    # anything queued before this existed — those just send as plain text,
    # same as always. MIGRATION REQUIRED, same pattern as the User columns:
    #     ALTER TABLE email_queue ADD COLUMN html_content TEXT;
    html_content = Column(Text, nullable=True)
    # FIX: was `Column(JSONB, nullable=True)` — JSONB is only imported
    # inside the `if _is_postgres:` branch above, so on SQLite (dev
    # fallback) this name is undefined and importing this module raises
    # NameError. Use the existing JSONBColumn() helper instead, which
    # already picks the right underlying type for either backend.
    attachments = JSONBColumn(nullable=True)
    status = Column(Text, nullable=False, default="QUEUED")
    status_text = Column(Text, nullable=True)
    # BUG FIX: nothing tracked who actually queued/sent an email — the
    # Applications Tracker's "Sent By" column was permanently blank for
    # anything routed through this table. Set at creation time
    # (create_email_queue / send_email_now) from current_user.id, then
    # propagated onto Application.recruiter_id when
    # process_single_email_queue_item creates/updates the Application row.
    #
    # MIGRATION REQUIRED: run this against the real Postgres database
    # before deploying this change:
    #     ALTER TABLE email_queue ADD COLUMN sent_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL;
    # Until that's applied, any insert/query touching email_queue will
    # fail with "column email_queue.sent_by_user_id does not exist".
    sent_by_user_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    scheduled_at = Column(TIMESTAMP(timezone=True), nullable=True, server_default=func.now(), index=True)
    # PERF: this is the default ORDER BY column for the Email Queue list
    # (list_email_queue) — unindexed meant every page load sorted the
    # whole table with nothing to lean on.
    #
    # MIGRATION REQUIRED: run this against the real Postgres database
    # before deploying this change:
    #     CREATE INDEX IF NOT EXISTS ix_email_queue_created_at ON email_queue (created_at);
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    VALID_STATUSES = {"QUEUED", "SENT", "FAILED", "PROCESSING"}

    @validates("status")
    def validate_status(self, key, value):
        if value not in self.VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(self.VALID_STATUSES)}, got '{value}'")
        return value

class Notification(Base):
    """Stores notifications for users."""
    __tablename__ = "notifications"
    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    user_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

class Resume(Base):
    __tablename__ = "resumes"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    user_id = Column(FK_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Links a manually-authored resume back to the specific requirement it
    # was generated for — only set when created via the dashboard's
    # "no job description" custom-resume flow. Requires the manual
    # migration above; create_all() won't add this to an existing table.
    requirement_id = Column(FK_TYPE, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True, index=True)
    title = Column(String(255), nullable=False)
    target_role = Column(String(255), nullable=True)
    job_description = Column(Text, nullable=True)
    data = JSONBColumn(nullable=False, default=dict)
    # BUG FIX ("finalize with a template, but Edit/View/Download shows
    # Classic again"): the picked template was only ever passed as a
    # transient argument to the ONE _generate_docx() call at finalize
    # time — never stored anywhere. Every other screen that renders or
    # regenerates this resume (Edit Resume's live preview, Save Changes,
    # View, Download) had no way to know which template was used, so
    # they all silently defaulted back to "classic". Persisting it here
    # so every one of those paths can read the SAME value finalize set.
    # MIGRATION REQUIRED — ALTER TABLE resumes ADD COLUMN template VARCHAR(50) DEFAULT 'classic';
    template = Column(String(50), nullable=True, default="classic")
    s3_key = Column(String(500), nullable=True)
    ats_score = Column(Integer, nullable=True)
    status = Column(String(50), nullable=False, default='draft', index=True)
    download_count = Column(Integer, nullable=False, default=0)
    last_downloaded = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at = Column(TIMESTAMP(timezone=True), nullable=True)

class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id = Column(PK_TYPE, primary_key=True, index=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

class ApplicationsDetailView(Base):
    __tablename__ = "v_applications_detail"
    
    application_id = Column(PK_TYPE, primary_key=True)
    consultant_id = Column(FK_TYPE)
    requirement_id = Column(FK_TYPE)
    recruiter_id = Column(FK_TYPE)
    generated_resume_id = Column(FK_TYPE)
    status = Column(Text)
    gmail_message_id = Column(Text)
    email_subject = Column(Text)
    email_body_preview = Column(Text)
    sent_at = Column(TIMESTAMP(timezone=True))
    applied_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True))
    updated_at = Column(TIMESTAMP(timezone=True))
    vendor_email = Column(Text)
    ats_score_at_send = Column(Numeric)
    error_message = Column(Text)
    # MIGRATION REQUIRED — this view is defined directly in the database
    # (no SQL migration file for it exists in this repo), so it can't be
    # altered from here. Add resume_attachment_path to its SELECT list
    # and re-run as CREATE OR REPLACE VIEW, e.g.:
    #     CREATE OR REPLACE VIEW v_applications_detail AS
    #     SELECT ...<all existing columns, unchanged>...,
    #            a.resume_attachment_path
    #     FROM applications a
    #     ...<existing JOINs, unchanged>...;
    # (alias "a" here stands in for whatever the view's own FROM clause
    # already aliases the applications table as — match it to what's
    # actually there). Application.resume_attachment_path is a real
    # column on the base table (see below); this view was just never
    # updated to select it, which is why resume_available below couldn't
    # see it until this column is added on the database side too.
    resume_attachment_path = Column(Text)

    requirement_client = Column(Text)
    requirement_role = Column(Text)
    requirement_job_description = Column(Text)
    requirement_vendor_email = Column(Text)
    
    consultant_name = Column(Text)
    consultant_email = Column(Text)
    
    recruiter_name = Column(Text)
    recruiter_email = Column(Text)