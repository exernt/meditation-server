# Personal and sensitive data inventory

This inventory supports the client’s privacy notice and terms drafting. It is a
technical inventory, not legal advice; the client and university ethics/privacy
owners must decide the lawful basis, wording, retention, and participant rights.

## Data controlled by the application

| Category | Fields/examples | Purpose | Primary location |
| --- | --- | --- | --- |
| Account identity | Email address, internal user UUID, role, researcher institution, researcher approval status | Authentication and role-based access | Aurora |
| Authentication security | Argon2 password hash, hashed refresh/reset/verification tokens, session version, failed-login counters, pseudonymous IP/email throttle hashes, security-event timestamps | Sign-in, recovery, abuse prevention, incident investigation | Aurora; short-lived app storage |
| Demographics | Age and self-described gender | Study demographics and analysis | Aurora |
| Study membership | Study ID, participant/researcher IDs, group assignment, registration, consent and withdrawal timestamps | Enrolment and study administration | Aurora |
| Consent | Consent text/version and acceptance timestamp | Ethics and consent evidence | Aurora |
| Questionnaire data | Questionnaire definitions, issued rounds, answers and submission timestamps | Research data collection | Aurora |
| Feedback | Rating/comment and associated user/study context where present | Product/research evaluation | Aurora |
| Location and route data | Origin/destination coordinates, park waypoint, route polylines, step coordinates/instructions, distance, duration, elevation and progress while walking | Route planning and location-triggered meditation | Device memory/cache; transient processing; active Aurora generation job; generated route/script response retained for up to seven days |
| Meditation inputs | Free-text context, researcher script parameters, route labels, terrain, weather, nearby landmarks, duration and word budgets | Generate personalised narration | Aurora study configuration; active generation job request cleared when the job becomes terminal |
| Generated content | Narration text, prompt/model/version metadata, generated audio cached on device | Deliver the meditation | Aurora/API generation result for up to seven days; app-private device files |
| Operational metadata | Request ID, status code, route template, duration, error category, queue message/job IDs | Reliability and security monitoring | CloudWatch/CloudTrail |

Passwords, raw access/refresh/reset/verification tokens, request bodies,
authorization headers, precise coordinates, questionnaire answers, and free-text
participant disclosures must not be written to application or API access logs.

## Third-party disclosures

| Recipient | Data sent | Explicit exclusions | Reason |
| --- | --- | --- | --- |
| Google Routes | Origin/destination and waypoint coordinates, travel mode and route constraints | Email, user UUID, tokens, questionnaire answers | Calculate walking routes |
| Google Places | Search coordinates/radius and requested place categories | Account identity and study membership | Find suitable parks/nearby features |
| Google Elevation | Route points/polyline coordinates | Account identity | Classify route elevation |
| Google Weather | Location used for current-weather lookup | Account identity | Add weather context |
| OpenAI | Meditation context, route labels/instructions, terrain, nearby features, weather, duration/word targets, approved study generation parameters | Email, user UUID, auth tokens, raw questionnaire answers and exact coordinates unless a reviewed feature explicitly requires them | Generate narration text |
| Amazon Polly | Narration text only; initial configuration is `en-AU`, standard engine, `Russell` | Coordinates, email, user UUID, auth tokens, study membership, questionnaire data | Generate MP3 narration |
| Amazon SES | Recipient email, sender, password-reset or email-verification link and minimal transactional wording | Demographics, location, questionnaire answers | Account verification and recovery |
| AWS Aurora/Secrets Manager | Application records and encrypted secrets described above | Raw passwords and raw long-lived tokens | Primary storage and secret custody |
| AWS CloudWatch/CloudTrail/WAF | Sanitised operational/security metadata; AWS control-plane activity; IP-based security telemetry at the edge | Request bodies, tokens, disclosures and exact route payloads | Operations, audit and abuse prevention |

## Device-held data

- Access and refresh tokens are encrypted using Android Keystore/AES-GCM.
- Generated scripts, route plans, coordinates, and Polly MP3 files are stored in
  app-private files only and are excluded from Android cloud backup and device
  transfer.
- Sign-out/account deletion must remove tokens and generated route/script/audio
  files.
- Account deletion removes the account and related session records. Submitted
  questionnaire answers are retained for study integrity with their respondent
  reference set to `NULL`.

## Decisions required before production

- Client-approved privacy notice/terms and ethics review.
- Named data controller, support/privacy contact, and processor list.
- Retention periods for accounts, withdrawn participants, questionnaire
  responses, consent evidence, security events, logs, and backups.
- Whether free-text context may contain health information and whether it may be
  disclosed to OpenAI or Polly.
- Participant access, correction, export, withdrawal, and deletion procedures.
- Data-location and cross-border disclosure assessment for every third party.

