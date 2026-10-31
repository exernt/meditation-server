# GitHub Actions deployment with AWS OIDC

This procedure creates short-lived deployment sessions. Do not create a GitHub
IAM user or store AWS access keys in GitHub.

## 1. Confirm the administrator session

Use AWS CLI v2.33.0 or later because Aurora Express Configuration is required
for the Free Plan.

```powershell
aws login --profile wm-admin --region ap-southeast-2
$wmIdentity = aws sts get-caller-identity --profile wm-admin | ConvertFrom-Json
$wmIdentity
```

Stop if the ARN ends in `:root` or the account ID is not the intended account.

## 2. Publish the current permissions boundary

The repository boundary now includes the separate `rds-data:*` service actions
needed by the runtime roles. `rds:*` does not imply `rds-data:*`.

For an existing `WM-CdkBoundary`, publish a new default policy version:

```powershell
$boundaryArn = "arn:aws:iam::$($wmIdentity.Account):policy/WM-CdkBoundary"
aws iam create-policy-version `
  --policy-arn $boundaryArn `
  --policy-document file://bootstrap/WM-CdkBoundary.json `
  --set-as-default `
  --profile wm-admin
```

IAM managed policies can retain only five versions. If this fails at that
limit, list the versions and delete one old **non-default** version, then retry.
For a new account, create the policy instead:

```powershell
aws iam create-policy `
  --policy-name WM-CdkBoundary `
  --policy-document file://bootstrap/WM-CdkBoundary.json `
  --profile wm-admin
```

Do not manually add the boundary to the other `cdk-*` bootstrap roles. CDK's
`--custom-permissions-boundary` option intentionally applies it to the
CloudFormation execution role; the other bootstrap roles are narrowly scoped
asset/deployment plumbing. The application applies the same boundary to every
IAM role it creates.

## 3. Confirm or create the CDK bootstrap

If `CDKToolkit` already exists and is `CREATE_COMPLETE`, retain it. Updating the
managed boundary policy does not require rebootstrap because the execution role
already references the policy ARN.

For a first bootstrap only:

```powershell
npm ci
npx cdk bootstrap "aws://$($wmIdentity.Account)/ap-southeast-2" `
  --profile wm-admin `
  --termination-protection `
  --custom-permissions-boundary WM-CdkBoundary
```

Confirm in CloudFormation that `CDKToolkit` is `CREATE_COMPLETE` with
termination protection enabled. In IAM, confirm the
`cdk-hnb659fds-cfn-exec-role-*` role has `WM-CdkBoundary` attached as its
permissions boundary.

## 4. Deploy the GitHub OIDC identity stack

The entry point now creates only `WmIdentity` when `stage` is omitted, so no
placeholder database, secret, budget, or email context is necessary:

```powershell
.\.venv\Scripts\Activate.ps1
npx cdk diff WmIdentity --profile wm-admin
npx cdk deploy WmIdentity --profile wm-admin --require-approval broadening
```

This creates one GitHub OIDC provider and two roles:

- `wm-dev-github-deployer`, trusted only for
  `repo:exernt@176450248/meditation-server@1425480678:environment:dev`;
- `wm-prod-github-deployer`, trusted only for
  `repo:exernt@176450248/meditation-server@1425480678:environment:production`.

Record the two role ARNs from the CloudFormation outputs. If the repository is
renamed or transferred, update and deploy the trust conditions before the old
repository leaves team control.

## 5. Configure the shared API Gateway logging role

API Gateway REST APIs expose one CloudWatch logging role setting per AWS
account and Region. Configure it once in `ap-southeast-2`; do not let the
environment-specific `WmApi-dev` and `WmApi-prod` stacks each create an
`AWS::ApiGateway::Account` resource. They retain separate APIs, stages, Lambda
functions, and log groups while sharing only this service-level prerequisite.

Create or select an IAM role that trusts `apigateway.amazonaws.com` and has the
AWS-managed `service-role/AmazonAPIGatewayPushToCloudWatchLogs` policy. In the
API Gateway console, select **Asia Pacific (Sydney)**, open **Settings**, choose
**Edit** under **Logging**, paste the role ARN into **CloudWatch log role ARN**,
and save it.

The equivalent CLI operation is:

