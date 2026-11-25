-- =============================================================================
-- RAP Database — Complete Standalone Schema Architecture
-- File: backend/db_structure.sql
--
-- Re-creates the exact production PostgreSQL database from scratch:
--   1. Extensions: 2 extensions (pg_trgm, pgcrypto)
--   2. Sequences: 32 sequences
--   3. Base Tables: 36 tables with 683 columns, data types, defaults, and NULL constraints
--   4. Primary Keys & Unique Constraints (42) and Check Constraints
--   5. Foreign Key Constraints: 33 foreign keys with exact cascade rules
--   6. Trigger Functions (6) & Triggers (5) (Gmail sync, auto-pause, timestamps)
--   7. Indexes: All 223 secondary, composite, partial, covering, and GIN trigram indexes
--   8. Views: All 12 pre-joined views for KPIs, Match Count, and Tracking
--
-- To initialize a brand new database:
--   createdb -U <user> -h <host> <db_name>
--   psql -U <user> -h <host> -d <db_name> -f db_structure.sql
-- =============================================================================

SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET client_min_messages = warning;
SET row_security = off;

-- -----------------------------------------------------------------------------
-- 1. EXTENSIONS
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

-- -----------------------------------------------------------------------------
-- 2. SEQUENCES
-- -----------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS public.ai_usage_logs_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.applications_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.audit_logs_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.batch_jobs_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.consultant_email_tokens_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.consultant_experience_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.consultants_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.email_accounts_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.email_queue_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.emails_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.feedback_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.feedback_image_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.generated_resumes_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.gmail_accounts_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.gmail_emails_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.gmail_sync_logs_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.job_postings_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.job_updates_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.manual_review_queue_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.matching_run_status_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.message_templates_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.notifications_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.openai_batch_jobs_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.processing_errors_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.recruiter_consultants_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.requirement_consultant_matches_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.requirements_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.resumes_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.sync_logs_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.system_events_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.training_labels_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;
CREATE SEQUENCE IF NOT EXISTS public.users_id_seq START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE;

-- -----------------------------------------------------------------------------
-- 3. BASE TABLES (Columns, Defaults, Nullability, In-line PKs, Checks, & Uniques)
-- -----------------------------------------------------------------------------
-- Table: public.ai_usage_logs
CREATE TABLE IF NOT EXISTS public.ai_usage_logs (
    id bigint DEFAULT nextval('ai_usage_logs_id_seq'::regclass) NOT NULL,
    purpose text NOT NULL,
    model text NOT NULL,
    input_tokens integer NOT NULL,
    output_tokens integer NOT NULL,
    estimated_cost numeric(10, 6) NOT NULL,
    entity_type text,
    entity_id text,
    consultant_id text,
    consultant_name text,
    requirement_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ai_usage_logs_pkey PRIMARY KEY (id)
);

-- Table: public.app_settings
CREATE TABLE IF NOT EXISTS public.app_settings (
    key text NOT NULL,
    value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text,
    CONSTRAINT app_settings_pkey PRIMARY KEY (key)
);

-- Table: public.applications
CREATE TABLE IF NOT EXISTS public.applications (
    id bigint DEFAULT nextval('applications_id_seq'::regclass) NOT NULL,
    consultant_id bigint NOT NULL,
    requirement_id bigint NOT NULL,
    generated_resume_id bigint,
    status text NOT NULL,
    vendor_email text,
    recruiter_id bigint,
    cc_email text,
    gmail_message_id text,
    email_subject text,
    email_body_preview text,
    ats_score_at_send numeric(5, 2),
    sent_at timestamp with time zone,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    candidate_id text,
    job_posting_id bigint,
    match_score numeric(5, 2),
    applied_at time with time zone,
    resume_attachment_path text,
    attachments_sent jsonb,
    job_match_id bigint,
    match_percentage numeric(5, 2),
    CONSTRAINT applications_pkey PRIMARY KEY (id),
    CONSTRAINT uq_application_cons_req UNIQUE (consultant_id, requirement_id)
);

-- Table: public.audit_logs
CREATE TABLE IF NOT EXISTS public.audit_logs (
    id bigint DEFAULT nextval('audit_logs_id_seq'::regclass) NOT NULL,
    actor_user_id text,
    actor_name text,
    actor_role text,
    action text NOT NULL,
    entity_type text,
    entity_id text,
    meta jsonb,
    ip_address text,
    user_agent text,
    request_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT audit_logs_pkey PRIMARY KEY (id)
);

-- Table: public.batch_jobs
CREATE TABLE IF NOT EXISTS public.batch_jobs (
    id bigint DEFAULT nextval('batch_jobs_id_seq'::regclass) NOT NULL,
    batch_id text NOT NULL,
    status text DEFAULT 'validating'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT batch_jobs_pkey PRIMARY KEY (id),
    CONSTRAINT batch_jobs_batch_id_key UNIQUE (batch_id)
);

-- Table: public.consultant_email_tokens
CREATE TABLE IF NOT EXISTS public.consultant_email_tokens (
    id bigint DEFAULT nextval('consultant_email_tokens_id_seq'::regclass) NOT NULL,
    consultant_id bigint NOT NULL,
    email_provider text NOT NULL,
    email_address text,
    access_token_encrypted text,
    refresh_token_encrypted text,
    token_expiry timestamp with time zone,
    send_permission_granted boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT consultant_email_tokens_pkey PRIMARY KEY (id)
);

-- Table: public.consultant_experience
CREATE TABLE IF NOT EXISTS public.consultant_experience (
    id bigint DEFAULT nextval('consultant_experience_id_seq'::regclass) NOT NULL,
    consultant_id bigint NOT NULL,
    client_name text NOT NULL,
    project_title text,
    role_title text NOT NULL,
    start_date date,
    end_date date,
    is_present boolean NOT NULL,
    location text,
    work_mode text,
    work_mode_detail text,
    technologies text[],
    responsibilities text,
    achievements text,
    implementation_partner text,
    sort_order integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT consultant_experience_pkey PRIMARY KEY (id)
);

-- Table: public.consultants
CREATE TABLE IF NOT EXISTS public.consultants (
    id bigint DEFAULT nextval('consultants_id_seq'::regclass) NOT NULL,
    user_id bigint,
    full_name text,
    email text,
    phone text,
    sales_recruiter_user_id bigint,
    current_location text,
    preferred_locations text,
    work_authorization text,
    availability_status text,
    total_experience_years numeric,
    primary_skills text,
    secondary_skills text,
    preferred_roles text,
    preferred_employment_types text[] NOT NULL,
    base_resume_file_path text,
    base_resume_text text,
    gmail_connected boolean NOT NULL,
    ats_score numeric(5, 2),
    status text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    linkedin_url text,
    education jsonb,
    base_resume_content jsonb,
    resume_rich_text text,
    last_profile_write_seq bigint,
    CONSTRAINT consultants_pkey PRIMARY KEY (id)
);

-- Table: public.email_accounts
CREATE TABLE IF NOT EXISTS public.email_accounts (
    id integer DEFAULT nextval('email_accounts_id_seq'::regclass) NOT NULL,
    email character varying(255) NOT NULL,
    password character varying(255) NOT NULL,
    label character varying(100),
    imap_host character varying(255),
    imap_port integer,
    active boolean,
    last_synced timestamp with time zone,
    last_uid bigint,
    sync_errors integer,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    mode character varying(20) DEFAULT 'public'::character varying NOT NULL,
    CONSTRAINT email_accounts_pkey PRIMARY KEY (id)
);

-- Table: public.email_queue
CREATE TABLE IF NOT EXISTS public.email_queue (
    id bigint DEFAULT nextval('email_queue_id_seq'::regclass) NOT NULL,
    consultant_id bigint NOT NULL,
    requirement_id bigint,
    from_email text NOT NULL,
    to_email text NOT NULL,
    subject text NOT NULL,
    content text,
    attachments jsonb,
    status text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status_text text DEFAULT ''::text,
    cc_email text,
    sent_by_user_id bigint,
    scheduled_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    html_content text,
    CONSTRAINT email_queue_pkey PRIMARY KEY (id)
);

-- Table: public.emails
CREATE TABLE IF NOT EXISTS public.emails (
    id bigint DEFAULT nextval('emails_id_seq'::regclass) NOT NULL,
    recruiter_id bigint,
    recruiter_email text NOT NULL,
    gmail_message_id text NOT NULL,
    gmail_thread_id text,
    gmail_uid bigint,
    gmail_folder text,
    sender_email text NOT NULL,
    sender_name text,
    raw_headers jsonb,
    to_addresses jsonb,
    cc_addresses jsonb,
    bcc_addresses jsonb,
    reply_to_address text,
    subject text,
    body_text text,
    body_html text,
    has_attachments boolean,
    attachment_details jsonb,
    gmail_labels text[],
    is_read boolean,
    is_starred boolean,
    parse_status text NOT NULL,
    parse_attempts integer NOT NULL,
    received_at timestamp with time zone,
    fetched_at timestamp with time zone DEFAULT now(),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT emails_pkey PRIMARY KEY (id)
);

-- Table: public.feedback
CREATE TABLE IF NOT EXISTS public.feedback (
    id bigint DEFAULT nextval('feedback_id_seq'::regclass) NOT NULL,
    user_id bigint,
    reporter_name text,
    reporter_role text,
    type text NOT NULL,
    title text NOT NULL,
    location text,
    description text NOT NULL,
    impact text,
    steps text,
    expected text,
    image_key text,
    image_name text,
    image_content_type text,
    page_url text,
    user_agent text,
    screen text,
    status text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT feedback_pkey PRIMARY KEY (id)
);

-- Table: public.feedback_image
CREATE TABLE IF NOT EXISTS public.feedback_image (
    id bigint DEFAULT nextval('feedback_image_id_seq'::regclass) NOT NULL,
    feedback_id bigint NOT NULL,
    image_key text NOT NULL,
    image_name text,
    image_content_type text,
    position integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT feedback_image_pkey PRIMARY KEY (id)
);

