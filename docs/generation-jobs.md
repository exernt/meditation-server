# Generation job lifecycle

The asynchronous generation boundary is implemented with Aurora PostgreSQL,
an SQS queue, an on-demand Lambda worker, and a transactional outbox. The worker
enriches client-supplied route steps with Google weather, elevation and nearby
context, then makes three independent `gpt-6-luna` calls for focused attention,
compassion, and closing narration. Each response is constrained by a strict JSON
Schema. The worker combines the three sections, pools their token usage and
cost, and validates the complete script's structure. Word targets guide pacing
but are not enforced as acceptance ranges. A deterministic, walking-safe
fallback is returned if a provider is unavailable or generated narration is
structurally invalid; the result's `fallback` field makes that behaviour explicit.

Enrichment is bounded by route legs rather than navigation-step count. Each
leg batches its step boundaries into one Elevation request and performs one
Nearby Search around the distance-weighted midpoint segment. A park route has
two legs, so generation makes two Elevation calls, two Nearby Search calls,
and one Weather call regardless of its usual number of navigation steps.

Only route labels/instructions, terrain classifications, nearby feature names,
weather, approximate word targets, the user's stated meditation context and approved study
parameters are sent to OpenAI. Exact coordinates, polylines, user IDs, account
data and authentication material are excluded from the prompt.

## Development script archive and outcome logs

The CDK `dev` generation stack creates a private S3 bucket for operator-facing
copies of completed generation results. The worker stores one JSON object under
`generation-results/YYYY/MM/DD/<job-id>.json` after the Aurora result commits.
The three Luna section responses are reconstructed into the legacy top-level
JSON document and stored alongside it as `<job-id>.attempt-<n>.raw.json`; the
attempt suffix preserves separate outputs when a job is retried. Reconstructed
raw output is also archived when post-generation validation fails and fallback
is disabled, provided all three Luna calls returned a response.
The bucket blocks public access, requires TLS, uses server-side encryption, and
expires objects after seven days. It is deliberately absent from production.

The worker also emits a `generation_outcome` record containing the job ID,
generation kind, attempt count, fallback status, model, token usage, estimated
cost, final archive key, and raw archive keys. Narration and route data are not
duplicated into CloudWatch Logs. The development worker log group also has
seven-day retention. An S3 archive failure is logged but does not make an
already-completed job fail; Aurora remains the delivery system of record.

## API contract

`POST /v1/generation-jobs` requires authentication and an
`Idempotency-Key` header containing 16-128 ASCII letters, digits, dots,
underscores, colons, or hyphens. A UUID is the recommended client value. The
body contains `kind` (`park` or `without_park`) and the corresponding validated
legacy script payload.

The API returns `202` with the job ID and relative status/result URLs. Reusing
the same key and request returns the original job. Reusing the key for a
different request returns `409`. A user may have two queued/running jobs and may
submit 20 jobs in any rolling 24-hour period; excess submissions return `429`.

Poll `GET /v1/generation-jobs/{job_id}`. Polling every two seconds is the
initial mobile default and is also returned through `Retry-After` when
`GET /v1/generation-jobs/{job_id}/result` is called before completion. Jobs are
visible only to their owning user. Failed results return a stable error code,
not a provider exception or request content.

## Transactional outbox

The submission transaction takes a per-user PostgreSQL advisory transaction
lock, checks idempotency and quotas, and inserts both `generation_jobs` and
`generation_outbox`. Only the job ID is sent to SQS. The API attempts delivery
immediately after commit; a failure leaves the outbox record available for the
scheduled recovery function, so a committed job cannot be lost between Aurora
and SQS.

Delivery timing:

| Control | Value | Reason |
| --- | --- | --- |
| Recovery schedule | Every 1 minute | Keeps accepted jobs from remaining undispatched after an API/SQS failure. |
| Recovery batch | 25 records per invocation | Bounds Data API and SQS calls while allowing up to 1,500 recovered jobs/hour. |
| Retry delays | 1, 2, 4, 8, 16, 32, then 60 minutes | Reduces pressure during a sustained SQS or permission failure; later attempts remain capped at 60 minutes. |
| Queue retention | 4 days | Existing queue setting; allows operational recovery from a worker outage. |
| DLQ transition | After 5 receives | Existing redrive setting; avoids unlimited paid/provider retries. |
| DLQ retention | 14 days | Existing setting; gives operators time to inspect and replay failures. |

Each maintenance schedule retries a failed Lambda invocation up to three times
for one hour and then writes the scheduler event to a separate 14-day maintenance
DLQ. An SQS publish failure is deliberately surfaced as a maintenance Lambda
error after its retry state has been committed. Both the generation DLQ and
maintenance DLQ alarm when non-empty, and maintenance Lambda errors have a
separate alarm.

SQS delivery is at-least-once. The worker therefore claims a job with a
conditional update and a delivery-specific lease token. Duplicate messages for
a complete/failed job, or a job with an active lease, are acknowledged without
rerunning generation. The worker has a five-minute Lambda timeout and uses a
ten-minute database lease. SQS visibility remains 30 minutes, so a terminated
worker's message becomes eligible for redelivery only after the lease has
expired. The fifth processing failure marks the job failed and allows the
message to move to the DLQ.

## Retention and cleanup

EventBridge Scheduler invokes the maintenance Lambda daily at **03:15
Australia/Sydney**. It deletes in batches of 500, up to 10 batches (5,000 rows)
per table per invocation.

| Data | Retention | Behaviour |
| --- | --- | --- |
| Successfully published outbox rows | 24 hours | Retains short-term delivery evidence without duplicating operational history indefinitely. |
| Complete and failed jobs and completed result JSON | 7 days after completion | Gives Android clients time to retrieve results while limiting storage of precise route and generated-script data. Request JSON is cleared as soon as a job becomes terminal. |
| Queued/running jobs | Not removed by retention cleanup | Prevents cleanup from discarding work that still needs recovery. |
| Unpublished outbox rows | Not removed by retention cleanup | They continue retrying with the capped backoff until the underlying problem is fixed. |

Apply `0002_generation_jobs.sql` before deploying API or worker code that uses
these tables. Migrations remain an explicit operator action and are not run at
Lambda cold start.
