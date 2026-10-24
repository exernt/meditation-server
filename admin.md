# Application administrator procedures

This runbook covers Walking Meditation **application** administrators stored in
the `users` table with `role = 'admin'`. It does not cover AWS IAM
administrators or the local AWS CLI profile named `wm-admin`.

There is currently no public registration endpoint for an administrator and no
administrator-promotion API. `POST /auth/register` creates only participant or
researcher accounts. An authorised operator must therefore promote an existing,
verified account through PostgreSQL. Do not insert password hashes or choose a
password on another person's behalf.

## Approve a new administrator account

### 1. Register and verify the account

The prospective administrator registers through the ordinary application flow
using their own email address and password. They must not supply a researcher
institution. They then follow the emailed verification link.

Before continuing, confirm that:

- the email address belongs to the intended named administrator;
- the account's email has been verified;
- the account is not disabled; and
- the change has been authorised according to the project's access-control
  process.

### 2. Configure the Data API connection

In PowerShell, set the same non-secret identifiers used by the migration runner:

```powershell
$env:DATABASE_RESOURCE_ARN = "arn:aws:rds:ap-southeast-2:ACCOUNT_ID:cluster:CLUSTER_ID"
$env:DATABASE_SECRET_ARN = "arn:aws:secretsmanager:ap-southeast-2:ACCOUNT_ID:secret:SECRET_ID"
$env:DATABASE_NAME = "walking_meditation"
```

Authenticate the authorised operator if necessary:

```powershell
aws login --profile wm-admin --region ap-southeast-2
aws sts get-caller-identity --profile wm-admin
```

Confirm that the returned AWS account and principal are the intended ones before
making the change. Never print or retrieve the database secret value.

### 3. Promote the verified account

Set the target email address, then execute the parameterised statement below.
The email is normalised in the same way as application registration.

```powershell
$targetEmail = "admin@example.com".Trim().ToLowerInvariant()

$sql = @'
WITH promoted AS (
    UPDATE users
       SET role = 'admin'::user_role,
           session_version = session_version + 1
     WHERE email = :email
       AND email_verified = true
       AND disabled_at IS NULL
       AND role <> 'admin'::user_role
     RETURNING id, email
),
revoked AS (
    UPDATE refresh_tokens
       SET revoked_at = now()
     WHERE user_id IN (SELECT id FROM promoted)
       AND revoked_at IS NULL
)
INSERT INTO security_events (user_id, event_type)
SELECT id, 'admin_role_granted'
FROM promoted
RETURNING user_id, event_type;
'@

aws rds-data execute-statement `
  --resource-arn $env:DATABASE_RESOURCE_ARN `
  --secret-arn $env:DATABASE_SECRET_ARN `
  --database $env:DATABASE_NAME `
  --sql $sql `
  --parameters "name=email,value={stringValue=$targetEmail}" `
  --include-result-metadata `
  --profile wm-admin `
  --region ap-southeast-2
```

A successful result contains the user's UUID and the event type
`admin_role_granted`. No returned record means that the account was not found,
was unverified, was disabled, or was already an administrator. Do not weaken the
eligibility checks to force a promotion; investigate the account instead.

The statement also increments `session_version` and revokes active refresh
tokens. This invalidates existing sessions so that the administrator must sign
in again and receive the shorter-lived administrator access token.

### 4. Verify the result

```powershell
$verifySql = @'
SELECT id, email, role::text AS role, email_verified, disabled_at, session_version
  FROM users
 WHERE email = :email;
'@

aws rds-data execute-statement `
  --resource-arn $env:DATABASE_RESOURCE_ARN `
  --secret-arn $env:DATABASE_SECRET_ARN `
  --database $env:DATABASE_NAME `
  --sql $verifySql `
  --parameters "name=email,value={stringValue=$targetEmail}" `
  --include-result-metadata `
  --profile wm-admin `
  --region ap-southeast-2
```

Confirm that the result has `role = admin`, `email_verified = true`, and a null
`disabled_at`. The new administrator should then sign in through the ordinary
login screen and confirm that the Admin Dashboard control appears.

## Existing administrator operations

An authenticated application administrator can currently:

- list pending researcher requests with
  `GET /auth/admin/researchers/pending`;
- approve a researcher with
  `POST /auth/admin/researchers/{user_id}/verify`;
- reject a pending researcher request with
  `POST /auth/admin/researchers/{user_id}/reject`;
- review general application feedback with `GET /feedback`; and
- disable a user with `POST /auth/admin/users/{user_id}/disable`.

The Android Admin Dashboard exposes the pending-request list, approval, and
rejection, plus a general application-feedback button. User disabling is
currently an API operation only and has no Android control.

## Review researcher requests in the Android app

1. Sign in through the ordinary login screen using an account whose application
   role is `admin`.
2. On the main screen, select **Admin Dashboard**.
3. Review the pending cards. Each card shows the applicant's email address,
   optional institution, and request date.
4. Select **Approve** to verify the researcher, or **Reject** and confirm the
   dialog to remove the request.

The dashboard obtains the cards from
`GET /auth/admin/researchers/pending`, ordered from oldest to newest. Approval
sets `researcher_profiles.verified_at` and permits the account to use
researcher-only endpoints.

Rejection applies only while the request is pending. It deletes the unverified
researcher profile, changes the account role back to `participant`, increments
the account's session version, revokes active refresh tokens, and records a
`researcher_rejected` security event. The account itself is retained, and the
user can sign in again as a participant or submit a new researcher request.

All researcher-review endpoints require an authenticated `admin` role. A
non-admin receives `403 Forbidden`; approving a missing profile or rejecting a
request that is missing or already verified returns `404 Not Found`.

The dashboard also contains a link to the general application-feedback list.
`GET /feedback` returns all feedback not tied to a study, newest first. It is
available to administrators and verified researchers. The dashboard does not
currently provide user search, user disabling, role management, security-event
review, or administrator management.

The user-disable operation invalidates the target user's access and refresh
sessions.

## Current limitations

- Administrator promotion is a controlled database operation rather than an API
  operation. It should be replaced by a dedicated, audited operator command
  before production use.
- The security event records the affected user's UUID and event type, but the
  current schema does not record the acting administrator or change-ticket ID.
- User disabling and other account-administration operations do not currently
  have Android interfaces.
- Application MFA is not implemented. AWS operators using the `wm-admin` profile
  are separate identities and should use MFA according to the AWS account
  access policy.

After completing database administration, end the AWS CLI session:

```powershell
aws logout --profile wm-admin
```
