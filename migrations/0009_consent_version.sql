ALTER TABLE study_registrations
    ADD COLUMN consent_version integer NOT NULL DEFAULT 1;
-- migrate:split
ALTER TABLE study_registrations
    ALTER COLUMN consent_version DROP DEFAULT;
