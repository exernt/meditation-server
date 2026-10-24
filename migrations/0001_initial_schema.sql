CREATE TYPE user_role AS ENUM ('participant', 'researcher', 'admin');
-- migrate:split
CREATE TYPE study_status AS ENUM ('draft', 'recruiting', 'active', 'closed');
-- migrate:split
CREATE TYPE study_group AS ENUM ('control', 'experimental');
-- migrate:split
CREATE TYPE feedback_category AS ENUM ('general', 'study', 'technical');
-- migrate:split
CREATE TYPE questionnaire_status AS ENUM ('draft', 'published', 'archived');
-- migrate:split
CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL,
    password_hash text NOT NULL,
    session_version integer NOT NULL DEFAULT 0,
    gender text,
    age integer,
    role user_role NOT NULL DEFAULT 'participant',
    email_verified boolean NOT NULL DEFAULT false,
    disabled_at timestamptz,
    created_on timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_email UNIQUE (email),
    CONSTRAINT ck_users_age_range CHECK (age IS NULL OR (age >= 0 AND age <= 150))
);
-- migrate:split
CREATE TABLE researcher_profiles (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    institution text,
    verified_at timestamptz,
    created_on timestamptz NOT NULL DEFAULT now()
);
-- migrate:split
CREATE TABLE password_reset_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_on timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_password_reset_tokens_token_hash UNIQUE (token_hash)
);
-- migrate:split
CREATE INDEX ix_password_reset_tokens_user_id ON password_reset_tokens(user_id);
-- migrate:split
CREATE TABLE email_verification_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_on timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_email_verification_tokens_token_hash UNIQUE (token_hash)
);
-- migrate:split
CREATE INDEX ix_email_verification_tokens_user_id ON email_verification_tokens(user_id);
-- migrate:split
CREATE TABLE refresh_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id uuid NOT NULL,
    parent_id uuid REFERENCES refresh_tokens(id) ON DELETE SET NULL,
    token_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_on timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_refresh_tokens_token_hash UNIQUE (token_hash),
    CONSTRAINT uq_refresh_tokens_parent_id UNIQUE (parent_id)
);
-- migrate:split
CREATE INDEX ix_refresh_tokens_user_id ON refresh_tokens(user_id);
-- migrate:split
CREATE INDEX ix_refresh_tokens_family_id ON refresh_tokens(family_id);
-- migrate:split
CREATE TABLE login_throttles (
    subject_hash text PRIMARY KEY,
    failed_attempts integer NOT NULL DEFAULT 0,
    blocked_until timestamptz,
    updated_on timestamptz NOT NULL DEFAULT now()
);
-- migrate:split
CREATE TABLE security_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    event_type text NOT NULL,
    subject_hash text,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
-- migrate:split
CREATE INDEX ix_security_events_user_id ON security_events(user_id);
-- migrate:split
CREATE INDEX ix_security_events_event_type ON security_events(event_type);
-- migrate:split
CREATE INDEX ix_security_events_occurred_at ON security_events(occurred_at);
-- migrate:split
CREATE TABLE study (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    duration integer NOT NULL,
    description text NOT NULL DEFAULT '',
    script_generation_parameters text NOT NULL DEFAULT '',
    status study_status NOT NULL DEFAULT 'draft',
    CONSTRAINT ck_study_duration_positive CHECK (duration > 0)
);
-- migrate:split
CREATE TABLE study_created_by (
    researcher_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    study_id uuid PRIMARY KEY REFERENCES study(id) ON DELETE CASCADE,
    created_on timestamptz NOT NULL DEFAULT now()
);
-- migrate:split
CREATE INDEX ix_study_created_by_researcher_id ON study_created_by(researcher_id);
-- migrate:split
CREATE TABLE consent_form (
    study_id uuid PRIMARY KEY REFERENCES study(id) ON DELETE CASCADE,
    body_text text NOT NULL,
    version integer NOT NULL DEFAULT 1
);
-- migrate:split
CREATE TABLE study_registrations (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    study_id uuid NOT NULL REFERENCES study(id) ON DELETE CASCADE,
    registered_at timestamptz NOT NULL DEFAULT now(),
    consented_at timestamptz NOT NULL,
    withdrawn_at timestamptz,
    study_group study_group NOT NULL,
    PRIMARY KEY (user_id, study_id)
);
-- migrate:split
CREATE INDEX ix_study_registrations_study_id ON study_registrations(study_id);
-- migrate:split
CREATE TABLE feedback (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    study_id uuid REFERENCES study(id) ON DELETE CASCADE,
    category feedback_category NOT NULL,
    rating integer,
    comment text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_feedback_rating_range CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
    CONSTRAINT ck_feedback_not_empty CHECK (rating IS NOT NULL OR comment IS NOT NULL)
);
-- migrate:split
CREATE INDEX ix_feedback_user_id ON feedback(user_id);
-- migrate:split
CREATE INDEX ix_feedback_study_id ON feedback(study_id);
-- migrate:split
CREATE TABLE questionnaires (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    researcher_id uuid REFERENCES users(id) ON DELETE SET NULL,
    status questionnaire_status NOT NULL DEFAULT 'draft',
    definition jsonb NOT NULL,
    created_on timestamptz NOT NULL DEFAULT now()
);
-- migrate:split
CREATE INDEX ix_questionnaires_researcher_id ON questionnaires(researcher_id);
-- migrate:split
CREATE TABLE study_questionnaires (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    study_id uuid NOT NULL REFERENCES study(id) ON DELETE CASCADE,
    questionnaire_id uuid NOT NULL REFERENCES questionnaires(id) ON DELETE CASCADE,
    round_number integer NOT NULL CHECK (round_number > 0),
    issued_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    CONSTRAINT uq_study_questionnaire_round UNIQUE (study_id, questionnaire_id, round_number)
);
-- migrate:split
CREATE INDEX ix_study_questionnaires_study_id ON study_questionnaires(study_id);
-- migrate:split
CREATE INDEX ix_study_questionnaires_questionnaire_id ON study_questionnaires(questionnaire_id);
-- migrate:split
CREATE TABLE questionnaire_responses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    study_questionnaire_id uuid NOT NULL REFERENCES study_questionnaires(id) ON DELETE CASCADE,
    respondent_id uuid REFERENCES users(id) ON DELETE SET NULL,
    answers jsonb NOT NULL,
    submitted_on timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_questionnaire_response_respondent_round
        UNIQUE (study_questionnaire_id, respondent_id)
);
-- migrate:split
CREATE INDEX ix_questionnaire_responses_study_questionnaire_id
    ON questionnaire_responses(study_questionnaire_id);
-- migrate:split
CREATE INDEX ix_questionnaire_responses_respondent_id
    ON questionnaire_responses(respondent_id);
