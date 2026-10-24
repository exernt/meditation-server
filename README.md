# Walking Meditation server

This repository contains the AWS CDK foundation and the migration target for the
Walking Meditation backend. The API Lambda now runs the migrated FastAPI
application through API Gateway; production remains gated on integration and
load testing.

## Current migration status

As of 2026-10-24, the synthetic `dev` infrastructure has been deployed and its
core controls have been verified in `ap-southeast-2`. Aurora PostgreSQL was
created with Express Configuration and the Data API enabled; the API Gateway
account uses the shared regional CloudWatch logging role; and the CloudTrail
management trail is delivering validated logs to its private, versioned audit
bucket.

The FastAPI boundary, native authentication flow, study, questionnaire,
questionnaire-response, feedback, Google route, and Polly TTS endpoints have
been ported. They use the shared Data API executor in AWS and preserve the
legacy paths, validation rules, authorization gates, response status codes, and
refresh-token/session semantics. Script endpoints preserve the legacy response
contract through the deterministic fallback generator. The asynchronous
job/outbox API, recovery and retention maintenance, and idempotent SQS worker
lifecycle are implemented locally; that worker also uses the fallback generator
until the OpenAI/elevation/nearby-context processor is added. Production has not
been deployed. See [`docs/generation-jobs.md`](docs/generation-jobs.md) for the
job contract and timing decisions.

The migrated application is implemented and locally tested in the current
working tree, but it has not yet been deployed over the verified synthetic
infrastructure baseline. Live Aurora endpoint tests, live Polly/device playback,
restore testing, failure-path exercises, and the 100-active-user test remain
release gates. Android integration is also pending review of the teammate's
incoming app pull request; no current mobile working-tree changes should be
treated as final until that review is complete.

The database foundation now includes ordered, checksummed PostgreSQL migrations
and a shared async executor boundary. Local integration code uses `asyncpg`;
AWS code uses the RDS Data API with the same named SQL and explicit transaction
handles. Migrations remain an operator action and are never run during Lambda
cold start.

The native authentication flow is intentionally retained rather than replaced
with Cognito. It uses Argon2id password hashes, verified email, short-lived JWTs
with issuer/audience/key IDs, opaque hashed refresh tokens, rotation and replay
revocation, account/IP login throttling, shorter admin sessions, password-reset
session invalidation, and security-event records.

The workload is restricted to `ap-southeast-2`. Amazon Polly is optional and is
selected with CDK context `ttsProvider=polly|disabled`; the supported initial
voice is `Russell`, Australian English, standard engine. See
[`docs/tts-implementation-and-testing.md`](docs/tts-implementation-and-testing.md)
for the exact request flow, current limitations, and provider comparison
procedure.

## Local infrastructure setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm ci
```

Keep the virtual environment active when running `npx cdk`. The CDK entry point
uses the `python` executable on `PATH`, which resolves to `.venv` locally and to
the Python configured by `actions/setup-python` in GitHub Actions.
The local requirements also provide a Python PostgreSQL client for Aurora
initialization; the standalone `psql` executable is not installed by `pip`.

The AWS Free Plan permits Aurora PostgreSQL only when the cluster is created
with **Express Configuration**. Because that create option is not currently
available through CloudFormation/CDK, provision the cluster once with
[`scripts/provision-aurora-express.ps1`](scripts/provision-aurora-express.ps1),
then pass its ARN as CDK context `databaseClusterArn`. CDK manages the separate
application credential and the rest of the workload; it does not replace or
delete the Express cluster.

`WmIdentity` can be synthesized and deployed without workload context. Workload
stacks require explicit database, concurrency, cost, secret, and email context.
The intended Free Plan validation state is one synthetic `dev` deployment. The
client must upgrade to a paid plan before the Free Plan expires and before the
long-lived production rollout. After that upgrade, separate `dev` and `prod`
stacks may coexist; development remains synthetic and must never receive a copy
of production data.

See `docs/aws-account-and-repo-setup.md`, `docs/github-actions-oidc.md`, and
`docs/account-handover-checklist.md` before bootstrapping or deploying.

## Database migrations

Migration files live in `migrations/` and use `-- migrate:split` between Data
API statements. The runner holds a PostgreSQL advisory transaction lock,
checks the SHA-256 checksum of every previously applied file, and commits all
pending migrations atomically.

Apply the schema to Aurora through the Data API after exporting the three
non-secret resource identifiers (do not export or print the secret value):

```powershell
$env:DATABASE_RESOURCE_ARN = "arn:aws:rds:ap-southeast-2:ACCOUNT_ID:cluster:CLUSTER_ID"
$env:DATABASE_SECRET_ARN = "arn:aws:secretsmanager:ap-southeast-2:ACCOUNT_ID:secret:SECRET_ID"
$env:DATABASE_NAME = "walking_meditation"
python scripts/apply_migrations.py --data-api --profile wm-admin --region ap-southeast-2
```

For disposable local PostgreSQL, use the same files through `asyncpg`:

```powershell
$env:DATABASE_URL = "postgresql://wm_app:password@127.0.0.1:5432/walking_meditation"
python scripts/apply_migrations.py
```

Never edit an applied migration. Add the next numbered SQL file instead.

