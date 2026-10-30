ALTER TABLE users ADD COLUMN pending_researcher_institution text;
-- migrate:split
ALTER TABLE users ADD CONSTRAINT ck_users_pending_researcher_institution
    CHECK (
        pending_researcher_institution IS NULL
        OR char_length(btrim(pending_researcher_institution)) BETWEEN 1 AND 255
    );
-- migrate:split
UPDATE users AS account
SET pending_researcher_institution = profile.institution
FROM researcher_profiles AS profile
WHERE account.id = profile.user_id
  AND account.email_verified = false;
-- migrate:split
DELETE FROM researcher_profiles AS profile
USING users AS account
WHERE account.id = profile.user_id
  AND account.email_verified = false;
