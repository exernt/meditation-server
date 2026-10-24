CREATE TYPE generation_job_kind AS ENUM ('park', 'without_park');
-- migrate:split
CREATE TYPE generation_job_status AS ENUM ('queued', 'running', 'complete', 'failed');
-- migrate:split
CREATE TYPE generation_audio_status AS ENUM ('not_requested', 'disabled');
-- migrate:split
CREATE TABLE generation_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    kind generation_job_kind NOT NULL,
    request_payload jsonb NOT NULL,
    status generation_job_status NOT NULL DEFAULT 'queued',
    audio_status generation_audio_status NOT NULL DEFAULT 'not_requested',
    attempt_count integer NOT NULL DEFAULT 0,
    lease_token text,
    lease_expires_at timestamptz,
    result jsonb,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT uq_generation_jobs_user_idempotency UNIQUE (user_id, idempotency_key),
    CONSTRAINT ck_generation_jobs_idempotency_length CHECK (char_length(idempotency_key) BETWEEN 16 AND 128),
    CONSTRAINT ck_generation_jobs_request_hash CHECK (char_length(request_hash) = 64),
    CONSTRAINT ck_generation_jobs_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT ck_generation_jobs_lease_pair CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
    CONSTRAINT ck_generation_jobs_complete_result CHECK ((status = 'complete') = (result IS NOT NULL)),
    CONSTRAINT ck_generation_jobs_terminal_time CHECK ((status IN ('complete', 'failed')) = (completed_at IS NOT NULL))
);
-- migrate:split
CREATE INDEX ix_generation_jobs_user_created_at
    ON generation_jobs(user_id, created_at DESC);
-- migrate:split
CREATE INDEX ix_generation_jobs_active_lease
    ON generation_jobs(status, lease_expires_at)
    WHERE status IN ('queued', 'running');
-- migrate:split
CREATE INDEX ix_generation_jobs_terminal_cleanup
    ON generation_jobs(completed_at)
    WHERE status IN ('complete', 'failed');
-- migrate:split
CREATE TABLE generation_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES generation_jobs(id) ON DELETE CASCADE,
    event_type text NOT NULL DEFAULT 'generation.requested',
    attempt_count integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_generation_outbox_job UNIQUE (job_id),
    CONSTRAINT ck_generation_outbox_event_type CHECK (event_type = 'generation.requested'),
    CONSTRAINT ck_generation_outbox_attempt_count CHECK (attempt_count >= 0)
);
-- migrate:split
CREATE INDEX ix_generation_outbox_pending
    ON generation_outbox(next_attempt_at, created_at)
    WHERE published_at IS NULL;
-- migrate:split
CREATE INDEX ix_generation_outbox_published_cleanup
    ON generation_outbox(published_at)
    WHERE published_at IS NOT NULL;
