-- Record which version of the study consent form each participant agreed to,
-- so consent stays attributable to the exact wording after the form is edited.
-- Every existing registration agreed to the original placeholder text, which is
-- version 1, so 1 is the correct backfill rather than a guess.
ALTER TABLE study_registrations
    ADD COLUMN consent_version integer NOT NULL DEFAULT 1;
-- migrate:split
-- New registrations must state the version explicitly.
ALTER TABLE study_registrations
    ALTER COLUMN consent_version DROP DEFAULT;
