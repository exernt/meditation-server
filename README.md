# Walking Meditation server

This repository contains the AWS CDK foundation and the migration target for the
Walking Meditation backend. The current Lambda handlers deliberately fail closed
except for health checks until the application migration is complete.

The workload is restricted to `ap-southeast-2`. Amazon Polly is optional and is
selected with CDK context `ttsProvider=polly|disabled`; the supported initial
voice is `Russell`, Australian English, standard engine.

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
The intended validation state is one synthetic `dev` deployment on the Free
Plan. The client must upgrade to a paid plan before the Free Plan expires and
before the long-lived production rollout.

See `docs/aws-account-and-repo-setup.md`, `docs/github-actions-oidc.md`, and
`docs/account-handover-checklist.md` before bootstrapping or deploying.