-- Table: public.generated_resumes
CREATE TABLE IF NOT EXISTS public.generated_resumes (
    id bigint DEFAULT nextval('generated_resumes_id_seq'::regclass) NOT NULL,
    consultant_id bigint NOT NULL,
    requirement_id bigint NOT NULL,
    created_by_user_id bigint,
    ai_model text NOT NULL,
    generation_notes text,
    generation_attempt integer NOT NULL,
    resume_content jsonb,
    ats_score numeric(5, 2),
    ats_keyword_score numeric(5, 2),
    ats_role_score numeric(5, 2),
    ats_format_score numeric(5, 2),
    ats_matched_keywords text[],
    ats_missing_keywords text[],
    docx_path text,
    pdf_path text,
    filename text,
    pdf_url text,
    generation_status text,
    status text NOT NULL,
    is_final boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    template character varying(50) DEFAULT 'classic'::character varying,
    CONSTRAINT generated_resumes_pkey PRIMARY KEY (id)
);

-- Table: public.gmail_accounts
CREATE TABLE IF NOT EXISTS public.gmail_accounts (
    id integer DEFAULT nextval('gmail_accounts_id_seq'::regclass) NOT NULL,
    email character varying(255) NOT NULL,
    password character varying(255) NOT NULL,
    label character varying(100),
    imap_host character varying(255) DEFAULT 'imap.gmail.com'::character varying,
    imap_port integer DEFAULT 993,
    active boolean DEFAULT true,
    last_synced timestamp with time zone,
    last_uid bigint DEFAULT 0,
    sync_errors integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    full_name character varying(255),
    phone character varying(50),
    skills jsonb DEFAULT '[]'::jsonb,
    resume_url text,
    mode character varying(20) DEFAULT 'public'::character varying NOT NULL,
    CONSTRAINT gmail_accounts_pkey PRIMARY KEY (id),
    CONSTRAINT chk_gmail_accounts_mode CHECK (((mode)::text = ANY ((ARRAY['public'::character varying, 'private'::character varying])::text[]))),
    CONSTRAINT gmail_accounts_email_key UNIQUE (email)
);

-- Table: public.gmail_emails
CREATE TABLE IF NOT EXISTS public.gmail_emails (
    id integer DEFAULT nextval('gmail_emails_id_seq'::regclass) NOT NULL,
    account_id integer,
    account_email character varying(255) NOT NULL,
    message_id character varying(512),
    uid text,
    folder character varying(100) DEFAULT 'INBOX'::character varying,
    subject text,
    from_address text,
    from_name character varying(255),
    to_addresses jsonb,
    cc_addresses jsonb,
    bcc_addresses jsonb,
    reply_to text,
    body_text text,
    body_html text,
    date timestamp with time zone,
    is_read boolean DEFAULT false,
    is_starred boolean DEFAULT false,
    has_attachments boolean DEFAULT false,
    attachments jsonb,
    labels text[],
    thread_id character varying(255),
    raw_headers jsonb,
    fetched_at timestamp with time zone DEFAULT now(),
    category character varying(20) DEFAULT 'unclassified'::character varying,
    priority character varying(10) DEFAULT 'low'::character varying,
    processed boolean DEFAULT false,
    classified_at timestamp without time zone,
    classifier_tier character varying(10),
    job_posting_id integer,
    status_desc text DEFAULT 'Pending'::text,
    reason text,
    mode character varying(20) DEFAULT 'public'::character varying NOT NULL,
    CONSTRAINT gmail_emails_pkey PRIMARY KEY (id),
    CONSTRAINT gmail_emails_account_id_uid_folder_key UNIQUE (account_id, uid, folder)
);

-- Table: public.gmail_sync_logs
CREATE TABLE IF NOT EXISTS public.gmail_sync_logs (
    id integer DEFAULT nextval('gmail_sync_logs_id_seq'::regclass) NOT NULL,
    account_id integer,
    account_email character varying(255),
    started_at timestamp with time zone DEFAULT now(),
    finished_at timestamp with time zone,
    emails_found integer DEFAULT 0,
    emails_saved integer DEFAULT 0,
    status character varying(20) DEFAULT 'running'::character varying,
    error_msg text,
    duration_ms integer,
    CONSTRAINT gmail_sync_logs_pkey PRIMARY KEY (id)
);

-- Table: public.job_matches
CREATE TABLE IF NOT EXISTS public.job_matches (
    id bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
    requirement_id bigint NOT NULL,
    consultant_id bigint NOT NULL,
    match_score numeric(5, 2),
    match_reasoning text,
    status text DEFAULT 'PENDING'::text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    matching_info jsonb,
    CONSTRAINT job_matches_pkey PRIMARY KEY (id),
    CONSTRAINT uq_job_match_req_cons UNIQUE (requirement_id, consultant_id)
);

-- Table: public.job_postings
CREATE TABLE IF NOT EXISTS public.job_postings (
    id integer DEFAULT nextval('job_postings_id_seq'::regclass) NOT NULL,
    source_email_id integer,
    company character varying(255),
    role character varying(255),
    pay_rate character varying(100),
    technologies jsonb DEFAULT '[]'::jsonb,
    city character varying(255),
    visa_type character varying(50),
    raw_subject text,
    raw_summary text,
    status character varying(20) DEFAULT 'new'::character varying,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    CONSTRAINT job_postings_pkey PRIMARY KEY (id)
);

-- Table: public.job_updates
CREATE TABLE IF NOT EXISTS public.job_updates (
    id integer DEFAULT nextval('job_updates_id_seq'::regclass) NOT NULL,
    application_id integer,
    email_id integer,
    update_type character varying(20) NOT NULL,
    interview_date timestamp without time zone,
    notes text,
    created_at timestamp without time zone DEFAULT now(),
    CONSTRAINT job_updates_pkey PRIMARY KEY (id)
);

-- Table: public.manual_review_queue
CREATE TABLE IF NOT EXISTS public.manual_review_queue (
    id bigint DEFAULT nextval('manual_review_queue_id_seq'::regclass) NOT NULL,
    error_id bigint NOT NULL,
    assigned_admin_id text,
    status text NOT NULL,
    correction_data jsonb,
    review_notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT manual_review_queue_pkey PRIMARY KEY (id)
);

-- Table: public.matching_run_status
CREATE TABLE IF NOT EXISTS public.matching_run_status (
    id bigint DEFAULT nextval('matching_run_status_id_seq'::regclass) NOT NULL,
    status text NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    total_requirements integer NOT NULL,
    processed_requirements integer NOT NULL,
    new_matches integer NOT NULL,
    error text,
    CONSTRAINT matching_run_status_pkey PRIMARY KEY (id)
);

-- Table: public.message_templates
CREATE TABLE IF NOT EXISTS public.message_templates (
    id integer DEFAULT nextval('message_templates_id_seq'::regclass) NOT NULL,
    name character varying(255) NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT message_templates_pkey PRIMARY KEY (id)
);

-- Table: public.message_templates_backup_names
CREATE TABLE IF NOT EXISTS public.message_templates_backup_names (
    id integer,
    name character varying(255)
);

-- Table: public.notifications
CREATE TABLE IF NOT EXISTS public.notifications (
    id bigint DEFAULT nextval('notifications_id_seq'::regclass) NOT NULL,
    user_id bigint NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    is_read boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    consultant_id bigint,
    CONSTRAINT notifications_pkey PRIMARY KEY (id)
);

-- Table: public.openai_batch_jobs
CREATE TABLE IF NOT EXISTS public.openai_batch_jobs (
    id integer DEFAULT nextval('openai_batch_jobs_id_seq'::regclass) NOT NULL,
    batch_id character varying(255) NOT NULL,
    email_ids integer[] NOT NULL,
    status character varying(50) DEFAULT 'pending'::character varying,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT openai_batch_jobs_pkey PRIMARY KEY (id)
);

-- Table: public.processing_errors
CREATE TABLE IF NOT EXISTS public.processing_errors (
    id bigint DEFAULT nextval('processing_errors_id_seq'::regclass) NOT NULL,
    source_type text,
    source_id text,
    error_stage text NOT NULL,
    error_message text NOT NULL,
    stack_trace text,
    raw_payload text,
    status text NOT NULL,
    retry_count integer NOT NULL,
    last_retry_at timestamp with time zone,
    raw_email_id text,
    requirement_id text,
    consultant_id text,
    additional_context jsonb,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    CONSTRAINT processing_errors_pkey PRIMARY KEY (id)
);

-- Table: public.processing_errors_backup_20261002
CREATE TABLE IF NOT EXISTS public.processing_errors_backup_20261002 (
    id bigint,
    source_type text,
    source_id text,
    error_stage text,
    error_message text,
    stack_trace text,
    raw_payload text,
    status text,
    retry_count integer,
    last_retry_at timestamp with time zone,
    raw_email_id text,
    requirement_id text,
    consultant_id text,
    additional_context jsonb,
    occurred_at timestamp with time zone,
    resolved_at timestamp with time zone
);

-- Table: public.recruiter_consultants
CREATE TABLE IF NOT EXISTS public.recruiter_consultants (
    id bigint DEFAULT nextval('recruiter_consultants_id_seq'::regclass) NOT NULL,
    recruiter_id bigint NOT NULL,
    consultant_id bigint NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT recruiter_consultants_pkey PRIMARY KEY (id),
    CONSTRAINT uq_recruiter_consultant UNIQUE (recruiter_id, consultant_id)
);

