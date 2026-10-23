# AWS account and repository setup tutorial

Last reviewed: 2026-10-23

Target architecture: [`aws-architecture-proposal.md`](aws-architecture-proposal.md)

Target region: Asia Pacific (Sydney), `ap-southeast-2`

This guide tracks the setup and implementation order for the project's current
single-account, free-tier-shaped architecture. It deliberately avoids baseline
resources with fixed hourly costs: no NAT Gateway, RDS Proxy, custom domain, or
always-on development/production pair while Free Plan validation is underway.
After the required paid-plan upgrade, separate development and production
stacks may coexist. WAF is created only for production and is a required
public-launch control.

The Free Plan is not an unlimited or permanent zero-cost plan. The current
requirement is to complete one full synthetic development deployment and its
verification while the account remains on that plan. Aurora is eligible only
when created with Express Configuration. Check remaining credits, expiry, and
service usage before each deployment. The client is responsible for upgrading
to a paid plan before expiry and before the long-lived production rollout.

## 1. Record the decisions first

Create a deployment register in the repository and agree on these values:

| Item | Current starting value |
| --- | --- |
| Workload name | `walking-meditation` |
| AWS account | One account, owned by or transferable to the client |
| AWS region | `ap-southeast-2` only |
| Repository | `exernt/meditation-server` |
| Free Plan validation state | One `dev` stack, synthetic data only |
| Paid-plan operating state | Separate `dev` and `production` stacks; dev remains synthetic |
| API hostname initially | Generated API Gateway URL |
| GitHub environments | `dev` and `production`, both targeting the same account |
| Resource prefix | `wm-dev-` / `wm-prod-` |
| Required tags | `Application`, `Environment`, `Owner`, `ManagedBy`, `DataClassification` |
| Data classification | `sensitive-research` for database, logs, and queues |
| TTS | Optional Polly: `Russell`, standard engine, `en-AU`; `disabled` remains supported |
| Recovery baseline | Production 30-day PITR; development 1 day; monthly restore test |
| Alert recipients | Maintained team/client mailbox, not one student's address |

Record owners for the AWS root account, billing, security alerts, production
approval, database restore, SES reputation, and DLQ incidents. Do not bind
critical ownership to a team member who may leave the project.

## 2. Prepare the single AWS account for handover

Do not create or join an AWS Organization for this phase. AWS currently states
that doing so upgrades a Free account plan to a paid plan and, when the upgrade
is triggered by Organizations, expires the remaining Free Tier credits. Check
the current rule before revisiting this decision: [AWS Free Tier plan FAQ](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-FAQ.html).

Inside the one account:

1. Confirm the account can be handed to the client without transferring a
   student's personal email, phone, payment method, or MFA device.
2. Record the 12-digit account ID and the current Free Tier/credit expiry.
3. Keep only one deployed application environment during Free Plan validation;
   permit separate dev and production stacks only after the paid-plan upgrade.
4. Give `dev` and `production` distinct stack names, roles, secrets, and tags.
5. Never copy production data into `dev`; stack naming is not a security
   boundary.
6. At handover, deploy production from a reviewed release, test it, then follow
   [`account-handover-checklist.md`](account-handover-checklist.md) to transfer
   ownership and billing. Retain development only as a synthetic-data feature
   validation environment with separately approved cost limits.

Separate stacks provide operational organization, not a hard security or quota
boundary. If the client later requires stronger team isolation, move production
and non-production into separate accounts. The code and naming scheme should
permit that later change, but it is not part of the current build.

Also defer AWS Control Tower, Service Control Policies, organization/tag/backup
policies, organization-wide CloudTrail, delegated security administrators,
consolidated billing, and organization-level IAM Identity Center. These all use
AWS Organizations directly or are meaningful only in a multi-account
organization. None is required by the current CDK, GitHub OIDC, budget,
CloudWatch, SES, Aurora/Data API, Lambda, API Gateway, or SQS steps.