```powershell
$apiGatewayLogsRoleArn = "PASTE_API_GATEWAY_CLOUDWATCH_ROLE_ARN"
aws apigateway update-account `
  --patch-operations "op=replace,path=/cloudwatchRoleArn,value=$apiGatewayLogsRoleArn" `
  --profile wm-admin `
  --region ap-southeast-2

aws apigateway get-account `
  --profile wm-admin `
  --region ap-southeast-2 `
  --query cloudwatchRoleArn `
  --output text
```

The repository deliberately sets `cloud_watch_role=False` on each REST API and
sets the CDK `disableCloudWatchRole` feature flag. This prevents concurrent dev
and production deployments from overwriting the shared account setting.

## 6. Create the GitHub environments

In GitHub, create environments named exactly `dev` and `production`. Require
reviewers for `production` and prevent administrator bypass if the GitHub plan
supports that rule. The deployment workflow runs only from `main`, so keep
`main` protected.

Configure these variables separately in each environment. For the manual
production rehearsal, use the following production sources rather than copying
the development environment wholesale:

| Variable | Production rehearsal value | Where to obtain or decide it |
| --- | --- | --- |
| `AWS_ACCOUNT_ID` | Twelve-digit target account ID | `aws sts get-caller-identity --profile wm-admin --query Account --output text` |
| `AWS_DEPLOY_ROLE_ARN` | ARN of `wm-prod-github-deployer` | `WmIdentity` CloudFormation output `GitHubDeployerProdRoleArn` |
| `DATABASE_CLUSTER_ARN` | ARN of `wm-prod-aurora-postgres` | `ClusterArn` printed by `provision-aurora-express.ps1 -Stage prod`, or `aws rds describe-db-clusters --db-cluster-identifier wm-prod-aurora-postgres --query "DBClusters[0].DBClusterArn" --output text` |
| `API_RESERVED_CONCURRENCY` | Leave unset | The quota increase was rejected; a positive reservation cannot be configured while the regional concurrency quota is 10 |
| `WORKER_MAX_CONCURRENCY` | `2` | Initial safety value in the architecture capacity plan; do not raise it without queue and downstream evidence |
| `MONTHLY_BUDGET_USD` | Team-approved rehearsal ceiling | Deployment register/budget owner decision; this is not discoverable from AWS |
| `BUDGET_ALERT_EMAIL` | Maintained team/client mailbox | Deployment register; the recipient must confirm the SNS subscription |
| `OPERATIONAL_ALERT_EMAIL` | Maintained team/client operations mailbox | Deployment register; it may initially match `BUDGET_ALERT_EMAIL`, and the recipient must confirm the SNS subscription |
| `GOOGLE_MAPS_SECRET_ARN` | ARN of the production/rehearsal server key secret | Secrets Manager in `ap-southeast-2`; select by the agreed secret name/tags, never copy the secret value into GitHub |
| `OPENAI_SECRET_ARN` | ARN of the production/rehearsal OpenAI secret | Secrets Manager in `ap-southeast-2`; select by the agreed secret name/tags, never copy the secret value into GitHub |
| `PASSWORD_RESET_FROM_EMAIL` | A verified SES email identity | SES **Verified identities** in `ap-southeast-2`, or `aws sesv2 list-email-identities`; a client-owned domain is deferred until after handover |
| `PASSWORD_RESET_LINK_BASE` | First pass: invalid HTTPS placeholder; second pass: deployed API URL plus `/auth/password-reset/open` | `WmApi-prod` output `ApiBaseUrl` from the first deployment |
| `EMAIL_VERIFICATION_LINK_BASE` | First pass: invalid HTTPS placeholder; second pass: deployed API URL plus `/auth/email-verification/open` | `WmApi-prod` output `ApiBaseUrl` from the first deployment |
| `RESPONSE_EXPORT_LINK_BASE` | First pass: invalid HTTPS placeholder; second pass: deployed API URL plus `/response-exports` | `WmApi-prod` output `ApiBaseUrl` from the first deployment |
| `TTS_PROVIDER` | `polly` for live audio verification, otherwise `disabled` | Release-test decision; both paths must eventually be tested |
| `WAF_MODE` | `count` for the first rehearsal, then `block` after reviewing matches and tuning false positives | Release-test decision recorded with the deployment; never move to `block` without reviewing the redacted WAF logs |
| `SMOKE_TEST_TTS_VOICE` | Optional `male` or `female` | Must match the current voice of the synthetic smoke account's active study; leave unset for a text-only rehearsal |

Store the smoke account credentials as GitHub environment **secrets**, not
variables:

| Secret | Meaning |
| --- | --- |
| `SMOKE_TEST_EMAIL` | Email of a dedicated, verified, enabled synthetic account |
| `SMOKE_TEST_PASSWORD` | Password for that account |

The deployment workflow reads the API URL from the `WmApi-{stage}` CloudFormation
output, then checks API health, database access, login, authenticated identity,
deterministic script generation, refresh-token rotation, and logout. If
`SMOKE_TEST_TTS_VOICE` is set, the account must also be an active participant
whose current study uses that voice; the suite requests a short synthetic
narration and verifies that non-empty MP3 audio is returned. Do not use a real
participant or researcher account.

Keep the account exclusive to this workflow. A successful login intentionally
increments its session version and revokes earlier refresh tokens, so sharing the
account with a person or another concurrent test would invalidate their session.

The configuration variables and ARNs are not secret contents; the smoke account
credentials remain environment secrets and API keys remain in Secrets Manager.
The removed `DATABASE_MIN_ACU`, `DATABASE_MAX_ACU`, and
`DATABASE_READER_ENABLED` variables do not apply to Express Configuration.

The full free-plan rehearsal sequence, including the documented load-test
exception and the WAF count-to-block transition, is in
[`production-deployment-rehearsal.md`](production-deployment-rehearsal.md).

## 7. Provision the Free Plan Aurora prerequisite

Use `dev` for ordinary Free Plan validation:

```powershell
.\scripts\provision-aurora-express.ps1 -Stage dev -Profile wm-admin
```

For the one-off, synthetic production deployment rehearsal, first confirm that
the account contains fewer than two Aurora clusters and that the second cluster
fits within the remaining Free Plan credits and limits. Then use the guarded
production path:

```powershell
.\scripts\provision-aurora-express.ps1 `
  -Stage prod `
  -Profile wm-admin `
  -AllowProduction
```

