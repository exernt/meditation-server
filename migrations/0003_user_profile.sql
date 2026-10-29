UPDATE users
SET age = NULL
WHERE age IS NOT NULL AND (age < 18 OR age > 120);
-- migrate:split
ALTER TABLE users DROP CONSTRAINT ck_users_age_range;
-- migrate:split
ALTER TABLE users ADD CONSTRAINT ck_users_age_range
    CHECK (age IS NULL OR (age >= 18 AND age <= 120));