## 3. Secure the account before workloads

1. Register at least two root-user MFA methods, preferably security keys. Never
   create root access keys.
2. Set root, billing, security, and operations contacts to maintained client or
   project addresses.
3. Do **not** create or join AWS Organizations and do not enable an
   organization instance of IAM Identity Center during the Free account plan.
4. Create named IAM console users for the small number of human operators and
   require MFA. Put permissions on groups/roles rather than duplicating policy
   on each user:
   - `WM-Administrators` for the limited infrastructure team;
   - `WM-Developers` for development deployment and logs;
   - `WM-Production-ReadOnly` for normal inspection; and
   - `WM-Production-Operators` for approved production actions.
5. Give these users console passwords but no long-lived access keys. Attach the
   narrowly scoped permissions required for their work and
   `SignInLocalDevelopmentAccess` where a user needs the browser-based
   `aws login` flow described in section 7.
6. Document a break-glass process and the client handover procedure.

Use least privilege for workload roles. The API, generation worker,
maintenance function, migration runner, and GitHub deployment jobs should not
share one broad runtime role.

An IAM Identity Center account instance can exist in a standalone account, but
it supports application assignments rather than sign-on to the AWS account.
The organization instance that supports AWS account permission sets would
require AWS Organizations, so it is deferred until after the client knowingly
upgrades the account plan.