This creates an Aurora PostgreSQL serverless Express cluster using the internet
access gateway and IAM-only master authentication; connect for the one-time
initialisation with a token generated by `aws rds generate-db-auth-token`. The
script applies and verifies the Free Plan maximum of one-day backup retention,
deletion protection, Data API, and PostgreSQL log export. After upgrading to a
paid plan, pass `-BackupRetentionDays 30` to apply the production retention
baseline; longer retention can create backup-storage charges.

The script uses Express Configuration, then enables the Data API and PostgreSQL
log export, sets one-day development backup retention, and sets the PostgreSQL
CloudWatch log group to 14-day retention (90 days in production). It also creates
and configures a version-matched cluster parameter group with privacy-preserving
error, slow-query, lock, temporary-file, and autovacuum logging. New clusters get
that parameter group before application initialization.

For an existing cluster, first run without the parameter-group switch and review
the named logging group reported by the script. After confirming the cluster's
pending-reboot state and approved maintenance window, schedule the association:

```powershell
.\scripts\provision-aurora-express.ps1 -Stage dev -Profile wm-admin -ApplyLoggingParameterGroup
```

The existing-cluster path does not use `--apply-immediately`; AWS schedules the
association for the maintenance window. Copy the script's `ClusterArn` result
into the GitHub `dev` variable `DATABASE_CLUSTER_ARN`. Do not create another
Aurora cluster from the RDS full-configuration path: that is not Free Plan
eligible.

## 8. Perform the one-time database credential handoff

Aurora Express uses IAM-only master authentication and does not create an
RDS-managed master secret. The Data API cannot use the Express master identity,
so first deploy the data stack to create a separate generated `wm_app` secret:

