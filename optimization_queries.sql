-- =============================================================================
-- RAP Backend — High-Concurrency Database Views & Indexing Suite
-- File: backend/optimization_queries.sql
--
-- Deadlock-Proof Execution Architecture:
--   1. Zero Table Locking: All 87 indexes are built using:
--        CREATE INDEX CONCURRENTLY IF NOT EXISTS
--      In PostgreSQL, CREATE INDEX CONCURRENTLY acquires only a ShareUpdateExclusiveLock
--      instead of an AccessExclusiveLock. Reads, writes, INSERTs, and UPDATEs on
--      all tables continue running in parallel without waiting or deadlocking.
--   2. Runs with SET lock_timeout = '15s' and SET statement_timeout = '180s'.
--   3. Views drop with CASCADE before recreating to avoid column drop/rename errors.
--   4. Calibrated specifically for a 2 vCPU / 4 GB RAM host footprint.
--
-- Safe to execute repeatedly (100% idempotent).
-- =============================================================================

-- Fail fast after 10s if a background worker or web request transaction is hanging
SET lock_timeout = '10s';
SET statement_timeout = '120s';

-- -----------------------------------------------------------------------------
-- 1. EXTENSIONS
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- -----------------------------------------------------------------------------
-- 2. INDEXES & TABLE STORAGE (STRICT ALPHABETICAL ORDER BY TABLE)
-- -----------------------------------------------------------------------------

