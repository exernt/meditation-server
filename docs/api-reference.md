# Walking Meditation server API reference

Last reviewed against this repository: 11 November 2026

This is the authoritative inventory of the HTTP endpoints exposed by the
FastAPI application. It describes inputs, authorization, behavior, successful
outputs, and the important error cases. The generated OpenAPI document is useful
for exploration, but this file also records behaviors that response inference
cannot express.

## 1. Protocol and authentication

- API Gateway base URLs include the deployed stage path and end in `/`.
- JSON is the default request and response format. UUIDs and timestamps are JSON
  strings; timestamps are UTC.
- Protected routes require `Authorization: Bearer <access_token>`.
- Successful status is 200 unless an endpoint is marked 201, 202, or 303.
- Validation errors use 422 with `{"detail":[...]}`. Ordinary application errors
  use `{"detail":"message"}`. Failed asynchronous generation results use a
  structured detail with `message` and `error_code`.
- Protected routes may return 401 for an invalid, expired, or revoked session;
  403 for insufficient role/ownership; 404 when a resource is absent or hidden;
  409 for a state conflict; 429 for throttling; and 503 when a required service
  is disabled or unavailable.
- Authentication, study, questionnaire, export, feedback, route, TTS, and `/v1`
  responses carry `Cache-Control: no-store`.
- `/docs`, `/redoc`, and `/openapi.json` are currently public. Decide whether to
  retain them before production launch.

Application roles are `participant`, `researcher`, and `admin`. “Verified
researcher” below means a researcher whose `researcher_profiles.verified_at` is
set. “Owner” means the verified researcher linked to the study.

## 2. Common request and response shapes

`LatLng`:

```json
{"lat": -37.8136, "lon": 144.9631}
```

Latitude is -90..90 and longitude is -180..180.

`RouteStep`:

```json
{
  "index": 1,
  "instruction": "Walk east",
  "start": {"lat": -37.8136, "lon": 144.9631},
  "end": {"lat": -37.8128, "lon": 144.9650},
  "duration_s": 120,
  "distance_m": 180,
  "polyline": "encoded-polyline"
}
```

`Study` is
`{id,name,duration,description,status,control_script_generation_parameters,experimental_script_generation_parameters,control_tts_voice,experimental_tts_voice}`.
Status is `draft`, `recruiting`, `active`, or `closed`; voices are `male` or
`female`.

`Questionnaire` is
`{id,title,researcher_id,status,definition,created_on}`. `Issuance` is
`{id,study_id,questionnaire_id,round_number,title,issued_at,revoked_at,has_responded}`.

`GenerationJob` is
`{job_id,kind,status,audio_status,attempt_count,created_at,updated_at,completed_at,error_code,status_url,result_url}`.

`ResponseExport` is
`{id,scope,study_id,status,filename,created_at,generated_at,expires_at,cached}`.

## 3. Public, readiness, and link endpoints

| Method and path | Input and behavior | Success output |
| --- | --- | --- |
| `GET /` | None. Liveness only; it does not check dependencies. | `{"Heartbeat":"Hello World"}` |
| `GET /health` | None. API readiness only. | `{"status":"ready","component":"api"}` |
| `GET /health/database` | Runs `SELECT 1`. | `{"database":"ready"}`; 503 if the database check fails |
| `GET /.well-known/assetlinks.json` | None. Reads configured Android package and certificate fingerprints. | Android App Links statement; 404 when fingerprints are not configured |
| `GET /auth/email-verification/open` | Browser page; token stays in the URL fragment and JavaScript submits it to `confirm`. | No-store verification HTML |
| `GET /auth/password-reset/open` | Browser page; preserves the token fragment while opening `walkingmeditation://password-reset`. | No-store HTML |
| `GET /study/join?id=<id>` | `id` is 1..128 characters. | HTML redirect/deep link to `walkingmeditation://study/join?id=...` |
| `GET /openapi.json` | None. | Generated OpenAPI 3.1 JSON |
| `GET /docs` | None. | Swagger UI |
| `GET /docs/oauth2-redirect` | Swagger helper. | Redirect page |
| `GET /redoc` | None. | ReDoc UI |

## 4. Authentication, profiles, and administration

### Registration and email verification

`POST /auth/register` is public and returns 201.

```json
{
  "email": "person@example.com",
  "password": "at-least-8-characters",
  "gender": "optional",
  "age": 18,
  "researcher_institution": "optional",
  "agrees_to_privacy_policy": true,
  "privacy_policy_version": 1
}
```

Password length is 8..1024, optional age is 18..120, and optional text is
trimmed. Supplying `researcher_institution` creates a pending researcher account;
otherwise the initial role is participant. A successful call stores the policy
acceptance time/version, creates a verification token, attempts email delivery,
and returns
`{"message":"Registration successful; verify your email before signing in"}`.
False agreement returns 400, a stale policy version or duplicate email returns
409, and missing fields return 422. Registration does not create a session.

