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

Configure these variables separately in each environment:

| Variable | Meaning |
| --- | --- |
| `AWS_ACCOUNT_ID` | Expected twelve-digit AWS account ID |
| `AWS_DEPLOY_ROLE_ARN` | Matching `wm-*-github-deployer` ARN |
| `DATABASE_CLUSTER_ARN` | ARN emitted by the Aurora Express provisioning script |
| `API_RESERVED_CONCURRENCY` | Optional API ceiling; leave unset while the account concurrency quota is 10 |
| `WORKER_MAX_CONCURRENCY` | SQS event-source maximum concurrency |
| `MONTHLY_BUDGET_USD` | Approved monthly budget |
| `BUDGET_ALERT_EMAIL` | Maintained operational address |
| `GOOGLE_MAPS_SECRET_ARN` | Existing Secrets Manager ARN; API and generation worker |
| `OPENAI_SECRET_ARN` | Existing Secrets Manager ARN; worker only |
| `PASSWORD_RESET_FROM_EMAIL` | Verified SES sender |
| `PASSWORD_RESET_LINK_BASE` | HTTPS API URL ending in `/auth/password-reset/open` |
| `EMAIL_VERIFICATION_LINK_BASE` | HTTPS API URL ending in `/auth/email-verification/open` |
| `TTS_PROVIDER` | `polly` or `disabled` |

These values and ARNs are not secret contents. API keys remain in Secrets
Manager. The removed `DATABASE_MIN_ACU`, `DATABASE_MAX_ACU`, and
`DATABASE_READER_ENABLED` variables do not apply to Express Configuration.

## 7. Provision the Free Plan Aurora prerequisite

Do this for `dev` only while validating on the Free Plan:

```powershell
.\scripts\provision-aurora-express.ps1 -Stage dev -Profile wm-admin
```

The script uses Express Configuration, then enables the Data API, PostgreSQL log
export, and one-day backup retention. Copy its `ClusterArn` result into the
GitHub `dev` variable `DATABASE_CLUSTER_ARN`. Do not create another Aurora
cluster from the RDS full-configuration path: that is not Free Plan eligible.

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
  -c googleMapsSecretArn=GOOGLE_SECRET_ARN `
  -c openAiSecretArn=OPENAI_SECRET_ARN `
  -c passwordResetFromEmail=VERIFIED_SES_EMAIL `
  -c passwordResetLinkBase=https://example.invalid/auth/password-reset/open `
  -c emailVerificationLinkBase=https://example.invalid/auth/email-verification/open
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

Set a clearly invalid HTTPS verification link for the first pass, run the
`Deploy` workflow for `dev`, then record the API Gateway endpoint output. Change
`EMAIL_VERIFICATION_LINK_BASE` to that endpoint plus
`/auth/email-verification/open` and rerun the deployment before enabling
registration. This two-pass configuration avoids a CloudFormation dependency
cycle.

Verify:

1. GitHub obtained credentials through `AssumeRoleWithWebIdentity`; there are no
   AWS access keys in GitHub.
2. API Gateway account settings in `ap-southeast-2` contain the shared
   CloudWatch logging role ARN.
3. CloudFormation shows `WmData-dev`, `WmGeneration-dev`, `WmApi-dev`, and
   `WmOperations-dev` as `CREATE_COMPLETE` or `UPDATE_COMPLETE`.
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