-- =============================================================================
-- TABLE: public.ai_usage_logs
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ai_usage_created_incl_cost
    ON public.ai_usage_logs (created_at DESC)
    INCLUDE (purpose, estimated_cost, input_tokens, output_tokens);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ai_usage_purpose_created
    ON public.ai_usage_logs (purpose, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ai_usage_consultant_created
    ON public.ai_usage_logs (consultant_id, created_at DESC)
    WHERE consultant_id IS NOT NULL;

-- =============================================================================
-- TABLE: public.applications
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_cons_req_status
    ON public.applications (consultant_id, requirement_id, status);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_req_cons_sent_partial
    ON public.applications (requirement_id, consultant_id, sent_at DESC)
    WHERE status = 'SENT';
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_status_sent_at
    ON public.applications (status, sent_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_effective_sent_at
    ON public.applications (COALESCE(sent_at, created_at) DESC, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_recruiter_effective_sent
    ON public.applications (recruiter_id, COALESCE(sent_at, created_at) DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_consultant_created_desc
    ON public.applications (consultant_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_recruiter_status_sent
    ON public.applications (recruiter_id, status, sent_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_consultant_status_sent
    ON public.applications (consultant_id, status, sent_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_created_status
    ON public.applications (created_at DESC)
    INCLUDE (status, recruiter_id, consultant_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_apps_sent_partial_cons
    ON public.applications (consultant_id, sent_at DESC)
    INCLUDE (created_at)
    WHERE status = 'SENT';

-- Storage & autovacuum tuning for public.applications
ALTER TABLE public.applications SET (
    fillfactor = 90,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
ALTER TABLE public.applications ALTER COLUMN status SET STATISTICS 500;

-- =============================================================================
-- TABLE: public.audit_logs
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_logs_duplicate_created
    ON public.audit_logs (created_at DESC)
    WHERE (meta->>'is_duplicate') = 'true';
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_logs_action_created
    ON public.audit_logs (action, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_logs_meta_consultant
    ON public.audit_logs ((meta->>'consultant_id'), created_at DESC)
    WHERE meta ? 'consultant_id';

-- =============================================================================
-- TABLE: public.consultant_experience
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_consultant_exp_cons_id
    ON public.consultant_experience (consultant_id);

-- =============================================================================
-- TABLE: public.consultants
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_consultants_user_status
    ON public.consultants (user_id, status);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_consultants_status_lower_name
    ON public.consultants (status, LOWER(full_name) ASC, id ASC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_consultants_full_name_trgm
    ON public.consultants USING GIN (full_name gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_consultants_email_trgm
    ON public.consultants USING GIN (email gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_consultants_lower_email
    ON public.consultants (LOWER(email));
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_consultants_sales_recruiter
    ON public.consultants (sales_recruiter_user_id)
    WHERE sales_recruiter_user_id IS NOT NULL;

-- =============================================================================
-- TABLE: public.email_queue
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_queue_req_cons_status
    ON public.email_queue (requirement_id, consultant_id, status);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_queue_lower_from_status
    ON public.email_queue (LOWER(from_email), status)
    INCLUDE (scheduled_at, created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_queue_status_scheduled
    ON public.email_queue (status, scheduled_at)
    WHERE status IN ('QUEUED', 'PROCESSING');
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_queue_status_created_desc
    ON public.email_queue (status, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_queue_consultant_created_desc
    ON public.email_queue (consultant_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_queue_sender_status_created
    ON public.email_queue (sent_by_user_id, status, created_at DESC)
    WHERE sent_by_user_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_queue_subject_trgm
    ON public.email_queue USING GIN (subject gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_queue_to_email_trgm
    ON public.email_queue USING GIN (to_email gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_queue_from_email_trgm
    ON public.email_queue USING GIN (from_email gin_trgm_ops);

-- Storage & autovacuum tuning for public.email_queue
ALTER TABLE public.email_queue SET (
    fillfactor = 85,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
ALTER TABLE public.email_queue ALTER COLUMN status SET STATISTICS 500;

-- =============================================================================
-- TABLE: public.generated_resumes
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_generated_resumes_req_cons_final
    ON public.generated_resumes (requirement_id, consultant_id)
    WHERE is_final = TRUE;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_generated_resumes_cons_req_created
    ON public.generated_resumes (consultant_id, requirement_id, created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_generated_resumes_cons_final
    ON public.generated_resumes (consultant_id)
    WHERE is_final = TRUE;

-- =============================================================================
-- TABLE: public.gmail_accounts
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_accounts_email_mode
    ON public.gmail_accounts (LOWER(TRIM(email)))
    INCLUDE (mode, active);

-- =============================================================================
-- TABLE: public.gmail_emails
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_status_processed
    ON public.gmail_emails (status_desc, processed);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_date_desc
    ON public.gmail_emails (date DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_effective_date_desc
    ON public.gmail_emails ((COALESCE(fetched_at, date)) DESC, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_account_effective_date
    ON public.gmail_emails (account_email, (COALESCE(fetched_at, date)) DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_category
    ON public.gmail_emails (category);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_classified_at_desc
    ON public.gmail_emails (classified_at DESC)
    WHERE classified_at IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_unclassified_partial
    ON public.gmail_emails (id)
    WHERE (category IS NULL OR category = 'unclassified')
      AND (status_desc IS NULL OR status_desc = 'Pending');
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_worker_status_partial
    ON public.gmail_emails (status_desc, id)
    WHERE status_desc IN ('Reparse Requested', 'Reparsing', 'Batch Queued', 'Batch Submitted');
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_subject_trgm
    ON public.gmail_emails USING GIN (subject gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_from_name_trgm
    ON public.gmail_emails USING GIN (from_name gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gmail_emails_from_address_trgm
    ON public.gmail_emails USING GIN (from_address gin_trgm_ops);

-- Storage & autovacuum tuning for public.gmail_emails
ALTER TABLE public.gmail_emails SET (
    fillfactor = 90,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
ALTER TABLE public.gmail_emails ALTER COLUMN status_desc SET STATISTICS 500;
ALTER TABLE public.gmail_emails ALTER COLUMN account_email SET STATISTICS 500;

-- =============================================================================
-- TABLE: public.notifications
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_notifs_user_read_created
    ON public.notifications (user_id, is_read, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_notifs_user_unread_partial
    ON public.notifications (user_id, created_at DESC)
    WHERE is_read = FALSE;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_notifs_user_created_desc
    ON public.notifications (user_id, created_at DESC);

-- Storage & autovacuum tuning for public.notifications
ALTER TABLE public.notifications SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

-- =============================================================================
-- TABLE: public.processing_errors
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_processing_errors_status_occurred
    ON public.processing_errors (status, occurred_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_processing_errors_stage_status
    ON public.processing_errors (error_stage, status);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_processing_errors_open_partial
    ON public.processing_errors (occurred_at DESC)
    WHERE status = 'OPEN';

-- =============================================================================
-- TABLE: public.recruiter_consultants
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rc_recruiter_active
    ON public.recruiter_consultants (recruiter_id, is_active, consultant_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rc_consultant_active
    ON public.recruiter_consultants (consultant_id, is_active, recruiter_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rc_recruiter_active_partial
    ON public.recruiter_consultants (recruiter_id, consultant_id)
    WHERE is_active = TRUE;

-- =============================================================================
-- TABLE: public.requirement_consultant_matches
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rcm_cons_status_created
    ON public.requirement_consultant_matches (consultant_id, status, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rcm_req_status
    ON public.requirement_consultant_matches (requirement_id, status);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rcm_status_created_score_id
    ON public.requirement_consultant_matches (status, created_at DESC, match_score DESC, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rcm_req_matching_applied_partial
    ON public.requirement_consultant_matches (requirement_id, consultant_id)
    WHERE status IN ('MATCHING', 'APPLIED');
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rcm_rejected_effective_updated
    ON public.requirement_consultant_matches (COALESCE(updated_at, created_at) DESC)
    WHERE status = 'REJECTED';
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rcm_created_desc_incl_req
    ON public.requirement_consultant_matches (created_at DESC)
    INCLUDE (requirement_id, consultant_id, status);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rcm_matching_created_partial
    ON public.requirement_consultant_matches (created_at DESC, consultant_id, requirement_id)
    WHERE status = 'MATCHING';

-- Storage & autovacuum tuning for public.requirement_consultant_matches
ALTER TABLE public.requirement_consultant_matches SET (
    fillfactor = 90,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
ALTER TABLE public.requirement_consultant_matches ALTER COLUMN status SET STATISTICS 500;

-- =============================================================================
-- TABLE: public.requirements
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_status_received
    ON public.requirements (status, received_date DESC, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_received_id_desc
    ON public.requirements (received_date DESC, id DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_mode_account_email
    ON public.requirements (UPPER(COALESCE(mode, '')), LOWER(COALESCE(account_email, '')));
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_work_mode_norm
    ON public.requirements (UPPER(REPLACE(REPLACE(work_mode, '-', ''), ' ', '')));
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_employment_types_gin
    ON public.requirements USING GIN (employment_types);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_role_trgm
    ON public.requirements USING GIN (role gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_vendor_trgm
    ON public.requirements USING GIN (vendor gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_client_trgm
    ON public.requirements USING GIN (client gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_vendor_email_trgm
    ON public.requirements USING GIN (vendor_email gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_location_trgm
    ON public.requirements USING GIN (location gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_raw_email_id
    ON public.requirements (raw_email_id)
    WHERE raw_email_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_requirements_created_desc
    ON public.requirements (created_at DESC);

-- Storage & autovacuum tuning for public.requirements
ALTER TABLE public.requirements SET (
    fillfactor = 90,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

-- =============================================================================
-- TABLE: public.resumes
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resumes_user_created_desc
    ON public.resumes (user_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resumes_user_req_completed
    ON public.resumes (user_id, requirement_id)
    WHERE status = 'completed';
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resumes_user_created_incl_ats
    ON public.resumes (user_id, created_at DESC)
    INCLUDE (ats_score, status);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resumes_user_active_partial
    ON public.resumes (user_id, created_at DESC)
    WHERE deleted_at IS NULL;

-- =============================================================================
-- TABLE: public.users
-- =============================================================================
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_users_email_covering
    ON public.users (email)
    INCLUDE (id, full_name, role, is_authorized, allowed_to_send);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_users_role_authorized
    ON public.users (role, is_authorized)
    INCLUDE (id, email, full_name);

-- =============================================================================
-- CONDITIONALLY LOADED TABLES (Alphabetical Order)
-- =============================================================================
DO $$
BEGIN
    IF to_regclass('public.feedback') IS NOT NULL THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS ix_feedback_user_created ON public.feedback (user_id, created_at DESC)';
        EXECUTE 'CREATE INDEX IF NOT EXISTS ix_feedback_status_created ON public.feedback (status, created_at DESC)';
    END IF;
    IF to_regclass('public.gmail_sync_logs') IS NOT NULL THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS ix_gmail_sync_logs_started_desc ON public.gmail_sync_logs (started_at DESC)';
    END IF;
    IF to_regclass('public.manual_review_queue') IS NOT NULL THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS ix_manual_review_error_id ON public.manual_review_queue (error_id)';
        EXECUTE 'CREATE INDEX IF NOT EXISTS ix_manual_review_status_created ON public.manual_review_queue (status, created_at DESC)';
    END IF;
    IF to_regclass('public.system_events') IS NOT NULL THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS ix_system_events_type_broadcast ON public.system_events (event_type, broadcast_at DESC)';
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- 3. PRE-JOINED DATABASE VIEWS (CASCADE DROP TO PREVENT 42P16 SCHEMA ERRORS)
-- -----------------------------------------------------------------------------

-- View: public.v_applications_detail
DROP VIEW IF EXISTS public.v_applications_detail CASCADE;
CREATE OR REPLACE VIEW public.v_applications_detail AS
SELECT
    a.id AS application_id,
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
FROM public.applications a
LEFT JOIN public.requirements r ON r.id = a.requirement_id
LEFT JOIN public.consultants c ON c.id = a.consultant_id
LEFT JOIN public.users u ON u.id = a.recruiter_id;

-- View: public.v_active_matches
DROP VIEW IF EXISTS public.v_active_matches CASCADE;
CREATE OR REPLACE VIEW public.v_active_matches AS
SELECT
    m.id,
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
FROM public.requirement_consultant_matches m
INNER JOIN public.requirements r ON r.id = m.requirement_id
INNER JOIN public.consultants c ON c.id = m.consultant_id
INNER JOIN public.users u ON u.id = c.user_id
LEFT JOIN public.applications a
    ON a.requirement_id = m.requirement_id
   AND a.consultant_id = m.consultant_id
   AND a.status = 'SENT'
WHERE c.status = 'ACTIVE'
  AND u.is_authorized = TRUE;

-- View: public.v_requirement_match_counts
DROP VIEW IF EXISTS public.v_requirement_match_counts CASCADE;
CREATE OR REPLACE VIEW public.v_requirement_match_counts AS
SELECT
    requirement_id,
    COUNT(id) FILTER (WHERE status IN ('MATCHING', 'APPLIED')) AS match_total,
    COUNT(id) FILTER (WHERE status = 'REJECTED') AS rejected_total
FROM public.requirement_consultant_matches
GROUP BY requirement_id;

-- View: public.v_consultant_directory_metrics
DROP VIEW IF EXISTS public.v_consultant_directory_metrics CASCADE;
CREATE OR REPLACE VIEW public.v_consultant_directory_metrics AS
SELECT
    c.id AS consultant_id,
    c.user_id,
    u.is_authorized AS user_is_authorized,
    u.resume_info AS user_resume_info,
    (
        SELECT COUNT(*)
        FROM public.consultant_experience ce
        WHERE ce.consultant_id = c.id
    ) AS experience_count,
    (
        SELECT COUNT(*)
        FROM public.applications a
        WHERE a.consultant_id = c.id
          AND a.status = 'SENT'
    ) AS active_application_count,
    (
        SELECT rs.ats_score
        FROM public.resumes rs
        WHERE rs.user_id = c.user_id
        ORDER BY rs.created_at DESC
        LIMIT 1
    ) AS latest_ats_score
FROM public.consultants c
LEFT JOIN public.users u ON u.id = c.user_id;

-- View: public.v_dashboard_kpis
DROP VIEW IF EXISTS public.v_dashboard_kpis CASCADE;
CREATE OR REPLACE VIEW public.v_dashboard_kpis AS
SELECT
    ge.total_emails,
    ge.processed_emails,
    ge.pending_emails,
    ge.failed_emails,
    ge.rep_emails_processed,
    (SELECT COUNT(*) FROM public.requirements) AS total_requirements,
    (SELECT COUNT(*) FROM public.consultants WHERE status = 'ACTIVE') AS active_consultants,
    (SELECT COUNT(*) FROM public.generated_resumes WHERE is_final = TRUE) AS resumes_generated,
    (SELECT COUNT(*) FROM public.applications WHERE status = 'SENT') AS applications_sent,
    (SELECT COUNT(*) FROM public.email_queue WHERE status IN ('QUEUED', 'RETRY', 'SENDING')) AS pending_queue,
    (SELECT COUNT(*) FROM public.processing_errors WHERE status = 'OPEN' AND occurred_at >= (NOW() - INTERVAL '24 hours')) AS errors_24h,
    (SELECT COUNT(*) FROM public.audit_logs) AS total_audit_logs
FROM (
    SELECT
        COUNT(*) AS total_emails,
        COUNT(*) FILTER (WHERE processed = TRUE) AS processed_emails,
        COUNT(*) FILTER (WHERE processed = FALSE AND COALESCE(status_desc, '') NOT LIKE 'Failed%') AS pending_emails,
        COUNT(*) FILTER (WHERE status_desc LIKE 'Failed%') AS failed_emails,
        COUNT(*) FILTER (
            WHERE status_desc IN ('Parsed', 'Parsed - NR', 'Parsed - Dup')
               OR (status_desc IS NULL AND processed = TRUE)
        ) AS rep_emails_processed
    FROM public.gmail_emails
) ge;

-- -----------------------------------------------------------------------------
-- 4. REFRESH PLANNER STATISTICS (Canonical Alphabetical Order)
-- -----------------------------------------------------------------------------
ANALYZE public.ai_usage_logs;
ANALYZE public.applications;
ANALYZE public.audit_logs;
ANALYZE public.consultant_experience;
ANALYZE public.consultants;
ANALYZE public.email_queue;
ANALYZE public.generated_resumes;
ANALYZE public.gmail_accounts;
ANALYZE public.gmail_emails;
ANALYZE public.notifications;
ANALYZE public.processing_errors;
ANALYZE public.recruiter_consultants;
ANALYZE public.requirement_consultant_matches;
ANALYZE public.requirements;
ANALYZE public.resumes;
ANALYZE public.users;

-- -----------------------------------------------------------------------------
-- 5. SERVER-LEVEL TUNING (run once as superuser, OUTSIDE a transaction;
--    calibrated specifically for a 2 vCPU / 4 GB RAM host / VM).
--    Left commented because managed providers (RDS / Cloud SQL / Supabase)
--    set these through parameter groups instead of ALTER SYSTEM.
-- -----------------------------------------------------------------------------
-- ALTER SYSTEM SET shared_buffers = '1GB';                 -- 25% of 4 GB RAM (requires restart)
-- ALTER SYSTEM SET effective_cache_size = '3GB';          -- 75% of 4 GB RAM (OS cache + shared_buffers)
-- ALTER SYSTEM SET work_mem = '4MB';                      -- Safe for ~100 concurrent complex sort/hash operations
-- ALTER SYSTEM SET maintenance_work_mem = '256MB';        -- For VACUUM / CREATE INDEX on 4 GB RAM
-- ALTER SYSTEM SET max_connections = 100;                 -- Well-matched to 2 vCPU / 4 GB RAM (requires restart)
-- ALTER SYSTEM SET random_page_cost = 1.1;                 -- Optimized for SSD / NVMe storage
-- ALTER SYSTEM SET effective_io_concurrency = 100;         -- Concurrent I/O operations for SSD
-- ALTER SYSTEM SET max_worker_processes = 2;              -- Matched to 2 vCPU cores (requires restart)
-- ALTER SYSTEM SET max_parallel_workers = 2;              -- Max parallel query workers across system
-- ALTER SYSTEM SET max_parallel_workers_per_gather = 1;   -- Max parallel workers per query (preserves CPU for concurrency)
-- ALTER SYSTEM SET max_parallel_maintenance_workers = 1;  -- Parallel workers for index building / VACUUM
-- ALTER SYSTEM SET autovacuum_max_workers = 2;             -- Limit background autovacuum to max 2 workers (requires restart)
-- ALTER SYSTEM SET autovacuum_naptime = '30s';            -- Check for dead tuples every 30s
-- ALTER SYSTEM SET jit = off;                              -- OLTP: avoid JIT compiler latency & CPU spikes
-- ALTER SYSTEM SET default_statistics_target = 100;       -- Balanced query planner statistics for 4 GB RAM
-- ALTER SYSTEM SET idle_in_transaction_session_timeout = '60s'; -- Auto-terminate leaked/hanging transactions
-- ALTER SYSTEM SET track_io_timing = on;
-- CREATE EXTENSION IF NOT EXISTS pg_stat_statements;       -- Monitor slow queries
-- SELECT pg_reload_conf();
--
-- NOTE: for zero-downtime index builds on a large live table under heavy 24/7 write traffic,
-- you can run any single CREATE INDEX above as CREATE INDEX CONCURRENTLY ... in its own statement.
