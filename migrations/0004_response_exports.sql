ALTER TABLE study_registrations ADD COLUMN respondent_code text;
-- migrate:split
UPDATE study_registrations
SET respondent_code = 'P-' || upper(replace(gen_random_uuid()::text, '-', ''));
-- migrate:split
ALTER TABLE study_registrations ALTER COLUMN respondent_code SET NOT NULL;
-- migrate:split
ALTER TABLE study_registrations ADD CONSTRAINT uq_study_registrations_respondent_code
    UNIQUE (study_id, respondent_code);
-- migrate:split
ALTER TABLE study_registrations ADD CONSTRAINT ck_study_registrations_respondent_code
    CHECK (respondent_code ~ '^P-[0-9A-F]{32}$');
-- migrate:split
ALTER TABLE questionnaire_responses ADD COLUMN respondent_code text;
-- migrate:split
UPDATE questionnaire_responses AS response
SET respondent_code = registration.respondent_code
FROM study_questionnaires AS issuance,
     study_registrations AS registration
WHERE response.study_questionnaire_id = issuance.id
  AND registration.study_id = issuance.study_id
  AND registration.user_id = response.respondent_id;
-- migrate:split
ALTER TABLE questionnaire_responses ADD CONSTRAINT ck_questionnaire_responses_respondent_code
    CHECK (
        respondent_code IS NULL
        OR respondent_code ~ '^P-[0-9A-F]{32}$'
    );
-- migrate:split
ALTER TABLE questionnaire_responses ADD CONSTRAINT ck_questionnaire_responses_identified_code
    CHECK (respondent_id IS NULL OR respondent_code IS NOT NULL);
-- migrate:split
CREATE TYPE response_export_scope AS ENUM ('all_studies', 'study');
-- migrate:split
CREATE TYPE response_export_status AS ENUM ('queued', 'running', 'complete', 'failed');
-- migrate:split
CREATE TABLE response_exports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    researcher_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope response_export_scope NOT NULL,
    study_id uuid REFERENCES study(id) ON DELETE CASCADE,
    status response_export_status NOT NULL DEFAULT 'queued',
    source_response_count bigint NOT NULL,
    source_last_submitted_on timestamptz,
    attempt_count integer NOT NULL DEFAULT 0,
    lease_token text,
    lease_expires_at timestamptz,
    s3_key text,
    filename text,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    generated_at timestamptz,
    expires_at timestamptz,
    notification_sent_at timestamptz,
    CONSTRAINT ck_response_exports_scope_study CHECK (
        (scope = 'study' AND study_id IS NOT NULL)
        OR (scope = 'all_studies' AND study_id IS NULL)
    ),
    CONSTRAINT ck_response_exports_source_count CHECK (source_response_count >= 0),
    CONSTRAINT ck_response_exports_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT ck_response_exports_lease_pair CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_response_exports_complete_object CHECK (
        (status = 'complete') = (
            s3_key IS NOT NULL
            AND filename IS NOT NULL
            AND generated_at IS NOT NULL
            AND expires_at IS NOT NULL
        )
    )
);
-- migrate:split
CREATE INDEX ix_response_exports_researcher_created_at
    ON response_exports(researcher_id, created_at DESC);
-- migrate:split
CREATE INDEX ix_response_exports_cache_lookup
    ON response_exports(
        researcher_id,
        scope,
        study_id,
        source_response_count,
        source_last_submitted_on,
        expires_at
    )
    WHERE status = 'complete';
-- migrate:split
CREATE INDEX ix_response_exports_active_lease
    ON response_exports(status, lease_expires_at)
    WHERE status IN ('queued', 'running');
-- migrate:split
CREATE TABLE response_export_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    export_id uuid NOT NULL REFERENCES response_exports(id) ON DELETE CASCADE,
    attempt_count integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_response_export_outbox_export UNIQUE (export_id),
    CONSTRAINT ck_response_export_outbox_attempt_count CHECK (attempt_count >= 0)
);
-- migrate:split
CREATE INDEX ix_response_export_outbox_pending
    ON response_export_outbox(next_attempt_at, created_at)
    WHERE published_at IS NULL;