```powershell
npx cdk deploy WmData-dev --profile wm-admin --require-approval broadening `
  -c stage=dev `
  -c databaseClusterArn=PASTE_CLUSTER_ARN `
  -c workerMaxConcurrency=2 `
  -c monthlyBudgetUsd=20 `
  -c budgetAlertEmail=TEAM_EMAIL `
  -c operationalAlertEmail=OPERATIONAL_TEAM_EMAIL `
  -c googleMapsSecretArn=GOOGLE_SECRET_ARN `
  -c openAiSecretArn=OPENAI_SECRET_ARN `
  -c passwordResetFromEmail=VERIFIED_SES_EMAIL `
  -c passwordResetLinkBase=https://example.invalid/auth/password-reset/open `
  -c emailVerificationLinkBase=https://example.invalid/auth/email-verification/open `
  -c responseExportLinkBase=https://example.invalid/response-exports
```

Use the `DatabaseApplicationSecretArn` output to retrieve the generated password
into an environment variable without printing it. Generate an IAM database token
for the Express endpoint and run the Python initializer from the activated virtual
environment. This path does not require the separately installed `psql` executable:

```powershell
$cluster = (aws rds describe-db-clusters --db-cluster-identifier wm-dev-aurora-postgres --profile wm-admin --region ap-southeast-2 | ConvertFrom-Json).DBClusters[0]
$secretArn = (aws cloudformation describe-stacks --stack-name WmData-dev --profile wm-admin --region ap-southeast-2 | ConvertFrom-Json).Stacks[0].Outputs.Where({$_.OutputKey -eq "DatabaseApplicationSecretArn"}).OutputValue
$secret = aws secretsmanager get-secret-value --secret-id $secretArn --profile wm-admin --region ap-southeast-2 | ConvertFrom-Json
$env:WM_APP_PASSWORD = ($secret.SecretString | ConvertFrom-Json).password
$env:PGPASSWORD = aws rds generate-db-auth-token --hostname $cluster.Endpoint --port $cluster.Port --username $cluster.MasterUsername --profile wm-admin --region ap-southeast-2
python scripts/initialize_aurora_express.py --host $cluster.Endpoint --port $cluster.Port --admin-user $cluster.MasterUsername
Remove-Item Env:WM_APP_PASSWORD, Env:PGPASSWORD
```

If PostgreSQL client tools are installed separately, the equivalent SQL remains
available in `scripts/initialize-aurora-express.psql` for use with `psql` 14 or
later.

Do not paste either password or token into chat, tickets, shell history, or
CloudFormation parameters. Repeat this coordinated procedure when rotating the
application database password.

## 9. Run the first OIDC deployment

Set clearly invalid HTTPS verification and export links for the first pass, run
the `Deploy` workflow for `dev`, then record the API Gateway endpoint output.
Change `EMAIL_VERIFICATION_LINK_BASE` to that endpoint plus
`/auth/email-verification/open`, change `RESPONSE_EXPORT_LINK_BASE` to that
endpoint plus `/response-exports`, and rerun the deployment before enabling
registration or exports. This two-pass configuration avoids a CloudFormation
dependency cycle.

Verify:

1. GitHub obtained credentials through `AssumeRoleWithWebIdentity`; there are no
   AWS access keys in GitHub.
2. API Gateway account settings in `ap-southeast-2` contain the shared
   CloudWatch logging role ARN.
3. CloudFormation shows `WmData-dev`, `WmGeneration-dev`, `WmExports-dev`,
   `WmApi-dev`, and `WmOperations-dev` as `CREATE_COMPLETE` or
   `UPDATE_COMPLETE`.
4. RDS shows one Express cluster, Data API enabled, at most 4 ACUs, at most 1 GiB
   storage, one-day backup retention, and no extra reader/cluster.
5. The API and worker roles have `WM-CdkBoundary`, exact cluster-scoped
   `rds-data` permissions, and read access only to their required secrets.
6. CloudTrail is single-region in `ap-southeast-2`; the audit bucket is retained,
   versioned, and has log validation enabled.
7. API health, Data API access, queue/DLQ, SES sandbox, Polly or text-only mode,
   and synthetic smoke tests pass.
8. An attempted unapproved production workflow waits at the GitHub environment
   gate, and the dev role cannot assume the production role.

The full synthetic development deployment can therefore be verified on the
Free Plan. Before the Free Plan expires—or before a long-lived production
deployment—the client must upgrade the AWS account to a paid plan and review
Aurora capacity, backup retention, deletion protection, HA, and cost settings.
After that upgrade, the client may retain a synthetic-data development stack
alongside production for feature validation. Each environment must use its own
Aurora cluster ARN and secrets; production data must never be copied into dev.
