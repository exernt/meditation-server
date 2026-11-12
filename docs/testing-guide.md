# Walking Meditation server testing guide

Last reviewed against this repository: 12 November 2026

This file is the authoritative list of test commands and verification flows for
`meditation-server`. Tests must use synthetic accounts, studies, routes,
questionnaires, feedback and narration. Never copy production data into dev or
local environments, and never put credentials, tokens, precise participant
locations, questionnaire answers, prompts, or generated narration in logs or
test output.

The former project testing AWS account is not the client's deployment or test
environment and will become defunct. Local/unit results remain valid repository
evidence, but every live AWS, provider, smoke, restore, WAF and capacity check
must be rerun against resources newly created in the client's AWS account.

## 1. Local setup

The supported local toolchain matches CI: Python 3.13 and Node 22.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --requirement requirements.txt
npm ci
```

Keep the virtual environment active for CDK commands because `cdk.json` invokes
`python infra/app.py`.

## 2. Standard test commands

Run everything required by pull-request CI:

```powershell
python -m pytest infra/tests tests -q
```

Run application tests only:

```powershell
python -m pytest tests -q
```

Run infrastructure/configuration/workflow tests only:

```powershell
python -m pytest infra/tests -q
```

Stop on the first failure while diagnosing:

```powershell
python -m pytest infra/tests tests -q -x
```

Run one file or one test:

```powershell
python -m pytest tests/test_generation_jobs.py -q
python -m pytest tests/test_generation_jobs.py::test_name_here -q
```

Use `-s` only when temporary diagnostic output is genuinely needed; remove any
diagnostic print before commit. The default test suite is mocked/unit-level and
must not call live AWS, Google, OpenAI, SES, or Polly.

## 3. Focused suites by change area

| Change area | Command/files |
| --- | --- |
| Authentication, transactions, login and profile | `python -m pytest tests/test_auth_transactions.py tests/test_api_contract.py tests/test_profile.py -q` |
| Privacy acceptance and study consent | `python -m pytest tests/test_privacy_policy_acceptance.py tests/test_consent_form.py tests/test_study_consent.py -q` |
| Researcher registration and administration | `python -m pytest tests/test_researcher_registration_verification.py tests/test_researcher_admin.py -q` |
| Studies and lifecycle boundaries | `python -m pytest tests/test_study_lifecycle_boundaries.py tests/test_study_consent.py -q` |
| Questionnaires and feedback | `python -m pytest tests/test_questionnaires.py tests/test_feedback.py -q` |
| Generation API/outbox/worker | `python -m pytest tests/test_generation_jobs.py tests/test_generation_processor.py tests/test_script_archive.py -q` |
| Google route/context integrations | `python -m pytest tests/test_maps.py tests/test_google_context.py -q` |
| TTS and voice authorization | `python -m pytest tests/test_speech.py tests/test_tts_voice_authorization.py -q` |
| Response exports | `python -m pytest tests/test_response_exports.py -q` |
| Account and export maintenance | `python -m pytest tests/test_account_maintenance.py tests/test_response_exports.py -q` |
| Email adapters | `python -m pytest tests/test_email.py -q` |
| Database executors and migration runner | `python -m pytest tests/test_asyncpg_executor.py tests/test_data_api_executor.py tests/test_migration_runner.py -q` |
| Settings and environment validation | `python -m pytest tests/test_settings.py infra/tests/test_config.py -q` |
| Aurora provisioning script | `python -m pytest tests/test_aurora_provisioning.py -q` |
| Post-deployment smoke client | `python -m pytest tests/test_post_deployment_smoke.py -q` |
| CDK stacks, policies, alarms and retention | `python -m pytest infra/tests -q` |
| Deployment workflow contract | `python -m pytest infra/tests/test_deployment_workflows.py infra/tests/test_run_cdk_ci.py -q` |

When API behavior changes, also run `tests/test_api_contract.py` and update
`api-reference.md` in the same commit. When a migration changes, run migration,
repository, API/domain, and infrastructure tests relevant to the affected data.

## 4. CI-equivalent CDK synthesis

Pull-request CI runs the full test suite and strict synthesis for both dev and
prod using synthetic ARNs and addresses. To reproduce the exact command, use
the context in `.github/workflows/ci.yml`. The current dev form is:

```powershell
npx cdk synth --strict `
  -c stage=dev `
  -c databaseClusterArn=arn:aws:rds:ap-southeast-2:111111111111:cluster:wm-dev-aurora-postgres `
  -c workerMaxConcurrency=2 `
  -c monthlyBudgetUsd=20 `
  -c budgetAlertEmail=ci@example.invalid `
  -c operationalAlertEmail=ci@example.invalid `
  -c googleMapsSecretArn=arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:ci-google-AbC123 `
  -c openAiSecretArn=arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:ci-openai-XyZ789 `
  -c passwordResetFromEmail=ci@example.invalid `
  -c passwordResetLinkBase=https://api.example.invalid/auth/password-reset/open `
  -c emailVerificationLinkBase=https://api.example.invalid/auth/email-verification/open `
  -c responseExportLinkBase=https://api.example.invalid/response-exports `
  -c wafMode=count
