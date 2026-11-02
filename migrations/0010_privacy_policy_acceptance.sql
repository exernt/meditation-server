-- Evidence of when, and to which version of the privacy policy, each user agreed
-- at registration. Accounts created before this was tracked have neither value.
ALTER TABLE users
    ADD COLUMN privacy_policy_accepted_at timestamptz,
    ADD COLUMN privacy_policy_version integer;