| Method and path | Input/access | Behavior and output |
| --- | --- | --- |
| `GET /auth/email-verification/open` | Public browser page | Submits a fragment token without placing it in a query string |
| `POST /auth/email-verification/confirm` | Public `{"token":"1..128 chars"}` | `{"message":"Email verified"}`; 400 invalid, used, or expired |
| `POST /auth/email-verification/resend` | Public `{"email":"..."}` | Always generic success; 60-second cooldown; replaces unused tokens |

The generic resend message is
`{"message":"If that account needs verification, a new link has been sent."}`.
Unverified users are eligible for scheduled cleanup after seven days.

### Sessions and recovery

| Method and path | Input/access | Behavior and output |
| --- | --- | --- |
| `POST /auth/login` | Public `{"email","password"}` | `{access_token,refresh_token,token_type:"bearer"}`; new login replaces the previous active session; 401 invalid/disabled, 403 unverified, 429 throttled |
| `POST /auth/refresh` | Public `{"refresh_token"}` | Rotates both tokens; replay or invalid/expired/revoked token returns 401 and replay revokes the token family |
| `POST /auth/logout` | Public `{"refresh_token"}` | `{"message":"Logged out"}`; idempotently revokes the family |
| `POST /auth/logout-all` | Bearer | Increments the session version, revokes refresh tokens, returns `{"message":"All devices have been logged out"}` |
| `POST /auth/password-reset/request` | Public `{"email"}` | Always `{"message":"If that email is registered, a reset link has been sent."}`; 60-second cooldown |
| `POST /auth/password-reset/confirm` | Public `{"token":"1..128","new_password":"8..1024"}` | `{"message":"Password has been reset"}` and invalidates sessions; 400 invalid/used/expired |

Login throttling uses pseudonymous account and source-IP subjects. Progressive
blocks begin after repeated failures; a throttled response includes
`Retry-After`. Do not infer account existence from a recovery response.

### Profile and role operations

| Method and path | Input/access | Behavior and output |
| --- | --- | --- |
| `GET /auth/me` | Bearer | `{user_id,email,role,age,gender,institution,verified_at}` |
| `PUT /auth/me` | Bearer `{"age":18..120,"gender":"1..100 chars"}` | `{age,gender}` |
| `DELETE /auth/me` | Bearer | Deletes the account and sessions; returns `{"message":"Account deleted"}`; retained research responses lose the direct user reference |
| `POST /auth/register-researcher` | Bearer with verified email; `{"institution":null|string}` | 201 `{institution,verified_at:null,role:"researcher"}`; awaits admin approval |
| `GET /auth/admin/researchers/pending` | Admin bearer | `{researchers:[{user_id,email,institution,requested_at}]}`, oldest first |
| `POST /auth/admin/researchers/{user_id}/verify` | Admin bearer | `{institution,verified_at,role:"researcher"}` |
| `POST /auth/admin/researchers/{user_id}/reject` | Admin bearer | Restores participant role, revokes sessions, records a security event, returns `{"message":"Researcher request rejected"}` |
| `POST /auth/admin/users/{user_id}/disable` | Admin bearer | Disables account, revokes sessions, returns `{"message":"Account disabled"}` |

There is no administrator-promotion endpoint. Promotion is the controlled
database procedure in the operations handbook.

## 5. Studies and consent

All study endpoints require bearer authentication. Creation and owner actions
require an approved researcher.

| Method and path | Input/access | Behavior and output |
| --- | --- | --- |
| `POST /studies` | Verified researcher; `{"name":"1..255","duration":positive integer}` | 201 `Study`; creates placeholder consent form version 1 |
| `GET /studies` | Bearer; optional `status=draft|recruiting|active|closed`; optional `owner=me` for researchers | `{studies:[Study]}` |
| `GET /studies/mine` | Bearer | `{studies:[{study_id,name,duration,description,status,activated_at,study_group,registered_at,consented_at,withdrawn_at,open_questionnaires:[...]}]}` |
| `GET /studies/current-script-parameters` | Bearer | `{participating,study_id,study_status,study_group,script_generation_parameters,tts_voice}`; safe empty defaults when not active |
| `GET /studies/{study_id}` | Bearer | `Study`; 404 missing |
| `GET /studies/{study_id}/consent-form` | Bearer | `{study_id,body_text,version}` |
| `POST /studies/{study_id}/consent-form/edit` | Owner while draft; `{"body_text":"1..20000"}` | Updated form; changed text increments version, identical text does not; 409 after publication |
| `POST /studies/{study_id}/join` | Bearer; `{"agrees_to_consent":true,"consent_version":positive integer or omitted}` | 201 registration; records current consent version and balances group assignment; 400 refusal; 409 stale version, invalid state, or active membership conflict |
| `POST /studies/{study_id}/leave` | Participant | Registration with `withdrawn_at`; 404 never joined, 409 already left |
| `POST /studies/{study_id}/getstatus` | Owner | `{name,duration,groups:{control:[{user_id,gender,age}],experimental:[...]}}` |
| `POST /studies/{study_id}/swapgroup` | Owner; `{"user_id":"uuid","target_group":"control|experimental"}` | `{study_id,user_id,group}` |
| `POST /studies/{study_id}/edit` | Owner; `{"column":"...","value":...}` | Updated `Study` |
| `POST /studies/{study_id}/publish` | Owner; draft only | `Study` in `recruiting`; 409 while consent remains placeholder |
| `POST /studies/{study_id}/activate` | Owner; recruiting only | `Study` in `active`; records activation time |
| `POST /studies/{study_id}/close` | Owner; recruiting or active | `Study` in `closed` |
| `GET /studies/{study_id}/feedback` | Owner | `{feedback:[{id,user_id,study_id,category,rating,comment,created_at}]}` |
| `POST /studies/{study_id}/response-export` | Owner | 202 export described in section 9 |