```

For prod, use `--output cdk.out.prod`, `-c stage=prod`, the prod cluster ARN, and
URL paths that include `/prod/` exactly as CI does. Synthesis runs cdk-nag; a
new warning is a test failure unless the underlying design is corrected or a
specific reviewed suppression explains why the control does not apply.

Do not deploy from CI fixtures. For a real environment, use the protected
workflow or the validated variable-based commands in `aws-deployment-guide.md`.

## 5. Migration tests and local PostgreSQL

Migration unit tests validate ordering, splitting, checksums, locking, and
transaction behavior without a live database:

```powershell
python -m pytest tests/test_migration_runner.py -q
```

For a disposable local PostgreSQL instance, use the same migration files via
`asyncpg`:

```powershell
$env:DATABASE_URL = 'postgresql://wm_app:password@127.0.0.1:5432/walking_meditation'
python scripts/apply_migrations.py
Remove-Item Env:DATABASE_URL
```

For a clean-install test, use a new disposable database and confirm every
migration through `0010_privacy_policy_acceptance.sql` is listed. Run the command
again and confirm `Database schema is already current.` For an upgrade test,
start from a copy of the immediately preceding synthetic schema, apply the new
migration, and validate affected reads/writes. Never point local migration tests
at production.

A checksum mismatch is expected to fail. Do not update
`wm_schema_migrations` to make it pass; restore the original applied file and
create a new numbered migration.

## 6. Post-deployment smoke suite

The smoke client checks:

1. `GET /health`;
2. `GET /health/database`;
3. login with a dedicated verified synthetic account;
4. `GET /auth/me` identity;
5. deterministic no-park script generation;
6. refresh-token rotation and authenticated use of the new access token;
7. optional non-empty Polly MP3 output; and
8. logout cleanup.

Set environment variables without echoing the password:

```powershell
$env:SMOKE_BASE_URL = 'https://<api-id>.execute-api.ap-southeast-2.amazonaws.com/<stage>/'
$env:SMOKE_TEST_EMAIL = '<SYNTHETIC_VERIFIED_ACCOUNT>'
$env:SMOKE_TEST_PASSWORD = '<PASSWORD>'
# Optional; must match the account's active study group voice.
$env:SMOKE_TEST_TTS_VOICE = 'male'
python scripts/post_deployment_smoke.py
Remove-Item Env:SMOKE_BASE_URL, Env:SMOKE_TEST_EMAIL, Env:SMOKE_TEST_PASSWORD
Remove-Item Env:SMOKE_TEST_TTS_VOICE -ErrorAction SilentlyContinue
```

`SMOKE_BASE_URL` must be absolute HTTPS. The client retries readiness for
transient 502/503/504 responses and uses a 35-second request timeout by default;
override only with a positive `SMOKE_TIMEOUT_SECONDS`. It never registers or
deletes the smoke account. A successful login replaces that account's previous
mobile session, so it must not be a person's account.

GitHub Actions supplies the email/password as protected environment secrets and
resolves the base URL from CloudFormation outputs. A smoke failure fails the
deployment job; investigate before re-running.

## 7. Manual API acceptance flow

Run this flow in dev after each substantial API or schema change and in prod
before launch. Use the endpoint contract in `api-reference.md`.

### Authentication and privacy

1. Register a new synthetic participant with current policy version and confirm
   201 without a session.
2. Confirm false agreement returns 400 and stale policy version returns 409.
3. Verify the email link, then login; confirm unverified login was previously
   blocked with 403.
4. Cause repeated invalid login attempts; verify generic errors, 429 and
   `Retry-After` without exposing account existence.
5. Rotate a refresh token; replay the old token and confirm the family is
   revoked.
6. Test logout, logout-all, password-reset request/confirm, expired/used tokens,
   and disabled-account behavior.

### Researcher, study and consent

1. Submit a researcher request, verify it appears only after email verification,
   and approve/reject through an admin account.
2. Create a draft study and confirm placeholder consent prevents publication.
3. Edit consent; confirm a changed save increments version, identical save does
   not, non-owner is denied, and post-publication editing returns 409.
4. Read consent as a participant, join with that version, and confirm a stale
   version returns 409. Leave and rejoin under the allowed lifecycle.
5. Exercise publish → activate → close transitions and reject invalid backward
   transitions.
6. Edit both group generation parameters and voices; confirm participants
   receive only their active group's values.

### Questionnaires, feedback and deletion

1. Validate all question types, required/optional answers, invalid choices,
   duplicate question IDs, and one-response-per-round behavior.
2. Issue only to an active owned study; verify a draft questionnaire becomes
   published, round increments, and revoked/closed rounds are hidden from
   participants.
3. Submit general, study and technical feedback; confirm study feedback requires
   active participation and list access is role-gated.
4. Delete a synthetic participant and confirm sessions disappear while retained
   questionnaire responses have a null direct respondent reference.

## 8. Route and generation acceptance

Use synthetic Melbourne coordinates with no connection to a participant.

1. Test a baseline route and park candidates; validate duration, distance,
   polylines and step indexing.
2. Submit synchronous park and no-park generation. Confirm the legacy response
   shape and explicit `script.fallback` flag.
3. Submit an asynchronous job with a UUID idempotency key. Confirm same key/body
   returns the same job, changed body returns 409, status/result ownership hides
   another user's job, and incomplete result returns 202 with `Retry-After: 2`.
4. Exercise the two-active and rolling-24-hour quota responses.
5. Disable or fault Google/OpenAI in dev and confirm deterministic fallback,
   stable error handling, no sensitive provider exception, and no payloads in
   logs.
6. Interrupt publish/worker processing to validate outbox recovery, conditional
   lease claims, duplicate delivery, fifth-failure terminal state, DLQ alarm,
   controlled redrive and terminal retention cleanup.
7. Confirm dev archives final/raw output for seven days and production does not
   create a generation archive bucket.

## 9. Response-export acceptance

1. Create synthetic questionnaire responses across multiple studies and rounds.
2. Request a single-study and all-owned-studies export; confirm 202, ownership,
   status polling, queued/running reuse, and unchanged-complete reuse.
3. Download through the emailed browser form. Test wrong email/password,
   unverified/non-owner/expired access, throttle, and the successful 303 to a
   new five-minute URL.
4. Inspect the CSV: UTF-8, exact columns, one row per answer/selection,
   study-scoped respondent codes, no participant UUID/email, and apostrophe
   protection for `=`, `+`, `-`, and `@` cell prefixes.
5. Interrupt generation after upload and before email; confirm worker retry
   resumes notification without rebuilding the file.
6. Confirm CloudTrail S3 data events and seven-day application/object expiry.

## 10. TTS tests and provider changes

Current server settings are Polly in `ap-southeast-2`, Australian English,
standard engine, Russell for male and Nicole for female. `(pause)` becomes a
2.5-second SSML break. Unit tests must cover disabled mode, voice mapping, XML
escaping, pause-only input, the 6,000-character SSML limit, timeouts/retries,
empty audio, 502/503 mapping, no-store response, and active-study voice
authorization.

To test a different Polly voice or engine:

1. Confirm voice/engine/language support in Sydney.
2. Change the fixed values in `infra/stacks/api_stack.py` and update the exact
   provider-request assertion in `tests/test_speech.py`.
3. Run speech, authorization, API, config and API-stack tests plus strict synth.
4. Review the CDK diff and deploy only to dev through the protected workflow.
5. Clear the Android TTS cache/current walk because a server-only voice change
   at the same URL cannot invalidate an existing client cache key.
6. Compare the same short/long scripts for pronunciation, pacing, latency,
   failure behavior and cost. Test >3,000-character input and disabled mode.

For a different provider, preserve the public `POST /tts` contract. Add a
bounded adapter, explicit provider selection that fails closed, exact Secrets
Manager/IAM access, adapter tests, privacy/region/retention approval, and remove
Polly permission when it is not selected. Verify only narration text leaves the
application and logs contain neither narration nor credentials.

Live Polly output and Android playback remain required because mocked tests
cannot establish audible quality, chunk sequencing, device cache behavior, or
provider availability.

## 11. Infrastructure and failure exercises

Before production launch, record these dev/rehearsal exercises:

- API Lambda error/throttle/duration alarms and API Gateway 5xx alarms;
- generation/export source backlog, worker error, maintenance error and every
  DLQ alarm;
- one controlled DLQ redrive after fixing the consumer;
- SES sandbox/production behavior, suppression, bounce/complaint and send
  failure without user enumeration;
- Google/OpenAI secret denial, quota and provider failure;
- JWT overlapping-key rotation, rate-limit pepper rotation, and coordinated
  database password/secret rotation;
- WAF count-mode legitimate large payloads, false-positive tuning, block-mode
  smoke, and a controlled rate-rule match;
- scheduled retention jobs and Australia/Sydney schedule behavior across DST;
- budget and SNS alert delivery to maintained mailboxes; and
- CloudTrail logging, digest validation, and export-bucket data events.

Do not close an exercise based only on an alarm entering `OK`; record the root
cause, evidence, remediation, affected jobs/data, and final state.

## 12. Backup and restore test

At least monthly in production and before client production acceptance:

1. Select an approved recovery point and restore Aurora to a new isolated
   identifier.
2. Record start time, source recovery time, target identifier and configuration.
3. Validate the migration table/checksums and representative schema constraints.
4. Point an isolated synthetic application configuration at the restored data
   and run health/database/auth smoke tests without sending real email/provider
   requests.
5. Measure RPO and RTO against the approved objectives.
6. Remove temporary access, then delete the restore only after the evidence and
   required snapshot decision are approved.

Deletion protection and a backup-retention value are not substitutes for this
test.

## 13. Production acceptance record

Attach to the release/change record:

- release commit and clean-worktree confirmation;
- full pytest result and strict dev/prod synthesis result;
- reviewed CDK diff and cdk-nag outcome;
- migration list and clean-upgrade evidence;
- post-deployment smoke result;
- manual auth/privacy/consent, generation, export and TTS results;
- WAF count/block evidence;
- alarms/queues/DLQs/log review;
- restore test and measured RPO/RTO;
- capacity result or explicitly accepted residual risk; and
- approver, completion time and rollback/forward-fix plan.

The currently documented 100-active-user/15-minute capacity target remains
unverified until the required regional quotas are available. It must be tested
or formally accepted as residual risk; it must never be marked passed based on
unit tests or a synthetic smoke run.

