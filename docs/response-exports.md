# Response exports

The response-export flow leaves the existing per-questionnaire JSON endpoint in
place while the mobile client migrates to CSV delivery.

## Request endpoints

- `POST /response-exports` requests one CSV containing responses from every
  study owned by the verified researcher.
- `POST /studies/{study_id}/response-export` requests one CSV containing every
  questionnaire and round for that owned study.
- `GET /response-exports/{export_id}` returns the authenticated researcher's job
  status.

Creation returns HTTP 202. A completed export with the same scope, response
count, and latest submission timestamp is reused while it remains within its
seven-day retention window. A queued or running matching job is also reused.

## Pseudonyms

Joining a study creates a random `P-`-prefixed 128-bit respondent code that is
unique within that study. Each questionnaire response stores a copy of the
code. CSV exports contain `respondent_code` and never contain `respondent_id`.
The same account receives a different code in each study.

Migration `0004_response_exports.sql` backfills codes for registrations and for
responses that still have a respondent UUID. A previously deleted participant
whose response already has a `NULL` respondent UUID cannot be retrospectively
linked across rounds.

## CSV contract

The UTF-8 CSV uses one row per answer. Multiple-choice selections use one row
per selection and set `selection_index`.

Columns are:

`study_id`, `study_name`, `respondent_code`, `response_id`,
`questionnaire_id`, `questionnaire_title`, `questionnaire_round`,
`questionnaire_issuance_id`, `questionnaire_issued_at`,
`response_submitted_at`, `question_id`, `question_prompt`, `question_type`,
`answer_value`, `answer_detail`, and `selection_index`.

Text beginning with `=`, `+`, `-`, or `@` is prefixed with an apostrophe to
prevent spreadsheet formula execution.

## Delivery

The API commits the export job and its outbox record before publishing the SQS
message. A scheduled maintenance Lambda retries unpublished outbox rows. The
idempotent export worker claims a database lease, reads the request-time response
snapshot in bounded pages, uploads the CSV to the private S3 bucket, and sends
the SES notification. Completion retains the lease until notification is
recorded, so an interrupted worker can resume email delivery without regenerating
the CSV.

The email links to `GET /response-exports/{export_id}/download`, which renders a
plain email/password form. Posting valid credentials checks verified-researcher
status, export ownership, completion, and expiry. Success returns HTTP 303 to a
fresh five-minute S3 presigned URL; failure renders red `Login failed.` text.
The download login does not issue tokens, increment the user's session version,
or revoke the mobile session.

S3 Block Public Access, bucket-owner-enforced access, TLS-only access, SSE-S3,
least-privilege object-prefix permissions, seven-day lifecycle expiration, and
CloudTrail data events protect and audit export access. Application expiry is
checked separately because lifecycle deletion is asynchronous.
