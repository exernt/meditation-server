# Production deployment rehearsal on the AWS Free Plan

This is a one-off, synthetic rehearsal of the production deployment flow before
handover. It is not approval to process real participant data and it is not a
substitute for the capacity validation that the client must complete after the
account and quotas permit it.

## Recorded constraints

- The AWS account remains on the Free Plan for this rehearsal. Aurora must use
  PostgreSQL Express Configuration. The account supports at most two eligible
  clusters/two instances, 4 ACUs and 1 GiB per cluster. Confirm the live account
  state and remaining credits before provisioning `wm-prod-aurora-postgres`.
- Keep the production rehearsal synthetic. Do not copy development or real
  participant data into it.
- The regional Lambda concurrency quota remains 10 and the requested increase
  was rejected. Leave `API_RESERVED_CONCURRENCY` unset.
- The 100-active-user/15-minute capacity test is therefore recorded as
  **not manually verifiable before handover**, not as passed. The client owns a
  future quota request and load test after handover.
- SES uses verified email identities in `ap-southeast-2`. A client-owned domain,
  DKIM, SPF/DMARC and SES production access are client responsibilities after
  handover.

## Before dispatching the workflow

1. Record the AWS account ID, remaining Free Plan credits/expiry, current Aurora
   cluster count, release commit, operators and approved rehearsal budget.
2. Confirm `main` CI is green and the release commit is immutable and reviewed.
3. Configure the GitHub `production` environment from the source table in
   [`github-actions-oidc.md`](github-actions-oidc.md). Require a reviewer and do
   not permit administrator bypass where the GitHub plan supports it.
4. Provision the separate production rehearsal cluster:

   ```powershell
   .\scripts\provision-aurora-express.ps1 `
     -Stage prod `
     -Profile wm-admin `
     -AllowProduction
   ```

   The script creates an Aurora PostgreSQL serverless Express cluster that uses
   the internet access gateway and IAM-only master authentication. On the Free
   Plan it must report `BackupRetentionDays: 1`, `DeletionProtection: True`,
   `DataApiEnabled: True`, and the PostgreSQL log export. Use
   `aws rds generate-db-auth-token` for the one-time administrator connection.
5. Deploy `WmData-prod`, initialise the `wm_app` role from the generated secret,
   and apply the ordered migrations. Do not print either credential.
6. Confirm both SNS email subscriptions: budget alerts and operational alerts.
7. Set `WAF_MODE=count` for the first full deployment.

## Two-pass URL configuration

The generated API ID does not exist before the first deployment. For the first
pass, use clearly invalid HTTPS placeholders with the required path suffixes.
After `WmApi-prod` exposes `ApiBaseUrl`, update:

- `PASSWORD_RESET_LINK_BASE` to `ApiBaseUrl + auth/password-reset/open`;
- `EMAIL_VERIFICATION_LINK_BASE` to `ApiBaseUrl + auth/email-verification/open`;
- `RESPONSE_EXPORT_LINK_BASE` to `ApiBaseUrl + response-exports`.

Run the production workflow again and verify that the post-deployment smoke test
uses the dedicated synthetic account.

## WAF rehearsal

The production web ACL starts in `count` mode and:

- evaluates the AWS Common, Known Bad Inputs and IP Reputation managed groups;
- leaves the managed 8 KiB `SizeRestrictions_BODY` rule in count mode and uses
  an explicit 64 KiB application boundary instead;
- rate-counts only abuse-sensitive authentication endpoints at 100 requests per
  source IP per five minutes;
- rate-counts the whole API at 3,000 requests per source IP per five minutes;
- stores only `COUNT` and `BLOCK` traffic records for 30 days, with query strings,
  authorization and cookie headers redacted; and
- alarms when at least 25 count/block matches occur in five minutes.

Exercise every legitimate synthetic flow, including the largest expected
questionnaire and generation payloads. Review the `aws-waf-logs-wm-prod-api`
log group for false positives and confirm alert delivery. Do not use a shared
team/NAT address to deliberately trigger the rate rules.

After tuning and recording any exclusions, change `WAF_MODE` to `block`, review
the CDK diff, redeploy, rerun the smoke suite, and perform one controlled benign
WAF probe. Record the ACL association, rule actions, alarm state, and log group.

## Backup and recovery evidence

Deletion protection prevents accidental cluster deletion, but it is not a
backup. Before handover, record:

- `BackupRetentionPeriod = 1` on the Free Plan and `DeletionProtection = true` from
  `describe-db-clusters`;
- latest and earliest restorable timestamps;
- one manual pre-handover snapshot identifier; and
- a point-in-time restore plus application/database smoke test, if the account's
  two-cluster limit and remaining credits allow the temporary restored cluster.

If a restore cannot be run without exceeding the two-cluster limit, record the
same explicit exception as the load test and make it a client-owned post-handover
action. Never disable deletion protection merely to make room for a test.

## Acceptance record

The rehearsal can be marked complete when deployment, migrations, health,
database, authentication, refresh rotation, deterministic generation, optional
Polly, WAF count/block, alarms, logs and rollback documentation pass with
synthetic data. The acceptance record must still state:

- capacity/load test: not verified because the quota increase was rejected;
- SES domain/reputation controls: deferred to the client after handover; and
- restore test: passed, or explicitly deferred because the Free Plan cluster
  limit made a safe temporary restore impossible.