-- Table: public.requirement_consultant_matches
CREATE TABLE IF NOT EXISTS public.requirement_consultant_matches (
    id bigint DEFAULT nextval('requirement_consultant_matches_id_seq'::regclass) NOT NULL,
    requirement_id bigint NOT NULL,
    consultant_id bigint NOT NULL,
    match_score numeric(5, 2) NOT NULL,
    skill_score numeric(5, 2),
    role_score numeric(5, 2),
    experience_score numeric(5, 2),
    employment_score numeric(5, 2),
    location_score numeric(5, 2),
    auth_score numeric(5, 2),
    matched_skills text[],
    missing_skills text[],
    match_reason text,
    status text DEFAULT 'MATCHING'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    score_breakdown jsonb,
    resume_status text,
    tier text,
    CONSTRAINT requirement_consultant_matches_pkey PRIMARY KEY (id),
    CONSTRAINT uq_requirement_consultant_match UNIQUE (requirement_id, consultant_id)
);

-- Table: public.requirements
CREATE TABLE IF NOT EXISTS public.requirements (
    id bigint DEFAULT nextval('requirements_id_seq'::regclass) NOT NULL,
    recruiter_id bigint,
    raw_email_id bigint,
    role text NOT NULL,
    vendor text,
    vendor_email text,
    vendor_contact text,
    client text,
    location text,
    work_mode text,
    employment_types text[],
    rate text,
    duration text,
    job_description text,
    jd_hash text,
    dedup_key text,
    parsed_fields jsonb,
    parse_confidence numeric(5, 2),
    ats_match_count integer,
    status text NOT NULL,
    received_date timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    experience text,
    skills text,
    work_authorization text,
    account_email text,
    mode character varying(20) DEFAULT 'public'::character varying NOT NULL,
    CONSTRAINT requirements_pkey PRIMARY KEY (id),
    CONSTRAINT chk_requirements_mode CHECK (((mode)::text = ANY ((ARRAY['public'::character varying, 'private'::character varying])::text[])))
);

-- Table: public.resumes
CREATE TABLE IF NOT EXISTS public.resumes (
    id bigint DEFAULT nextval('resumes_id_seq'::regclass) NOT NULL,
    user_id bigint NOT NULL,
    title character varying(255) NOT NULL,
    target_role character varying(255),
    job_description text,
    data jsonb NOT NULL,
    s3_key character varying(500),
    ats_score integer,
    status character varying(50) NOT NULL,
    download_count integer NOT NULL,
    last_downloaded timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    requirement_id bigint,
    template character varying(50) DEFAULT 'classic'::character varying,
    CONSTRAINT resumes_pkey PRIMARY KEY (id)
);

-- Table: public.sync_logs
CREATE TABLE IF NOT EXISTS public.sync_logs (
    id integer DEFAULT nextval('sync_logs_id_seq'::regclass) NOT NULL,
    account_id integer,
    account_email character varying(255),
    started_at timestamp with time zone DEFAULT now(),
    finished_at timestamp with time zone,
    emails_found integer,
    emails_saved integer,
    status character varying(20),
    error_msg text,
    duration_ms integer,
    CONSTRAINT sync_logs_pkey PRIMARY KEY (id)
);

-- Table: public.system_events
CREATE TABLE IF NOT EXISTS public.system_events (
    id bigint DEFAULT nextval('system_events_id_seq'::regclass) NOT NULL,
    event_type text NOT NULL,
    payload jsonb,
    broadcast_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT system_events_pkey PRIMARY KEY (id)
);

-- Table: public.training_labels
CREATE TABLE IF NOT EXISTS public.training_labels (
    id integer DEFAULT nextval('training_labels_id_seq'::regclass) NOT NULL,
    email_id integer NOT NULL,
    label character varying(20) NOT NULL,
    labeled_by character varying(100),
    created_at timestamp without time zone DEFAULT now(),
    CONSTRAINT training_labels_pkey PRIMARY KEY (id),
    CONSTRAINT training_labels_email_id_key UNIQUE (email_id)
);

-- Table: public.users
CREATE TABLE IF NOT EXISTS public.users (
    id bigint DEFAULT nextval('users_id_seq'::regclass) NOT NULL,
    full_name text NOT NULL,
    email text NOT NULL,
    password_hash text,
    role text NOT NULL,
    is_authorized boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    needto_fetch_mail boolean DEFAULT true NOT NULL,
    skills jsonb DEFAULT '[]'::jsonb NOT NULL,
    experience_years numeric,
    resume_info jsonb DEFAULT '[]'::jsonb NOT NULL,
    last_login_at timestamp with time zone,
    mobile_number text,
    extension text,
    linkedin_url text,
    designation text,
    email_signature text,
    allowed_to_send boolean DEFAULT false NOT NULL,
    CONSTRAINT users_pkey PRIMARY KEY (id)
);

-- -----------------------------------------------------------------------------
-- SEQUENCE OWNERSHIP
-- -----------------------------------------------------------------------------
ALTER SEQUENCE public.ai_usage_logs_id_seq OWNED BY public.ai_usage_logs.id;
ALTER SEQUENCE public.applications_id_seq OWNED BY public.applications.id;
ALTER SEQUENCE public.audit_logs_id_seq OWNED BY public.audit_logs.id;
ALTER SEQUENCE public.batch_jobs_id_seq OWNED BY public.batch_jobs.id;
ALTER SEQUENCE public.consultant_email_tokens_id_seq OWNED BY public.consultant_email_tokens.id;
ALTER SEQUENCE public.consultant_experience_id_seq OWNED BY public.consultant_experience.id;
ALTER SEQUENCE public.consultants_id_seq OWNED BY public.consultants.id;
ALTER SEQUENCE public.email_accounts_id_seq OWNED BY public.email_accounts.id;
ALTER SEQUENCE public.email_queue_id_seq OWNED BY public.email_queue.id;
ALTER SEQUENCE public.emails_id_seq OWNED BY public.emails.id;
ALTER SEQUENCE public.feedback_id_seq OWNED BY public.feedback.id;
ALTER SEQUENCE public.feedback_image_id_seq OWNED BY public.feedback_image.id;
ALTER SEQUENCE public.generated_resumes_id_seq OWNED BY public.generated_resumes.id;
ALTER SEQUENCE public.gmail_accounts_id_seq OWNED BY public.gmail_accounts.id;
ALTER SEQUENCE public.gmail_emails_id_seq OWNED BY public.gmail_emails.id;
ALTER SEQUENCE public.gmail_sync_logs_id_seq OWNED BY public.gmail_sync_logs.id;
ALTER SEQUENCE public.job_matches_id_seq OWNED BY public.job_matches.id;
ALTER SEQUENCE public.job_postings_id_seq OWNED BY public.job_postings.id;
ALTER SEQUENCE public.job_updates_id_seq OWNED BY public.job_updates.id;
ALTER SEQUENCE public.manual_review_queue_id_seq OWNED BY public.manual_review_queue.id;
ALTER SEQUENCE public.matching_run_status_id_seq OWNED BY public.matching_run_status.id;
ALTER SEQUENCE public.message_templates_id_seq OWNED BY public.message_templates.id;
ALTER SEQUENCE public.notifications_id_seq OWNED BY public.notifications.id;
ALTER SEQUENCE public.openai_batch_jobs_id_seq OWNED BY public.openai_batch_jobs.id;
ALTER SEQUENCE public.processing_errors_id_seq OWNED BY public.processing_errors.id;
ALTER SEQUENCE public.recruiter_consultants_id_seq OWNED BY public.recruiter_consultants.id;
ALTER SEQUENCE public.requirement_consultant_matches_id_seq OWNED BY public.requirement_consultant_matches.id;
ALTER SEQUENCE public.requirements_id_seq OWNED BY public.requirements.id;
ALTER SEQUENCE public.resumes_id_seq OWNED BY public.resumes.id;
ALTER SEQUENCE public.sync_logs_id_seq OWNED BY public.sync_logs.id;
ALTER SEQUENCE public.system_events_id_seq OWNED BY public.system_events.id;
ALTER SEQUENCE public.training_labels_id_seq OWNED BY public.training_labels.id;
ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;