Editable columns are `name`, `duration`, `description`,
`control_script_generation_parameters`,
`experimental_script_generation_parameters`, `control_tts_voice`, and
`experimental_tts_voice`. Parameter and description values are at most 10,000
characters; voices are `male` or `female`.

Maintained clients must read the consent form and send the version displayed.
Omission exists only for backward compatibility. A policy text change requires
coordinated updates to `CURRENT_PRIVACY_POLICY_VERSION` in the server and
`PrivacyPolicyActivity.POLICY_VERSION` in Android.

## 6. Questionnaires and responses

A questionnaire body is `{"definition":{...}}`. The definition requires a
non-empty `title` and `questions`. Each question has a unique non-empty `id`, a
non-empty `prompt`, optional boolean `required`, and type `text`,
`single_choice`, `multiple_choice`, or `rating`. Choice questions require
options; answer selections use `{value,detail?}`. Ratings use an integer min/max
or integer-valued options. Unknown fields are rejected where the request model
forbids extras.

| Method and path | Input/access | Behavior and output |
| --- | --- | --- |
| `POST /questionnaires` | Verified researcher; `{"definition":...}` | 201 owned draft `Questionnaire` |
| `GET /questionnaires` | Verified researcher | `{questionnaires:[Questionnaire]}` owned by caller |
| `GET /questionnaires/{questionnaire_id}` | Owning verified researcher | `Questionnaire`; 404 hides non-owned items |
| `PUT /questionnaires/{questionnaire_id}` | Owning verified researcher; replacement definition | Replaced `Questionnaire`; 409 unless draft |
| `POST /studies/{study_id}/questionnaires` | Study owner; `{"questionnaire_id":"uuid"}` | 201 `Issuance`; study must be active; first issue publishes a draft; round increments |
| `GET /studies/{study_id}/questionnaires` | Active participant or owner | `{issued_questionnaires:[Issuance]}`; participants see only open/published rounds |
| `GET /studies/{study_id}/questionnaires/{study_questionnaire_id}` | Active participant or owner | `Issuance` plus `definition`; closed/revoked rounds are hidden from participants |
| `POST /studies/{study_id}/questionnaires/{study_questionnaire_id}/revoke` | Owner | `Issuance` with `revoked_at`; 409 if already revoked |
| `POST /studies/{study_id}/questionnaires/{study_questionnaire_id}/responses` | Active participant; `{"answers":{"question_id":answer}}` | 201 `{id,study_questionnaire_id,respondent_id,answers,submitted_on}`; one response per participant per round |
| `GET /studies/{study_id}/questionnaires/{study_questionnaire_id}/responses` | Owner | `{responses:[...]}` |

## 7. Feedback

| Method and path | Input/access | Behavior and output |
| --- | --- | --- |
| `POST /feedback` | Bearer; `{"study_id":uuid|null,"category":"general|study|technical","rating":1..5|null,"comment":string|null}` | 201 feedback row. Rating or comment is required; a study-linked submission requires active participation. |
| `GET /feedback` | Admin or verified researcher | `{feedback:[...]}` for general feedback only, newest first |

## 8. Route and TTS

| Method and path | Input/access | Behavior and output |
| --- | --- | --- |
| `POST /route/generate-path` | Bearer; `{source:LatLng,destination:LatLng,total_travel_time:0..14400}` | `{origin,destination,total_travel_time,baseline:{duration_s,distance_m,polyline,steps},parks:[{park,baseline_s,detour_s,to_park_s,park_to_destination_s,added_s,slack_s,polyline_to_park,polyline_from_park,steps_to_park,steps_from_park}]}`; 502 provider failure |
| `POST /tts` | Bearer; `{"text":"1..3000","voice":"male|female"}` | MP3 bytes, `Content-Type: audio/mpeg`, no-store, inline `narration.mp3`; 409 active-study voice mismatch, 422 invalid speech/SSML, 503 disabled, 502 provider failure |

