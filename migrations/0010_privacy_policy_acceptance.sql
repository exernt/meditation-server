ALTER TABLE users
    ADD COLUMN privacy_policy_accepted_at timestamptz,
    ADD COLUMN privacy_policy_version integer;
