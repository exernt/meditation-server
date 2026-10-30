ALTER TABLE study
    ADD COLUMN activated_at timestamptz;

-- Existing active studies predate an explicit activation timestamp. The last
-- registration is the closest durable approximation of the end of recruitment.
-- Studies activated after this migration receive the exact transition time.
-- migrate:split
UPDATE study s
SET activated_at = COALESCE(
    (
        SELECT max(sr.registered_at)
        FROM study_registrations sr
        WHERE sr.study_id = s.id
    ),
    (
        SELECT scb.created_on
        FROM study_created_by scb
        WHERE scb.study_id = s.id
    ),
    now()
)
WHERE s.status = 'active'::study_status
  AND s.activated_at IS NULL;