Route output contains `origin`, `destination`, `total_travel_time`, a `baseline`
with duration/distance/polyline/steps, and `parks` containing detour timings,
slack, polylines, and step lists.

TTS maps `male` to Russell and `female` to Nicole in the current deployment,
uses Australian English standard-engine Polly, converts `(pause)` tokens to
2.5-second SSML breaks, and never stores generated audio server-side. Callers
without an active study resolve to the default male voice.

## 9. Asynchronous generation jobs

`POST /v1/generation-jobs` requires bearer authentication and an
`Idempotency-Key` header of 16..128 ASCII letters, digits, `.`, `_`, `:`, or `-`.
A UUID is recommended.

```json
{
  "kind": "park",
  "payload": {"...": "the matching park generation payload below"}
}
```

`kind` is `park` or `without_park`; the nested payload is validated against the
corresponding generation request model.

Park generation payload:

```json
{
  "source": {"lat": -37.8136, "lon": 144.9631},
  "destination": {"lat": -37.8128, "lon": 144.9650},
  "park": {"lat": -37.8130, "lon": 144.9640},
  "to_park_time": 180,
  "park_time": 300,
  "park_to_destination_time": 180,
  "context": "A quiet walk",
  "study_parameters": "optional, max 10000 characters",
  "source_to_park_steps": [],
  "park_to_destination_steps": []
}
```

No-park generation payload:

```json
{
  "source": {"lat": -37.8136, "lon": 144.9631},
  "destination": {"lat": -37.8128, "lon": 144.9650},
  "total_walking_time": 300,
  "context": "A quiet walk",
  "study_parameters": "",
  "route_steps": []
}
```

| Method and path | Behavior and output |
| --- | --- |
| `POST /v1/generation-jobs` | 202 `GenerationJob` plus `created:boolean`. Same key and body returns the original; same key with another body returns 409. More than two active jobs or twenty rolling-24-hour submissions returns 429. |
| `GET /v1/generation-jobs/{job_id}` | Owning bearer receives `GenerationJob`; 404 hides absent/non-owned jobs. |
| `GET /v1/generation-jobs/{job_id}/result` | Queued/running returns 202 `{job_id,status}` and `Retry-After: 2`; complete returns `{job_id,...result}`; failed returns 409 with stable error information. |

Poll initially every two seconds. Jobs are queued transactionally through an
outbox and may be delivered more than once internally; the worker is idempotent.

## 10. Response exports

| Method and path | Input/access | Behavior and output |
| --- | --- | --- |
| `POST /response-exports` | Verified researcher | 202 `ResponseExport` with `scope:"all_studies"` and `study_id:null` |
| `POST /studies/{study_id}/response-export` | Study owner | 202 `ResponseExport` with `scope:"study"` |
| `GET /response-exports/{export_id}` | Owning verified researcher | `ResponseExport`; 404 absent/non-owned |
| `GET /response-exports/{export_id}/download` | Public unguessable UUID link | No-store HTML email/password form |
| `POST /response-exports/{export_id}/download` | `application/x-www-form-urlencoded` fields `email` and `password`, body at most 16 KiB | 303 to a fresh five-minute S3 URL after verified-researcher, ownership, completion, and expiry checks; generic 401 HTML on failure; 429 throttle preserved |

An existing queued/running export or an unchanged completed export may be
returned with `cached:true`. The UTF-8 CSV has one row per answer and one row per
multiple-choice selection. Columns are:

`study_id`, `study_name`, `respondent_code`, `response_id`,
`questionnaire_id`, `questionnaire_title`, `questionnaire_round`,
`questionnaire_issuance_id`, `questionnaire_issued_at`,
`response_submitted_at`, `question_id`, `question_prompt`, `question_type`,
`answer_value`, `answer_detail`, and `selection_index`.

Exports never contain participant email or UUID. Formula-leading cell text is
prefixed with an apostrophe. Export jobs and objects expire after seven days;
successful login does not create an API session or affect mobile sessions.

## 11. Client behavior summary

- Treat 401 as a single refresh opportunity, then require sign-in.
- Honor `Retry-After` on 202 and 429 responses.
- On privacy-policy or consent-version 409, fetch and display the current text
  before asking for agreement again.
- Do not retry non-idempotent requests blindly. Use the generation idempotency
  key exactly as documented.
- Do not log request bodies, bearer/refresh/reset/verification tokens, precise
  coordinates, questionnaire answers, feedback, prompts, generated scripts, or
  export credentials.
- Treat 502 generation/route/TTS failures as dependency failures and 503 TTS as
  an intentional disabled-service state.