-- -----------------------------------------------------------------------------
-- 4. FOREIGN KEY CONSTRAINTS
-- -----------------------------------------------------------------------------
-- Foreign keys for public.applications
ALTER TABLE ONLY public.applications ADD CONSTRAINT applications_consultant_id_fkey FOREIGN KEY (consultant_id) REFERENCES consultants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.applications ADD CONSTRAINT applications_generated_resume_id_fkey FOREIGN KEY (generated_resume_id) REFERENCES generated_resumes(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.applications ADD CONSTRAINT applications_requirement_id_fkey FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.applications ADD CONSTRAINT fk_applications_job_match_id FOREIGN KEY (job_match_id) REFERENCES job_matches(id) ON DELETE SET NULL;

-- Foreign keys for public.consultant_email_tokens
ALTER TABLE ONLY public.consultant_email_tokens ADD CONSTRAINT consultant_email_tokens_consultant_id_fkey FOREIGN KEY (consultant_id) REFERENCES consultants(id) ON DELETE CASCADE;

-- Foreign keys for public.consultant_experience
ALTER TABLE ONLY public.consultant_experience ADD CONSTRAINT consultant_experience_consultant_id_fkey FOREIGN KEY (consultant_id) REFERENCES consultants(id) ON DELETE CASCADE;

-- Foreign keys for public.email_queue
ALTER TABLE ONLY public.email_queue ADD CONSTRAINT email_queue_consultant_id_fkey FOREIGN KEY (consultant_id) REFERENCES consultants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.email_queue ADD CONSTRAINT email_queue_requirement_id_fkey FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.email_queue ADD CONSTRAINT email_queue_sent_by_user_id_fkey FOREIGN KEY (sent_by_user_id) REFERENCES users(id) ON DELETE SET NULL;

-- Foreign keys for public.feedback
ALTER TABLE ONLY public.feedback ADD CONSTRAINT feedback_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;

-- Foreign keys for public.feedback_image
ALTER TABLE ONLY public.feedback_image ADD CONSTRAINT feedback_image_feedback_id_fkey FOREIGN KEY (feedback_id) REFERENCES feedback(id) ON DELETE CASCADE;

-- Foreign keys for public.generated_resumes
ALTER TABLE ONLY public.generated_resumes ADD CONSTRAINT generated_resumes_consultant_id_fkey FOREIGN KEY (consultant_id) REFERENCES consultants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.generated_resumes ADD CONSTRAINT generated_resumes_requirement_id_fkey FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE CASCADE;

-- Foreign keys for public.gmail_emails
ALTER TABLE ONLY public.gmail_emails ADD CONSTRAINT fk_gmail_emails_job_posting FOREIGN KEY (job_posting_id) REFERENCES job_postings(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.gmail_emails ADD CONSTRAINT gmail_emails_account_id_fkey FOREIGN KEY (account_id) REFERENCES gmail_accounts(id) ON DELETE CASCADE;

-- Foreign keys for public.gmail_sync_logs
ALTER TABLE ONLY public.gmail_sync_logs ADD CONSTRAINT gmail_sync_logs_account_id_fkey FOREIGN KEY (account_id) REFERENCES gmail_accounts(id) ON DELETE CASCADE;

-- Foreign keys for public.job_matches
ALTER TABLE ONLY public.job_matches ADD CONSTRAINT fk_job_matches_consultants FOREIGN KEY (consultant_id) REFERENCES consultants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.job_matches ADD CONSTRAINT fk_job_matches_requirements FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE CASCADE;

-- Foreign keys for public.job_postings
ALTER TABLE ONLY public.job_postings ADD CONSTRAINT job_postings_source_email_id_fkey FOREIGN KEY (source_email_id) REFERENCES gmail_emails(id) ON DELETE SET NULL;

-- Foreign keys for public.job_updates
ALTER TABLE ONLY public.job_updates ADD CONSTRAINT job_updates_application_id_fkey FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.job_updates ADD CONSTRAINT job_updates_email_id_fkey FOREIGN KEY (email_id) REFERENCES gmail_emails(id) ON DELETE SET NULL;

-- Foreign keys for public.manual_review_queue
ALTER TABLE ONLY public.manual_review_queue ADD CONSTRAINT manual_review_queue_error_id_fkey FOREIGN KEY (error_id) REFERENCES processing_errors(id);

-- Foreign keys for public.notifications
ALTER TABLE ONLY public.notifications ADD CONSTRAINT notifications_consultant_id_fkey FOREIGN KEY (consultant_id) REFERENCES consultants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.notifications ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;

-- Foreign keys for public.recruiter_consultants
ALTER TABLE ONLY public.recruiter_consultants ADD CONSTRAINT recruiter_consultants_consultant_id_fkey FOREIGN KEY (consultant_id) REFERENCES consultants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.recruiter_consultants ADD CONSTRAINT recruiter_consultants_recruiter_id_fkey FOREIGN KEY (recruiter_id) REFERENCES users(id) ON DELETE CASCADE;

-- Foreign keys for public.requirement_consultant_matches
ALTER TABLE ONLY public.requirement_consultant_matches ADD CONSTRAINT requirement_consultant_matches_consultant_id_fkey FOREIGN KEY (consultant_id) REFERENCES consultants(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.requirement_consultant_matches ADD CONSTRAINT requirement_consultant_matches_requirement_id_fkey FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE CASCADE;

-- Foreign keys for public.requirements
ALTER TABLE ONLY public.requirements ADD CONSTRAINT requirements_raw_email_id_fkey FOREIGN KEY (raw_email_id) REFERENCES gmail_emails(id) ON DELETE SET NULL;

-- Foreign keys for public.resumes
ALTER TABLE ONLY public.resumes ADD CONSTRAINT resumes_requirement_id_fkey FOREIGN KEY (requirement_id) REFERENCES requirements(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.resumes ADD CONSTRAINT resumes_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;

-- Foreign keys for public.sync_logs
ALTER TABLE ONLY public.sync_logs ADD CONSTRAINT sync_logs_account_id_fkey FOREIGN KEY (account_id) REFERENCES email_accounts(id) ON DELETE SET NULL;

-- Foreign keys for public.training_labels
ALTER TABLE ONLY public.training_labels ADD CONSTRAINT training_labels_email_id_fkey FOREIGN KEY (email_id) REFERENCES gmail_emails(id) ON DELETE CASCADE;

-- -----------------------------------------------------------------------------
-- 5. TRIGGER FUNCTIONS
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_sync_gmail_account_mode()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    -- Only an e-mail ADDRESS change needs the child rows re-linked.
    -- A mode change is intentionally NOT propagated any more.
    IF NEW.email IS DISTINCT FROM OLD.email THEN
        UPDATE public.gmail_emails
        SET account_email = NEW.email
        WHERE account_id = NEW.id
           OR LOWER(TRIM(account_email)) = LOWER(TRIM(OLD.email));

        UPDATE public.requirements r
        SET account_email = NEW.email
        FROM public.gmail_emails ge
        WHERE r.raw_email_id = ge.id
          AND (ge.account_id = NEW.id OR LOWER(TRIM(ge.account_email)) = LOWER(TRIM(NEW.email)));
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.rap_pause_gmail_on_consultant_inactive()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    UPDATE consultant_email_tokens SET is_active = FALSE
     WHERE consultant_id = NEW.id AND is_active = TRUE;
    RETURN NULL;
END;
$function$;

CREATE OR REPLACE FUNCTION public.rap_pause_gmail_on_user_deauthorized()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    UPDATE consultant_email_tokens SET is_active = FALSE
     WHERE is_active = TRUE
       AND consultant_id IN (SELECT id FROM consultants WHERE user_id = NEW.id);
    RETURN NULL;
END;
$function$;

CREATE OR REPLACE FUNCTION public.rap_sync_consultant_gmail_connected()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP <> 'DELETE' THEN
        UPDATE consultants c
           SET gmail_connected = EXISTS (
                   SELECT 1 FROM consultant_email_tokens t
                    WHERE t.consultant_id = NEW.consultant_id AND t.is_active)
         WHERE c.id = NEW.consultant_id
           AND c.gmail_connected IS DISTINCT FROM EXISTS (
                   SELECT 1 FROM consultant_email_tokens t
                    WHERE t.consultant_id = NEW.consultant_id AND t.is_active);
    END IF;

    IF TG_OP = 'DELETE'
       OR (TG_OP = 'UPDATE' AND OLD.consultant_id IS DISTINCT FROM NEW.consultant_id) THEN
        UPDATE consultants c
           SET gmail_connected = EXISTS (
                   SELECT 1 FROM consultant_email_tokens t
                    WHERE t.consultant_id = OLD.consultant_id AND t.is_active)
         WHERE c.id = OLD.consultant_id;
    END IF;

    RETURN NULL;
END;
$function$;

CREATE OR REPLACE FUNCTION public.update_gmail_accounts_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$function$;

CREATE OR REPLACE FUNCTION public.update_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$function$;

-- -----------------------------------------------------------------------------
-- 6. TRIGGERS
-- -----------------------------------------------------------------------------
CREATE TRIGGER trg_rap_sync_consultant_gmail_connected AFTER INSERT OR DELETE OR UPDATE ON public.consultant_email_tokens FOR EACH ROW EXECUTE FUNCTION rap_sync_consultant_gmail_connected();

CREATE TRIGGER trg_rap_pause_gmail_on_consultant_inactive AFTER UPDATE OF status ON public.consultants FOR EACH ROW WHEN (((new.status = 'INACTIVE'::text) AND (old.status IS DISTINCT FROM new.status))) EXECUTE FUNCTION rap_pause_gmail_on_consultant_inactive();

CREATE TRIGGER trg_gmail_accounts_updated BEFORE UPDATE ON public.gmail_accounts FOR EACH ROW EXECUTE FUNCTION update_gmail_accounts_updated_at();

CREATE TRIGGER trg_sync_gmail_account_mode AFTER UPDATE OF email ON public.gmail_accounts FOR EACH ROW WHEN (((old.email)::text IS DISTINCT FROM (new.email)::text)) EXECUTE FUNCTION fn_sync_gmail_account_mode();

CREATE TRIGGER trg_rap_pause_gmail_on_user_deauthorized AFTER UPDATE OF is_authorized ON public.users FOR EACH ROW WHEN (((new.is_authorized = false) AND (old.is_authorized IS DISTINCT FROM new.is_authorized))) EXECUTE FUNCTION rap_pause_gmail_on_user_deauthorized();

-- -----------------------------------------------------------------------------
-- 7. SECONDARY, COMPOSITE, COVERING, PARTIAL & GIN INDEXES
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_ai_usage_logs_model ON public.ai_usage_logs USING btree (model);
CREATE INDEX IF NOT EXISTS ix_ai_usage_consultant_created ON public.ai_usage_logs USING btree (consultant_id, created_at DESC) WHERE (consultant_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ai_usage_created_incl_cost ON public.ai_usage_logs USING btree (created_at DESC) INCLUDE (purpose, estimated_cost, input_tokens, output_tokens);
CREATE INDEX IF NOT EXISTS ix_ai_usage_logs_created_at ON public.ai_usage_logs USING btree (created_at);
CREATE INDEX IF NOT EXISTS ix_ai_usage_logs_id ON public.ai_usage_logs USING btree (id);
CREATE INDEX IF NOT EXISTS ix_ai_usage_logs_purpose ON public.ai_usage_logs USING btree (purpose);
CREATE INDEX IF NOT EXISTS ix_ai_usage_purpose_created ON public.ai_usage_logs USING btree (purpose, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_applications_candidate ON public.applications USING btree (candidate_id);
CREATE INDEX IF NOT EXISTS idx_applications_posting ON public.applications USING btree (job_posting_id);
CREATE INDEX IF NOT EXISTS idx_applications_status ON public.applications USING btree (status);
CREATE INDEX IF NOT EXISTS ix_applications_cons_req_status ON public.applications USING btree (consultant_id, requirement_id, status);
CREATE INDEX IF NOT EXISTS ix_applications_consultant_created_at ON public.applications USING btree (consultant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_applications_consultant_id ON public.applications USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_applications_consultant_status ON public.applications USING btree (consultant_id, status);
CREATE INDEX IF NOT EXISTS ix_applications_created_range ON public.applications USING btree (created_at);
CREATE INDEX IF NOT EXISTS ix_applications_gmail_message_id ON public.applications USING btree (gmail_message_id);
CREATE INDEX IF NOT EXISTS ix_applications_id ON public.applications USING btree (id);
CREATE INDEX IF NOT EXISTS ix_applications_recruiter_status ON public.applications USING btree (recruiter_id, status);
CREATE INDEX IF NOT EXISTS ix_applications_requirement_id ON public.applications USING btree (requirement_id);
CREATE INDEX IF NOT EXISTS ix_applications_sent_at_desc ON public.applications USING btree (sent_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS ix_applications_sent_range ON public.applications USING btree (sent_at) WHERE (status = 'SENT'::text);
CREATE INDEX IF NOT EXISTS ix_applications_sent_status ON public.applications USING btree (status, sent_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS ix_applications_status_sent_at ON public.applications USING btree (status, sent_at DESC);
CREATE INDEX IF NOT EXISTS ix_apps_cons_req_status ON public.applications USING btree (consultant_id, requirement_id, status);
CREATE INDEX IF NOT EXISTS ix_apps_consultant_created_desc ON public.applications USING btree (consultant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_apps_consultant_status_sent ON public.applications USING btree (consultant_id, status, sent_at DESC);
CREATE INDEX IF NOT EXISTS ix_apps_created_status ON public.applications USING btree (created_at DESC) INCLUDE (status, recruiter_id, consultant_id);
CREATE INDEX IF NOT EXISTS ix_apps_effective_sent_at ON public.applications USING btree (COALESCE(sent_at, created_at) DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_apps_recruiter_effective_sent ON public.applications USING btree (recruiter_id, COALESCE(sent_at, created_at) DESC);
CREATE INDEX IF NOT EXISTS ix_apps_recruiter_status_sent ON public.applications USING btree (recruiter_id, status, sent_at DESC);
CREATE INDEX IF NOT EXISTS ix_apps_req_cons_sent_partial ON public.applications USING btree (requirement_id, consultant_id, sent_at DESC) WHERE (status = 'SENT'::text);
CREATE INDEX IF NOT EXISTS ix_apps_sent_partial_cons ON public.applications USING btree (consultant_id, sent_at DESC) INCLUDE (created_at) WHERE (status = 'SENT'::text);
CREATE INDEX IF NOT EXISTS ix_apps_status_sent_at ON public.applications USING btree (status, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON public.audit_logs USING btree (action);
CREATE INDEX IF NOT EXISTS ix_audit_logs_action ON public.audit_logs USING btree (action);
CREATE INDEX IF NOT EXISTS ix_audit_logs_action_created ON public.audit_logs USING btree (action, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON public.audit_logs USING btree (created_at);
CREATE INDEX IF NOT EXISTS ix_audit_logs_duplicate_created ON public.audit_logs USING btree (created_at DESC) WHERE ((meta ->> 'is_duplicate'::text) = 'true'::text);
CREATE INDEX IF NOT EXISTS ix_audit_logs_entity_type ON public.audit_logs USING btree (entity_type);
CREATE INDEX IF NOT EXISTS ix_audit_logs_id ON public.audit_logs USING btree (id);
CREATE INDEX IF NOT EXISTS ix_audit_logs_meta_consultant ON public.audit_logs USING btree (((meta ->> 'consultant_id'::text)), created_at DESC) WHERE (meta ? 'consultant_id'::text);
CREATE UNIQUE INDEX IF NOT EXISTS ix_consultant_email_tokens_consultant_id ON public.consultant_email_tokens USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_consultant_email_tokens_id ON public.consultant_email_tokens USING btree (id);
CREATE INDEX IF NOT EXISTS ix_consultant_exp_cons_id ON public.consultant_experience USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_consultant_experience_consultant_id ON public.consultant_experience USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_consultant_experience_id ON public.consultant_experience USING btree (id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_consultants_email ON public.consultants USING btree (email);
CREATE INDEX IF NOT EXISTS ix_consultants_email_trgm ON public.consultants USING gin (email gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_consultants_full_name_trgm ON public.consultants USING gin (full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_consultants_id ON public.consultants USING btree (id);
CREATE INDEX IF NOT EXISTS ix_consultants_lower_email ON public.consultants USING btree (lower(email));
CREATE INDEX IF NOT EXISTS ix_consultants_sales_recruiter ON public.consultants USING btree (sales_recruiter_user_id) WHERE (sales_recruiter_user_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS ix_consultants_status_lower_name ON public.consultants USING btree (status, lower(full_name), id);
CREATE INDEX IF NOT EXISTS ix_consultants_status_updated ON public.consultants USING btree (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_consultants_user_id ON public.consultants USING btree (user_id);
CREATE INDEX IF NOT EXISTS ix_consultants_user_status ON public.consultants USING btree (user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ix_email_accounts_email ON public.email_accounts USING btree (email);
CREATE INDEX IF NOT EXISTS ix_email_accounts_id ON public.email_accounts USING btree (id);
CREATE INDEX IF NOT EXISTS ix_email_queue_consultant_created_desc ON public.email_queue USING btree (consultant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_email_queue_consultant_id ON public.email_queue USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_email_queue_consultant_status ON public.email_queue USING btree (consultant_id, status);
CREATE INDEX IF NOT EXISTS ix_email_queue_created_range ON public.email_queue USING btree (created_at) WHERE (status = 'SENT'::text);
CREATE INDEX IF NOT EXISTS ix_email_queue_from_email_scheduled ON public.email_queue USING btree (from_email, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS ix_email_queue_from_email_trgm ON public.email_queue USING gin (from_email gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_email_queue_id ON public.email_queue USING btree (id);
CREATE INDEX IF NOT EXISTS ix_email_queue_lower_from_status ON public.email_queue USING btree (lower(from_email), status) INCLUDE (scheduled_at, created_at);
CREATE INDEX IF NOT EXISTS ix_email_queue_req_cons_status ON public.email_queue USING btree (requirement_id, consultant_id, status);
CREATE INDEX IF NOT EXISTS ix_email_queue_requirement_id ON public.email_queue USING btree (requirement_id);
CREATE INDEX IF NOT EXISTS ix_email_queue_sender_status_created ON public.email_queue USING btree (sent_by_user_id, status, created_at DESC) WHERE (sent_by_user_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS ix_email_queue_sent_by_user ON public.email_queue USING btree (sent_by_user_id, status);
CREATE INDEX IF NOT EXISTS ix_email_queue_status_created ON public.email_queue USING btree (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_email_queue_status_created_desc ON public.email_queue USING btree (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_email_queue_status_scheduled ON public.email_queue USING btree (status, scheduled_at) WHERE (status = ANY (ARRAY['QUEUED'::text, 'PROCESSING'::text]));
CREATE INDEX IF NOT EXISTS ix_email_queue_subject_trgm ON public.email_queue USING gin (subject gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_email_queue_to_email_trgm ON public.email_queue USING gin (to_email gin_trgm_ops);
CREATE UNIQUE INDEX IF NOT EXISTS ix_emails_gmail_message_id ON public.emails USING btree (gmail_message_id);
CREATE INDEX IF NOT EXISTS ix_emails_id ON public.emails USING btree (id);
CREATE INDEX IF NOT EXISTS ix_emails_parse_received ON public.emails USING btree (parse_status, received_at);
CREATE INDEX IF NOT EXISTS ix_emails_parse_status_received ON public.emails USING btree (parse_status, received_at DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_created_at ON public.feedback USING btree (created_at);
CREATE INDEX IF NOT EXISTS ix_feedback_id ON public.feedback USING btree (id);
CREATE INDEX IF NOT EXISTS ix_feedback_status ON public.feedback USING btree (status);
CREATE INDEX IF NOT EXISTS ix_feedback_status_created ON public.feedback USING btree (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_user_created ON public.feedback USING btree (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_user_id ON public.feedback USING btree (user_id);
CREATE INDEX IF NOT EXISTS ix_feedback_image_feedback_id ON public.feedback_image USING btree (feedback_id);
CREATE INDEX IF NOT EXISTS ix_feedback_image_id ON public.feedback_image USING btree (id);
CREATE INDEX IF NOT EXISTS ix_generated_resumes_cons_final ON public.generated_resumes USING btree (consultant_id) WHERE (is_final = true);
CREATE INDEX IF NOT EXISTS ix_generated_resumes_cons_req ON public.generated_resumes USING btree (consultant_id, requirement_id);
CREATE INDEX IF NOT EXISTS ix_generated_resumes_cons_req_created ON public.generated_resumes USING btree (consultant_id, requirement_id, created_at);
CREATE INDEX IF NOT EXISTS ix_generated_resumes_consultant_id ON public.generated_resumes USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_generated_resumes_id ON public.generated_resumes USING btree (id);
CREATE INDEX IF NOT EXISTS ix_generated_resumes_req_cons_final ON public.generated_resumes USING btree (requirement_id, consultant_id) WHERE (is_final = true);
CREATE INDEX IF NOT EXISTS ix_generated_resumes_requirement_id ON public.generated_resumes USING btree (requirement_id);
CREATE INDEX IF NOT EXISTS ix_gmail_accounts_email_lower ON public.gmail_accounts USING btree (lower((email)::text));
CREATE INDEX IF NOT EXISTS ix_gmail_accounts_email_mode ON public.gmail_accounts USING btree (lower(TRIM(BOTH FROM email))) INCLUDE (mode, active);
CREATE INDEX IF NOT EXISTS ix_gmail_accounts_mode ON public.gmail_accounts USING btree (mode);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_account_email ON public.gmail_emails USING btree (account_email);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_account_id ON public.gmail_emails USING btree (account_id);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_category ON public.gmail_emails USING btree (category);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_coalesce_date ON public.gmail_emails USING btree (COALESCE(fetched_at, date));
CREATE INDEX IF NOT EXISTS idx_gmail_emails_date ON public.gmail_emails USING btree (date DESC);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_fetched_at ON public.gmail_emails USING btree (fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_from ON public.gmail_emails USING btree (from_address);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_from_address_trgm ON public.gmail_emails USING gin (from_address gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_message_id ON public.gmail_emails USING btree (message_id);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_priority ON public.gmail_emails USING btree (priority);
CREATE INDEX IF NOT EXISTS idx_gmail_emails_processed ON public.gmail_emails USING btree (processed);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_account_effective_date ON public.gmail_emails USING btree (account_email, COALESCE(fetched_at, date) DESC);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_body_fts ON public.gmail_emails USING gin (to_tsvector('simple'::regconfig, "left"(COALESCE(NULLIF(body_text, ''::text), body_html), 200000)));
CREATE INDEX IF NOT EXISTS ix_gmail_emails_body_trgm ON public.gmail_emails USING gin (COALESCE(NULLIF(body_text, ''::text), body_html) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_category ON public.gmail_emails USING btree (category);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_classified_at ON public.gmail_emails USING btree (classified_at);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_classified_at_desc ON public.gmail_emails USING btree (classified_at DESC) WHERE (classified_at IS NOT NULL);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_date_desc ON public.gmail_emails USING btree (date DESC);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_effective_date_desc ON public.gmail_emails USING btree (COALESCE(fetched_at, date) DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_from_address_trgm ON public.gmail_emails USING gin (from_address gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_from_name_trgm ON public.gmail_emails USING gin (from_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_mode_account_email ON public.gmail_emails USING btree (mode, lower((account_email)::text));
CREATE INDEX IF NOT EXISTS ix_gmail_emails_recent ON public.gmail_emails USING btree (COALESCE(fetched_at, date) DESC);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_status_desc ON public.gmail_emails USING btree (status_desc);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_status_processed ON public.gmail_emails USING btree (status_desc, processed);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_subject_trgm ON public.gmail_emails USING gin (subject gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_gmail_emails_unclassified_partial ON public.gmail_emails USING btree (id) WHERE (((category IS NULL) OR ((category)::text = 'unclassified'::text)) AND ((status_desc IS NULL) OR (status_desc = 'Pending'::text)));
CREATE INDEX IF NOT EXISTS ix_gmail_emails_worker_status_partial ON public.gmail_emails USING btree (status_desc, id) WHERE (status_desc = ANY (ARRAY['Reparse Requested'::text, 'Reparsing'::text, 'Batch Queued'::text, 'Batch Submitted'::text]));
CREATE INDEX IF NOT EXISTS idx_gmail_sync_logs_account ON public.gmail_sync_logs USING btree (account_id);
CREATE INDEX IF NOT EXISTS ix_gmail_sync_logs_started_desc ON public.gmail_sync_logs USING btree (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_job_matches_consultant_id ON public.job_matches USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_job_matches_id ON public.job_matches USING btree (id);
CREATE INDEX IF NOT EXISTS ix_job_matches_req_cons ON public.job_matches USING btree (requirement_id, consultant_id);
CREATE INDEX IF NOT EXISTS ix_job_matches_requirement_id ON public.job_matches USING btree (requirement_id);
CREATE INDEX IF NOT EXISTS ix_job_matches_status ON public.job_matches USING btree (status);
CREATE INDEX IF NOT EXISTS ix_job_matches_status_consultant_created_desc ON public.job_matches USING btree (status, consultant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_job_matches_status_created_desc ON public.job_matches USING btree (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_postings_city ON public.job_postings USING btree (city);
CREATE INDEX IF NOT EXISTS idx_job_postings_visa ON public.job_postings USING btree (visa_type);
CREATE INDEX IF NOT EXISTS idx_job_updates_application ON public.job_updates USING btree (application_id);
CREATE INDEX IF NOT EXISTS idx_job_updates_type ON public.job_updates USING btree (update_type);
CREATE INDEX IF NOT EXISTS ix_manual_review_error_id ON public.manual_review_queue USING btree (error_id);
CREATE INDEX IF NOT EXISTS ix_manual_review_queue_id ON public.manual_review_queue USING btree (id);
CREATE INDEX IF NOT EXISTS ix_manual_review_queue_status ON public.manual_review_queue USING btree (status);
CREATE INDEX IF NOT EXISTS ix_manual_review_status_created ON public.manual_review_queue USING btree (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_notifications_consultant_id ON public.notifications USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_notifications_id ON public.notifications USING btree (id);
CREATE INDEX IF NOT EXISTS ix_notifications_user_id ON public.notifications USING btree (user_id);
CREATE INDEX IF NOT EXISTS ix_notifications_user_unread ON public.notifications USING btree (user_id, is_read);
CREATE INDEX IF NOT EXISTS ix_notifs_user_created_desc ON public.notifications USING btree (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_notifs_user_read_created ON public.notifications USING btree (user_id, is_read, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_notifs_user_unread_partial ON public.notifications USING btree (user_id, created_at DESC) WHERE (is_read = false);
CREATE INDEX IF NOT EXISTS ix_processing_errors_error_stage ON public.processing_errors USING btree (error_stage);
CREATE INDEX IF NOT EXISTS ix_processing_errors_id ON public.processing_errors USING btree (id);
CREATE INDEX IF NOT EXISTS ix_processing_errors_occurred_at ON public.processing_errors USING btree (occurred_at);
CREATE INDEX IF NOT EXISTS ix_processing_errors_open_partial ON public.processing_errors USING btree (occurred_at DESC) WHERE (status = 'OPEN'::text);
CREATE INDEX IF NOT EXISTS ix_processing_errors_source ON public.processing_errors USING btree (source_type, source_id);
CREATE INDEX IF NOT EXISTS ix_processing_errors_stage_status ON public.processing_errors USING btree (error_stage, status);
CREATE INDEX IF NOT EXISTS ix_processing_errors_status ON public.processing_errors USING btree (status);
CREATE INDEX IF NOT EXISTS ix_processing_errors_status_occurred ON public.processing_errors USING btree (status, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_rc_consultant_active ON public.recruiter_consultants USING btree (consultant_id, is_active, recruiter_id);
CREATE INDEX IF NOT EXISTS ix_rc_recruiter_active ON public.recruiter_consultants USING btree (recruiter_id, is_active, consultant_id);
CREATE INDEX IF NOT EXISTS ix_rc_recruiter_active_partial ON public.recruiter_consultants USING btree (recruiter_id, consultant_id) WHERE (is_active = true);
CREATE INDEX IF NOT EXISTS ix_recruiter_consultants_cons_active ON public.recruiter_consultants USING btree (consultant_id, is_active);
CREATE INDEX IF NOT EXISTS ix_recruiter_consultants_consultant_id ON public.recruiter_consultants USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_recruiter_consultants_id ON public.recruiter_consultants USING btree (id);
CREATE INDEX IF NOT EXISTS ix_recruiter_consultants_is_active ON public.recruiter_consultants USING btree (is_active);
CREATE INDEX IF NOT EXISTS ix_recruiter_consultants_rec_active ON public.recruiter_consultants USING btree (recruiter_id, is_active);
CREATE INDEX IF NOT EXISTS ix_recruiter_consultants_recruiter_id ON public.recruiter_consultants USING btree (recruiter_id);
CREATE INDEX IF NOT EXISTS ix_rcm_cons_status_created ON public.requirement_consultant_matches USING btree (consultant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rcm_created_desc_incl_req ON public.requirement_consultant_matches USING btree (created_at DESC) INCLUDE (requirement_id, consultant_id, status);
CREATE INDEX IF NOT EXISTS ix_rcm_matching_cons_req ON public.requirement_consultant_matches USING btree (consultant_id, requirement_id) WHERE (status = 'MATCHING'::text);
CREATE INDEX IF NOT EXISTS ix_rcm_matching_cons_req_created ON public.requirement_consultant_matches USING btree (consultant_id, requirement_id, created_at DESC) WHERE (status = 'MATCHING'::text);
CREATE INDEX IF NOT EXISTS ix_rcm_matching_created_partial ON public.requirement_consultant_matches USING btree (created_at DESC, consultant_id, requirement_id) WHERE (status = 'MATCHING'::text);
CREATE INDEX IF NOT EXISTS ix_rcm_rejected_effective_updated ON public.requirement_consultant_matches USING btree (COALESCE(updated_at, created_at) DESC) WHERE (status = 'REJECTED'::text);
CREATE INDEX IF NOT EXISTS ix_rcm_req_matching_applied_partial ON public.requirement_consultant_matches USING btree (requirement_id, consultant_id) WHERE (status = ANY (ARRAY['MATCHING'::text, 'APPLIED'::text]));
CREATE INDEX IF NOT EXISTS ix_rcm_req_status ON public.requirement_consultant_matches USING btree (requirement_id, status);
CREATE INDEX IF NOT EXISTS ix_rcm_req_status_id ON public.requirement_consultant_matches USING btree (requirement_id, status, id);
CREATE INDEX IF NOT EXISTS ix_rcm_status_created_score_id ON public.requirement_consultant_matches USING btree (status, created_at DESC, match_score DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_req_cons_matches_cons_score ON public.requirement_consultant_matches USING btree (consultant_id, match_score DESC);
CREATE INDEX IF NOT EXISTS ix_req_cons_matches_req_score ON public.requirement_consultant_matches USING btree (requirement_id, match_score DESC);
CREATE INDEX IF NOT EXISTS ix_req_consultant_match_req_cons ON public.requirement_consultant_matches USING btree (requirement_id, consultant_id);
CREATE INDEX IF NOT EXISTS ix_requirement_consultant_matches_consultant_id ON public.requirement_consultant_matches USING btree (consultant_id);
CREATE INDEX IF NOT EXISTS ix_requirement_consultant_matches_id ON public.requirement_consultant_matches USING btree (id);
CREATE INDEX IF NOT EXISTS ix_requirement_consultant_matches_requirement_id ON public.requirement_consultant_matches USING btree (requirement_id);
CREATE INDEX IF NOT EXISTS idx_requirements_raw_email_id ON public.requirements USING btree (raw_email_id);
CREATE INDEX IF NOT EXISTS idx_requirements_received_date ON public.requirements USING btree (received_date DESC);
CREATE INDEX IF NOT EXISTS ix_requirements_client_trgm ON public.requirements USING gin (client gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_requirements_created_desc ON public.requirements USING btree (created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ix_requirements_dedup_key ON public.requirements USING btree (dedup_key);
CREATE INDEX IF NOT EXISTS ix_requirements_employment_types_gin ON public.requirements USING gin (employment_types);
CREATE INDEX IF NOT EXISTS ix_requirements_id ON public.requirements USING btree (id);
CREATE INDEX IF NOT EXISTS ix_requirements_jd_hash ON public.requirements USING btree (jd_hash);
CREATE INDEX IF NOT EXISTS ix_requirements_location_trgm ON public.requirements USING gin (location gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_requirements_mode_account_email ON public.requirements USING btree (upper((COALESCE(mode, ''::character varying))::text), lower(COALESCE(account_email, ''::text)));
CREATE INDEX IF NOT EXISTS ix_requirements_rate_trgm ON public.requirements USING gin (rate gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_requirements_raw_email_id ON public.requirements USING btree (raw_email_id) WHERE (raw_email_id IS NOT NULL);
CREATE INDEX IF NOT EXISTS ix_requirements_received_id_desc ON public.requirements USING btree (received_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_requirements_role_trgm ON public.requirements USING gin (role gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_requirements_status ON public.requirements USING btree (status);
CREATE INDEX IF NOT EXISTS ix_requirements_status_id ON public.requirements USING btree (status, id);
CREATE INDEX IF NOT EXISTS ix_requirements_status_received ON public.requirements USING btree (status, received_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_requirements_vendor_email_trgm ON public.requirements USING gin (vendor_email gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_requirements_vendor_trgm ON public.requirements USING gin (vendor gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_requirements_work_mode_norm ON public.requirements USING btree (upper(replace(replace(work_mode, '-'::text, ''::text), ' '::text, ''::text)));
CREATE INDEX IF NOT EXISTS ix_requirements_work_mode_trgm ON public.requirements USING gin (work_mode gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_resumes_id ON public.resumes USING btree (id);
CREATE INDEX IF NOT EXISTS ix_resumes_requirement_id ON public.resumes USING btree (requirement_id);
CREATE INDEX IF NOT EXISTS ix_resumes_status ON public.resumes USING btree (status);
CREATE INDEX IF NOT EXISTS ix_resumes_user_active_partial ON public.resumes USING btree (user_id, created_at DESC) WHERE (deleted_at IS NULL);
CREATE INDEX IF NOT EXISTS ix_resumes_user_created_desc ON public.resumes USING btree (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_resumes_user_created_incl_ats ON public.resumes USING btree (user_id, created_at DESC) INCLUDE (ats_score, status);
CREATE INDEX IF NOT EXISTS ix_resumes_user_id ON public.resumes USING btree (user_id);
CREATE INDEX IF NOT EXISTS ix_resumes_user_req_completed ON public.resumes USING btree (user_id, requirement_id) WHERE ((status)::text = 'completed'::text);
CREATE INDEX IF NOT EXISTS ix_sync_logs_id ON public.sync_logs USING btree (id);
CREATE INDEX IF NOT EXISTS ix_system_events_broadcast_at ON public.system_events USING btree (broadcast_at);
CREATE INDEX IF NOT EXISTS ix_system_events_event_type ON public.system_events USING btree (event_type);
CREATE INDEX IF NOT EXISTS ix_system_events_id ON public.system_events USING btree (id);
CREATE INDEX IF NOT EXISTS ix_system_events_type_broadcast ON public.system_events USING btree (event_type, broadcast_at DESC);
CREATE INDEX IF NOT EXISTS idx_training_labels_label ON public.training_labels USING btree (label);
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON public.users USING btree (email);
CREATE INDEX IF NOT EXISTS ix_users_email_covering ON public.users USING btree (email) INCLUDE (id, full_name, role, is_authorized, allowed_to_send);
CREATE INDEX IF NOT EXISTS ix_users_full_name_asc ON public.users USING btree (full_name);
CREATE INDEX IF NOT EXISTS ix_users_id ON public.users USING btree (id);
CREATE INDEX IF NOT EXISTS ix_users_role_active ON public.users USING btree (role, is_authorized);
CREATE INDEX IF NOT EXISTS ix_users_role_authorized ON public.users USING btree (role, is_authorized) INCLUDE (id, email, full_name);

-- -----------------------------------------------------------------------------
-- 8. DATABASE VIEWS
-- -----------------------------------------------------------------------------
-- View: public.gmail_sync_status
DROP VIEW IF EXISTS public.gmail_sync_status CASCADE;
CREATE OR REPLACE VIEW public.gmail_sync_status AS
SELECT ea.id,
    ea.email,
    ea.label,
    ea.active,
    ea.last_synced,
    ea.sync_errors,
    count(e.id) AS total_emails,
    max(e.date) AS latest_email_date,
    sl.status AS last_sync_status,
    sl.emails_saved AS last_sync_count,
    COALESCE(ea.mode, 'public'::character varying) AS mode
   FROM ((gmail_accounts ea
     LEFT JOIN gmail_emails e ON ((e.account_id = ea.id)))
     LEFT JOIN LATERAL ( SELECT gmail_sync_logs.status,
            gmail_sync_logs.emails_saved
           FROM gmail_sync_logs
          WHERE (gmail_sync_logs.account_id = ea.id)
          ORDER BY gmail_sync_logs.started_at DESC
         LIMIT 1) sl ON (true))
  GROUP BY ea.id, ea.email, ea.label, ea.active, ea.last_synced, ea.sync_errors, sl.status, sl.emails_saved, ea.mode;

-- View: public.v_active_matches
DROP VIEW IF EXISTS public.v_active_matches CASCADE;
CREATE OR REPLACE VIEW public.v_active_matches AS
SELECT m.id,
    m.requirement_id,
    r.role AS requirement_title,
    COALESCE(r.client, r.vendor) AS requirement_company,
    r.vendor_email AS requirement_vendor_email,
    r.raw_email_id,
    r.account_email,
    r.mode,
    m.consultant_id,
    c.full_name AS consultant_name,
    c.email AS consultant_email,
    m.match_score,
    m.score_breakdown,
    m.match_reason,
    m.status,
    m.resume_status,
    m.created_at,
    m.updated_at,
    COALESCE(m.updated_at, m.created_at) AS effective_updated_at,
    (a.id IS NOT NULL) AS has_sent_application
   FROM ((((requirement_consultant_matches m
     JOIN requirements r ON ((r.id = m.requirement_id)))
     JOIN consultants c ON ((c.id = m.consultant_id)))
     JOIN users u ON ((u.id = c.user_id)))
     LEFT JOIN applications a ON (((a.requirement_id = m.requirement_id) AND (a.consultant_id = m.consultant_id) AND (a.status = 'SENT'::text))))
  WHERE ((c.status = 'ACTIVE'::text) AND (u.is_authorized = true));

-- View: public.v_application_tracker_details
DROP VIEW IF EXISTS public.v_application_tracker_details CASCADE;
CREATE OR REPLACE VIEW public.v_application_tracker_details AS
SELECT a.id,
    a.consultant_id,
    c.full_name AS consultant_name,
    c.email AS consultant_email,
    a.requirement_id,
    r.role AS requirement_title,
    COALESCE(r.client, r.vendor) AS company_name,
    r.client,
    r.vendor,
    a.recruiter_id,
    u.full_name AS recruiter_name,
    u.role AS recruiter_role,
    a.status,
    a.vendor_email,
    a.cc_email,
    a.email_subject,
    a.gmail_message_id,
    a.generated_resume_id,
    a.resume_attachment_path,
    ((a.generated_resume_id IS NOT NULL) OR (a.resume_attachment_path IS NOT NULL)) AS resume_available,
    a.sent_at,
    a.applied_at,
    a.created_at,
    a.updated_at,
    a.error_message,
    r.account_email AS requirement_account_email,
    COALESCE(r.mode, 'public'::character varying) AS requirement_mode
   FROM (((applications a
     JOIN consultants c ON ((a.consultant_id = c.id)))
     JOIN requirements r ON ((a.requirement_id = r.id)))
     LEFT JOIN users u ON ((a.recruiter_id = u.id)));

-- View: public.v_applications_detail
DROP VIEW IF EXISTS public.v_applications_detail CASCADE;
CREATE OR REPLACE VIEW public.v_applications_detail AS
SELECT a.id AS application_id,
    a.consultant_id,
    a.requirement_id,
    a.recruiter_id,
    a.generated_resume_id,
    a.status,
    a.gmail_message_id,
    a.email_subject,
    a.email_body_preview,
    a.sent_at,
    a.applied_at,
    a.created_at,
    a.updated_at,
    a.vendor_email,
    a.ats_score_at_send,
    a.error_message,
    a.resume_attachment_path,
    r.client AS requirement_client,
    r.role AS requirement_role,
    r.job_description AS requirement_job_description,
    r.vendor_email AS requirement_vendor_email,
    c.full_name AS consultant_name,
    c.email AS consultant_email,
    u.full_name AS recruiter_name,
    u.email AS recruiter_email,
    r.account_email AS requirement_account_email,
    COALESCE(r.mode, 'public'::character varying) AS requirement_mode
   FROM (((applications a
     LEFT JOIN requirements r ON ((r.id = a.requirement_id)))
     LEFT JOIN consultants c ON ((c.id = a.consultant_id)))
     LEFT JOIN users u ON ((u.id = a.recruiter_id)));

-- View: public.v_consultant_directory_metrics
DROP VIEW IF EXISTS public.v_consultant_directory_metrics CASCADE;
CREATE OR REPLACE VIEW public.v_consultant_directory_metrics AS
SELECT c.id AS consultant_id,
    c.user_id,
    u.is_authorized AS user_is_authorized,
    u.resume_info AS user_resume_info,
    ( SELECT count(*) AS count
           FROM consultant_experience ce
          WHERE (ce.consultant_id = c.id)) AS experience_count,
    ( SELECT count(*) AS count
           FROM applications a
          WHERE ((a.consultant_id = c.id) AND (a.status = 'SENT'::text))) AS active_application_count,
    ( SELECT rs.ats_score
           FROM resumes rs
          WHERE (rs.user_id = c.user_id)
          ORDER BY rs.created_at DESC
         LIMIT 1) AS latest_ats_score
   FROM (consultants c
     LEFT JOIN users u ON ((u.id = c.user_id)));

-- View: public.v_dashboard_kpis
DROP VIEW IF EXISTS public.v_dashboard_kpis CASCADE;
CREATE OR REPLACE VIEW public.v_dashboard_kpis AS
SELECT ge.total_emails,
    ge.processed_emails,
    ge.pending_emails,
    ge.failed_emails,
    ge.rep_emails_processed,
    ( SELECT count(*) AS count
           FROM requirements) AS total_requirements,
    ( SELECT count(*) AS count
           FROM consultants
          WHERE (consultants.status = 'ACTIVE'::text)) AS active_consultants,
    ( SELECT count(*) AS count
           FROM generated_resumes
          WHERE (generated_resumes.is_final = true)) AS resumes_generated,
    ( SELECT count(*) AS count
           FROM applications
          WHERE (applications.status = 'SENT'::text)) AS applications_sent,
    ( SELECT count(*) AS count
           FROM email_queue
          WHERE (email_queue.status = ANY (ARRAY['QUEUED'::text, 'RETRY'::text, 'SENDING'::text]))) AS pending_queue,
    ( SELECT count(*) AS count
           FROM processing_errors
          WHERE ((processing_errors.status = 'OPEN'::text) AND (processing_errors.occurred_at >= (now() - '24:00:00'::interval)))) AS errors_24h,
    ( SELECT count(*) AS count
           FROM audit_logs) AS total_audit_logs
   FROM ( SELECT count(*) AS total_emails,
            count(*) FILTER (WHERE (gmail_emails.processed = true)) AS processed_emails,
            count(*) FILTER (WHERE ((gmail_emails.processed = false) AND (COALESCE(gmail_emails.status_desc, ''::text) !~~ 'Failed%'::text))) AS pending_emails,
            count(*) FILTER (WHERE (gmail_emails.status_desc ~~ 'Failed%'::text)) AS failed_emails,
            count(*) FILTER (WHERE ((gmail_emails.status_desc = ANY (ARRAY['Parsed'::text, 'Parsed - NR'::text, 'Parsed - Dup'::text])) OR ((gmail_emails.status_desc IS NULL) AND (gmail_emails.processed = true)))) AS rep_emails_processed
           FROM gmail_emails) ge;

-- View: public.v_email_queue_details
DROP VIEW IF EXISTS public.v_email_queue_details CASCADE;
CREATE OR REPLACE VIEW public.v_email_queue_details AS
SELECT eq.id,
    eq.consultant_id,
    c.full_name AS consultant_name,
    c.email AS consultant_email,
    eq.requirement_id,
    r.role AS requirement_title,
    r.client AS requirement_client,
    r.vendor AS requirement_vendor,
    eq.from_email,
    eq.to_email,
    eq.cc_email,
    eq.subject,
    eq.content,
    eq.html_content,
    eq.attachments,
    eq.status,
    eq.status_text,
    eq.sent_by_user_id,
    u.full_name AS sent_by_name,
    u.role AS sent_by_role,
    eq.scheduled_at,
    eq.created_at,
    eq.updated_at,
    r.account_email AS requirement_account_email,
    COALESCE(r.mode, 'public'::character varying) AS requirement_mode
   FROM (((email_queue eq
     LEFT JOIN consultants c ON ((eq.consultant_id = c.id)))
     LEFT JOIN requirements r ON ((eq.requirement_id = r.id)))
     LEFT JOIN users u ON ((eq.sent_by_user_id = u.id)));

-- View: public.v_pending_job_matches
DROP VIEW IF EXISTS public.v_pending_job_matches CASCADE;
CREATE OR REPLACE VIEW public.v_pending_job_matches AS
SELECT jm.id,
    jm.requirement_id,
    r.role AS requirement_title,
    COALESCE(r.client, r.vendor) AS requirement_company,
    jm.consultant_id,
    c.full_name AS consultant_name,
    c.email AS consultant_email,
    jm.match_score,
    jm.match_reasoning,
    jm.status,
    jm.created_at,
    r.account_email AS requirement_account_email,
    COALESCE(r.mode, 'public'::character varying) AS requirement_mode
   FROM ((job_matches jm
     JOIN requirements r ON ((jm.requirement_id = r.id)))
     JOIN consultants c ON ((jm.consultant_id = c.id)));

-- View: public.v_pending_matches_active
DROP VIEW IF EXISTS public.v_pending_matches_active CASCADE;
CREATE OR REPLACE VIEW public.v_pending_matches_active AS
SELECT rcm.id,
    rcm.requirement_id,
    r.role AS requirement_title,
    COALESCE(r.client, r.vendor) AS requirement_company,
    r.vendor_email AS requirement_vendor_email,
    r.raw_email_id,
    rcm.consultant_id,
    c.full_name AS consultant_name,
    c.email AS consultant_email,
    rcm.match_score,
    rcm.score_breakdown,
    rcm.match_reason,
    rcm.status,
    rcm.resume_status,
    rcm.created_at,
    rcm.updated_at,
    c.user_id,
    (EXISTS ( SELECT 1
           FROM applications a
          WHERE ((a.requirement_id = rcm.requirement_id) AND (a.consultant_id = rcm.consultant_id) AND (a.status = 'SENT'::text)))) AS has_sent_application,
    r.account_email AS requirement_account_email,
    COALESCE(r.mode, 'public'::character varying) AS requirement_mode
   FROM (((requirement_consultant_matches rcm
     JOIN requirements r ON ((rcm.requirement_id = r.id)))
     JOIN consultants c ON ((rcm.consultant_id = c.id)))
     JOIN users u ON ((u.id = c.user_id)))
  WHERE ((c.status = 'ACTIVE'::text) AND (u.is_authorized = true));

-- View: public.v_requirement_match_counts
DROP VIEW IF EXISTS public.v_requirement_match_counts CASCADE;
CREATE OR REPLACE VIEW public.v_requirement_match_counts AS
SELECT requirement_consultant_matches.requirement_id,
    count(requirement_consultant_matches.id) FILTER (WHERE (requirement_consultant_matches.status = ANY (ARRAY['MATCHING'::text, 'APPLIED'::text]))) AS match_total,
    count(requirement_consultant_matches.id) FILTER (WHERE (requirement_consultant_matches.status = 'REJECTED'::text)) AS rejected_total
   FROM requirement_consultant_matches
  GROUP BY requirement_consultant_matches.requirement_id;

-- View: public.v_requirements_with_mode
DROP VIEW IF EXISTS public.v_requirements_with_mode CASCADE;
CREATE OR REPLACE VIEW public.v_requirements_with_mode AS
SELECT r.id,
    r.recruiter_id,
    r.raw_email_id,
    COALESCE(r.account_email, (ge.account_email)::text) AS account_email,
    COALESCE(lower((r.mode)::text), lower((ga.mode)::text), lower((ge.mode)::text), 'public'::text) AS mode,
    r.role,
    r.vendor,
    r.vendor_email,
    r.vendor_contact,
    r.client,
    r.location,
    r.work_mode,
    r.employment_types,
    r.rate,
    r.duration,
    r.experience,
    r.skills,
    r.work_authorization,
    r.parse_confidence,
    r.ats_match_count,
    r.status,
    r.received_date,
    r.created_at,
    r.updated_at,
    u.id AS owner_user_id,
    u.full_name AS owner_user_name,
    u.role AS owner_user_role
   FROM (((requirements r
     LEFT JOIN gmail_emails ge ON ((r.raw_email_id = ge.id)))
     LEFT JOIN gmail_accounts ga ON ((lower(TRIM(BOTH FROM COALESCE(r.account_email, (ge.account_email)::text))) = lower(TRIM(BOTH FROM ga.email)))))
     LEFT JOIN users u ON ((lower(TRIM(BOTH FROM COALESCE(r.account_email, (ge.account_email)::text))) = lower(TRIM(BOTH FROM u.email)))));

-- View: public.v_user_reporting_stats
DROP VIEW IF EXISTS public.v_user_reporting_stats CASCADE;
CREATE OR REPLACE VIEW public.v_user_reporting_stats AS
SELECT u.id AS user_id,
    u.full_name AS user_name,
    u.role AS user_role,
    COALESCE(app_counts.applications_sent, (0)::bigint) AS applications_sent,
    COALESCE(eq_counts.emails_sent, (0)::bigint) AS emails_sent
   FROM ((users u
     LEFT JOIN ( SELECT applications.recruiter_id,
            count(*) AS applications_sent
           FROM applications
          WHERE ((applications.status = 'SENT'::text) AND (applications.recruiter_id IS NOT NULL))
          GROUP BY applications.recruiter_id) app_counts ON ((u.id = app_counts.recruiter_id)))
     LEFT JOIN ( SELECT email_queue.sent_by_user_id,
            count(*) AS emails_sent
           FROM email_queue
          WHERE ((email_queue.status = 'SENT'::text) AND (email_queue.sent_by_user_id IS NOT NULL))
          GROUP BY email_queue.sent_by_user_id) eq_counts ON ((u.id = eq_counts.sent_by_user_id)));