References: [AWS account administrator security practices](https://docs.aws.amazon.com/signin/latest/userguide/best-practices-admin.html),
[IAM security best practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html),
and [Identity Center instance capabilities](https://docs.aws.amazon.com/singlesignon/latest/userguide/identity-center-instances.html).

## 4. Put cost and audit guardrails in place

Before the first CDK deployment:

1. Enable Free Tier usage alerts and create a monthly AWS Budget with alerts at
   50%, 80%, and 100%, plus forecasted 100%.
2. Send alerts to the maintained mailbox and verify the subscription.
3. Enable cost-allocation tags matching section 1.
4. Enable Cost Anomaly Detection for the account.
5. Deliver single-region CloudTrail management events for `ap-southeast-2` to
   the retained, versioned audit bucket with log-file validation. Multi-region
   features are unavailable on the Free Plan. Do not enable paid S3/Lambda data
   events until a concrete audit requirement exists.
6. Enable IAM Access Analyzer. Review public and cross-account findings.
7. Set explicit CloudWatch log retention (start with 14 days in development)
   and alarms only for actionable conditions.

Defer GuardDuty, Security Hub, customer-managed KMS keys, and CloudTrail data
events until the production threat model and client budget are agreed. These
are useful controls, but some can create ongoing usage charges. Reassess them
before processing real participant data.

Budgets alert; they do not normally stop resources. Add an automatic shutdown
only if it has been designed not to destroy production data. See
[AWS Free Tier account plans](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier.html)
and [creating a cost budget](https://docs.aws.amazon.com/cost-management/latest/userguide/create-cost-budget.html).

## 5. Defer the custom domain; configure email deliberately

### API URL

Use the generated API Gateway `execute-api` HTTPS URL during development and
initial handover. This avoids a Route 53 hosted-zone charge and removes the
need to establish DNS and ACM ownership now.

Before a branded public release, add a Regional API Gateway custom domain, an
ACM certificate in `ap-southeast-2`, and DNS only after the client confirms who
owns the domain. Treat that as a separately costed production enhancement.

### SES sender

SES remains part of the minimal design because password reset and verification
email need a transactional sender. A domain is **not** required to confirm the
integration:

1. In the SES console for `ap-southeast-2`, create an **email address** identity
   for a shared project/client mailbox and click the verification link delivered
   to it. Identity verification is regional, so do not verify it in another
   region by mistake.
2. Keep the account in the SES sandbox for development. The From address must
   be the verified identity. A real recipient must also be verified while the
   account remains in the sandbox.
3. For a real end-to-end content test, either send from and to the same verified
   mailbox or verify a second team-controlled recipient. Confirm subject,
   text/HTML rendering, reset URL, expiry wording, and that no token is logged.
4. Use the SES mailbox simulator for automated AWS integration tests. It does
   not require recipient verification and can exercise success, bounce,
   complaint, out-of-office, and suppression-list handling. For example:
   - `success@simulator.amazonses.com`
   - `bounce@simulator.amazonses.com`
   - `complaint@simulator.amazonses.com`
5. Keep an in-memory/recording `EmailSender` adapter for unit and local tests.
   Assert the recipient, template variables, and reset link without sending
   externally. The SES adapter test separately proves IAM and API integration.
6. Do not request production access merely to test functionality. Before a
   public release, verify the client-owned domain, publish DKIM and SPF/DMARC,
   request SES production access, and document bounce/complaint handling.

An individually verified address is suitable for development, but not the
final branded sender: it verifies only that exact address and does not establish
domain-level DKIM ownership or allow arbitrary addresses at the domain.

References: [SES verified identities](https://docs.aws.amazon.com/ses/latest/dg/verify-addresses-and-domains.html)
and [SES mailbox simulator](https://docs.aws.amazon.com/ses/latest/dg/send-an-email-from-console.html#send-email-simulator).

## 6. Check quotas and keep concurrency bounded

Do not assume a new/free-tier account has the standard Lambda regional quota.
Check it:

```powershell
aws lambda get-account-settings `
  --region ap-southeast-2 `
  --profile wm-admin
```

Also inspect:

- Lambda concurrent executions;
- API Gateway account throttles;
- Aurora Serverless v2 capacity and Data API quotas;
- SES daily/per-second sending quotas;
- SQS quotas; and
- Secrets Manager quotas.

The account currently has a regional Lambda concurrency quota of 10, which is
too small to configure any positive reserved concurrency because Lambda must
leave 100 units unreserved. Leave API reserved concurrency unset for initial
deployment testing; setting it to `0` would disable the API. Keep worker SQS
maximum concurrency at 2 so the worker cannot consume the whole shared pool.
Request and receive a quota increase before the 100-user load test, then choose
an API reservation from measured peak concurrency and downstream capacity.

The worker uses SQS batch size 1 and maximum concurrency 2 during initial
deployment testing. API Gateway, per-account, and per-user limits protect the
database and third-party bills. No provisioned concurrency is used initially.

Reference: [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html).

## 7. Install local tooling without permanent AWS keys

Install on developer workstations:

- Git;
- Python 3.13;
- Node.js LTS for the CDK CLI;
- AWS CLI v2.33.0 or later, for browser-based temporary credentials and Aurora
  Express Configuration;
- Docker Desktop for Linux-compatible builds and disposable PostgreSQL; and
- the Android toolchain only where mobile work is required.

Sign in using the named IAM console user and obtain temporary CLI credentials:

```powershell
aws login --profile wm-admin --region ap-southeast-2
aws sts get-caller-identity --profile wm-admin
aws configure get region --profile wm-admin
```

The browser flow uses the user's MFA-protected console session and caches
temporary credentials for up to 12 hours; it does not create an IAM access key.
Run `aws logout --profile wm-admin` when finished. Set `ap-southeast-2` as the
profile region and confirm the account ID before every bootstrap or deployment.
Never place `.aws`, login cache files, `.env`, keys, keystores, or database
exports in the repository.

Reference: [AWS CLI login with console credentials](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html).

## 8. Bootstrap CDK once

An authorized administrator bootstraps the single account/region once:

```powershell
aws login --profile wm-admin --region ap-southeast-2
$wmIdentity = aws sts get-caller-identity --profile wm-admin | ConvertFrom-Json
npm ci
npx cdk bootstrap "aws://$($wmIdentity.Account)/ap-southeast-2" `
  --profile wm-admin `
  --termination-protection `
  --custom-permissions-boundary WM-CdkBoundary
```

Keep `CDKToolkit` under termination protection. Both logical environments use
the same bootstrap resources; do not bootstrap twice merely because stack names
differ.

Reference: [CDK bootstrapping](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping-env.html).

## 9. Create the repository layout

Use one repository for runtime and infrastructure, with independently buildable
packages:

```text
meditation-server/
├── src/meditation_server/
│   ├── api/
│   ├── auth/
│   ├── domain/
│   ├── application/
│   ├── repositories/
│   │   ├── sql/
│   │   ├── data_api.py
│   │   └── asyncpg.py
│   ├── integrations/
│   │   └── speech.py
│   ├── workers/
│   ├── observability.py
│   └── settings.py
├── migrations/
├── infra/
│   ├── app.py
│   ├── stacks/
│   └── tests/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── load/
├── docs/
├── scripts/
├── .github/workflows/
├── pyproject.toml
├── requirements-api.lock
├── requirements-worker.lock
├── requirements-infra.lock
├── requirements.txt
├── cdk.json
├── .env.example
├── .gitignore
└── README.md
```

Rules:

- Use `src/` packaging and a thin Mangum/Lambda adapter.
- Share parameterised PostgreSQL statements between an `asyncpg` local executor
  and an RDS Data API executor. Do not make SQLAlchemy connection pooling part
  of the AWS runtime.
- Represent transaction boundaries explicitly so refresh-token row locks run
  under one Data API transaction ID.
- Store migrations as ordered SQL runnable through either executor. Apply them
  explicitly; never migrate during cold start.
- Pin dependencies reproducibly and keep API, worker, infrastructure, migration,
  and development dependencies separable.
- Exclude Qwen, Torch, torchaudio, FFmpeg, model files, generated audio, and
  scientific/visualisation packages from Lambda artifacts.
- Do not commit prompts, questionnaire responses, production exports, or
  location traces.

The `.gitignore` should cover at least:

```gitignore
.env
.env.*
!.env.example
.venv/
.aws/
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage*
htmlcov/
cdk.out/
node_modules/
dist/
build/
*.pem
*.key
*.p12
*.jks
*.keystore
var/
generated/
```

## 10. Provision Aurora Express and implement the Data API boundary

The AWS runtime calls Aurora over signed HTTPS. It does not open PostgreSQL
sockets and is not attached to a customer VPC.

The AWS Free Plan permits Aurora PostgreSQL only through **Express
Configuration**. Express creates service-managed networking and an IAM-only
master identity; it does not create a master password or an RDS-managed master
secret. It uses the default supported PostgreSQL engine version and cannot be
created with the full-configuration VPC/subnet/security-group controls.
CloudFormation/CDK does not currently expose the Express create option, so the
cluster is an explicit account prerequisite:

```powershell
.\scripts\provision-aurora-express.ps1 -Stage dev -Profile wm-admin
```

The script calls `create-db-cluster --with-express-configuration`, waits for the
cluster, enables the Data API and PostgreSQL log export, and configures one-day
development backup retention. Pass its cluster ARN to CDK as
`databaseClusterArn`. CDK then creates a separate generated `wm_app` secret.
Use the IAM master token once with
`scripts/initialize_aurora_express.py` from the project virtual environment to
create the database role whose password matches that secret. The equivalent
`scripts/initialize-aurora-express.psql` file remains available when PostgreSQL
client tools are installed separately. The Data API cannot use the Express
master user.

1. Define a small executor interface for parameterised statements, batches,
   and transactions.
2. Implement `AsyncpgExecutor` for local integration tests.
3. Implement `RdsDataApiExecutor` using the AWS SDK.
4. Keep repository SQL shared between the two executors.
5. Implement begin/commit/rollback and pass the transaction ID through each
   transaction-scoped repository call.
6. Add integration tests for refresh rotation/replay and concurrent row locks.
7. Paginate exports and large result sets; Data API responses are limited to
   1 MiB.
8. Add retry with bounded jitter only for safe/idempotent operations.

The API, worker, maintenance, and migration roles need only the exact cluster
and secret ARN permissions required for their statements. The Free Plan limits
each eligible cluster to 4 ACUs and 1 GiB storage and permits at most two
clusters/two instances in the account; this project deliberately uses one
development cluster for verification.

References: [using the RDS Data API](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.html),
[supported regions and engines](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.Aurora_Fea_Regions_DB-eng.Feature.Data_API.html),
and [Data API limitations](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.limitations.html).

## 11. Keep TTS optional from the first commit

Define a `SpeechSynthesizer` protocol with Polly and disabled adapters:

```text
TTS_PROVIDER=polly
TTS_VOICE_ID=Russell
TTS_ENGINE=standard
TTS_LANGUAGE_CODE=en-AU
```

The provider can be changed to `TTS_PROVIDER=disabled`; script generation and
text-only playback continue without audio. Unit tests use a fake speech provider.

Grant only `polly:SynthesizeSpeech`, send narration text only, enforce Polly's
3,000 billed-character request limit, and cache MP3 chunks in Android
app-private storage. Do not add an audio bucket, Qwen/model dependencies, or
FFmpeg. The application falls back to text-only playback when synthesis is
disabled or unavailable.

## 12. Define configuration and secret ownership

| Value | Store | Notes |
| --- | --- | --- |
| Environment, log level, queue URL | Lambda environment | Non-secret values only |
| Aurora cluster ARN/database name | Lambda environment | Resource identifier, not credential |
| Aurora Express master | IAM database authentication | Operator bootstrap only; not usable by Data API |
| `wm_app` credential | CDK-created Secrets Manager secret | Used by Data API; exact ARN grants only; initialise/rotate with the database role |
| Google Maps server key | Secrets Manager | Server-restricted; not the Android key |
| OpenAI key | Secrets Manager | Worker only |
| JWT current/previous signing keys | Secrets Manager | Add `kid` before rotation |
| TTS provider | Lambda environment | `polly` or `disabled` |
| Android Maps key | Android release system | Restrict by package/certificate |

Secrets Manager has its own usage price and is not made free merely because the
caller is Lambda. Keep the minimum number of secrets, combine only values with
the same owner/rotation/access boundary, and grant exact ARN access. Never type
secret values into logged CLI commands or commit them to `.env`.

Reference: [Secrets Manager best practices](https://docs.aws.amazon.com/secretsmanager/latest/userguide/best-practices.html).

## 13. Protect the GitHub repository

In `exernt/meditation-server`:

1. Protect `main`: pull requests, an independent review, passing CI, resolved
   conversations, and no force push/deletion.
2. Require infrastructure-owner review for `infra/**`, deployment workflows,
   migrations, and IAM policy changes.
3. Enable Dependabot and secret scanning/push protection where the plan permits.
4. Pin third-party actions to full commit SHAs.
5. Create `dev` and `production` GitHub environments.
6. Require separate human approval for the production environment.
7. Store `AWS_REGION`, `AWS_ACCOUNT_ID`, and role ARNs as environment variables;
   never store static AWS access keys.

Reference: [GitHub deployment environments](https://docs.github.com/en/actions/concepts/workflows-and-actions/deployment-environments).

## 14. Connect GitHub Actions with OIDC

Use one GitHub OIDC provider in the AWS account and two roles:

- `wm-dev-github-deployer` trusts only the repository's `dev` environment;
- `wm-prod-github-deployer` trusts only the `production` environment.

Restrict the trust policy to the exact GitHub organization, repository, and
environment subject. Verify the actual subject emitted by GitHub; never use a
wildcard that allows every repository. Scope each role to the matching resource
prefixes and CDK deployment roles. This reference to an organization means the
GitHub owner namespace, not AWS Organizations.

Minimum workflow permissions:

```yaml
permissions:
  contents: read
  id-token: write
```

Use `allowed-account-ids` in `aws-actions/configure-aws-credentials` so a bad
configuration fails instead of deploying elsewhere. The two roles are
separation of deployment authority inside one account, not separate security
or billing boundaries.

References: [AWS IAM GitHub OIDC guidance](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html)
and [`configure-aws-credentials`](https://github.com/aws-actions/configure-aws-credentials).

## 15. Define the CI/CD lifecycle for separate environments

### Pull-request CI — no AWS credentials

- install from lock files;
- format, lint, type-check, and unit test;
- run authorization and refresh-token concurrency tests;
- run migrations against disposable PostgreSQL through `AsyncpgExecutor`;
- synthesize/test CDK and scan source, dependencies, and templates; and
- confirm Lambda artifacts contain no model/audio dependencies.

### Development deployment

- deploy only from protected `main` through the `dev` GitHub environment;
- build Linux-compatible artifacts;
- show and retain `cdk diff` before deployment;
- apply ordered SQL migrations through the non-public migration command;
- run health, auth, Data API, queue/DLQ, SES simulator, and synthetic smoke
  tests; and
- use synthetic users, answers, and routes only.

### Handover/production deployment

- deploy from a reviewed immutable release through the protected `production`
  environment;
- require human approval and show the CDK diff;
- create a clean production stack rather than promoting the development data;
- apply backward-compatible migrations and smoke tests without sensitive logs;
- confirm backup/restore, alarms, client access, billing, and runbooks; and
- retain development only with synthetic data, separate secrets and database,
  and an approved ongoing budget.

Use GitHub concurrency groups so two jobs cannot mutate the same environment.
Dev and production jobs may run concurrently, so neither environment stack may
own account/Region singletons such as the API Gateway CloudWatch logging role.

## 16. Implement the CDK stacks in order

Keep the stack set small:

1. **Account access/cost setup** — GitHub OIDC provider and deployer roles where
   these are not managed manually; the shared API Gateway CloudWatch logging
   role setting; budgets and notification topics.
2. **Aurora prerequisite** — one externally provisioned Aurora PostgreSQL
   Express cluster for Free Plan verification, with Data API enabled.
3. **Data stack** — generated `wm_app` application secret and the documented
   one-time credential handoff. The Express cluster remains outside
   CloudFormation until the account is paid and a reviewed migration is needed.
4. **Generation stack** — SQS queue/DLQ, worker, maintenance schedule, queue-age
   and DLQ alarms. No server-side audio storage.
5. **API stack** — API Lambda, API Gateway REST API, gateway throttling, SES
   permission, log retention, and core alarms.
6. **Migration/operations** — non-public migration command/function, restore and
   DLQ runbooks, and narrowly scoped operator permissions.

Aurora Express uses AWS-managed connectivity through its internet access
gateway. Application Lambdas are not VPC-attached and reach it through signed
Data API HTTPS calls. There is no customer VPC, RDS Proxy, NAT Gateway, Route 53
zone, or custom domain. WAF is attached only to production.

Use AWS-managed encryption at rest. Add a customer-managed KMS key later only
for an explicit requirement with a named rotation/deletion owner.

## 17. Map application configuration

| Variable | Initial AWS target |
| --- | --- |
| `GOOGLE_MAPS_API_KEY` | Exact Secrets Manager secret, API/route component only |
| `OPENAI_API_KEY` | Exact Secrets Manager secret, worker only |
| `DATABASE_RESOURCE_ARN` | Aurora cluster ARN in environment/config |
| `DATABASE_SECRET_ARN` | CDK-created `wm_app` secret ARN in environment/config |
| `DATABASE_NAME` | Non-secret environment value |
| `JWT_SECRET_KEY` | Replace with current/previous secret ARNs plus `kid` support |
| `EMAIL_BACKEND` | `ses` |
| `AWS_REGION` | `ap-southeast-2` |
| `PASSWORD_RESET_FROM_EMAIL` | Verified SES identity |
| `PASSWORD_RESET_LINK_BASE` | Generated API Gateway URL until custom domain exists |
| `EMAIL_VERIFICATION_LINK_BASE` | Generated API Gateway URL ending in `/auth/email-verification/open` |
| `TTS_PROVIDER` | `polly` for audio testing, or `disabled` for text-only testing |
| `ANDROID_APP_PACKAGE_NAME` | Production Android application ID |
| `ANDROID_APP_SHA256_CERT_FINGERPRINTS` | Release signing fingerprints only |

Remove direct-connection settings such as `DATABASE_URL`, pool size, pool
overflow, proxy endpoint, and VPC settings from the AWS runtime. Local tests may
use a separate `LOCAL_DATABASE_URL` that is never deployed.

When the release URL becomes stable, configure the Android build's
`SERVER_BASE_URL`. Verified Android App Links may require a client-owned custom
domain; treat that as the trigger to add DNS/ACM rather than provisioning them
speculatively now.

## 18. Pre-implementation checklist

- [ ] AWS account ID, current Free Tier/credit expiry, owner, and handover plan are recorded.
- [ ] Root MFA, contacts, named IAM users with MFA, and break-glass steps are complete.
- [ ] Budget, Free Tier, and anomaly alerts reach a maintained mailbox.
- [ ] Sydney is selected and quotas are documented.
- [ ] CDK is bootstrapped once with termination protection.
- [ ] API Gateway account settings in Sydney point to the shared CloudWatch
      logging role; environment API stacks do not own `AWS::ApiGateway::Account`.
- [ ] The current `WM-CdkBoundary` version permits the required `rds-data` actions.
- [ ] One development Aurora cluster was created with Express Configuration;
      Data API is enabled and its ARN is recorded.
- [ ] The CDK-created `wm_app` secret and PostgreSQL role have matching credentials.
- [ ] `main`, GitHub environments, and exact OIDC trust are protected.
- [ ] No static AWS key or application secret exists in GitHub or the repo.
- [ ] The package layout, dependency locks, tests, and ignore rules exist.
- [ ] Shared SQL, local asyncpg, and AWS Data API executors are designed.
- [ ] Refresh-token transaction/race tests pass through both executors.
- [ ] Migrations are explicit and never run at cold start.
- [ ] API/worker artifacts contain no Qwen, Torch, FFmpeg, model, or audio stack.
- [ ] Both Polly synthesis and `TTS_PROVIDER=disabled` text-only fallback are tested.
- [ ] Logs redact tokens, emails, answers, feedback, prompts, coordinates, polylines, and scripts.
- [ ] Consent, retention, overseas disclosure, and research-ethics decisions have owners.
- [ ] Only synthetic data is used before the production handover deployment.

## 19. Explicitly deferred work

Do not accidentally reintroduce these while following older EC2/Heroku notes:

- EC2, EBS sizing, or a host-local PostgreSQL container in production;
- a self-hosted Qwen/Torch TTS model in either Lambda;
- an audio bucket, signed audio URLs, or self-hosted TTS runtime;
- VPC-attached Lambdas, NAT Gateway, or RDS Proxy;
- WAF in development (production WAF remains mandatory);
- Route 53/ACM/custom domain before stable URL or App Link ownership requires it;
- customer-managed KMS keys without an explicit key-management requirement;
- multi-account or multi-region deployment without a requirement.

The first implementation milestone is a package skeleton, Data API repository
boundary, optional Polly adapter, CDK synthesis, and local tests. The first AWS
milestone is a complete synthetic development deployment on the Free Plan
followed by the defined 100-active-user test—not a production launch. Record
the plan-expiry date and the named client owner responsible for upgrading the
account before expiry.
