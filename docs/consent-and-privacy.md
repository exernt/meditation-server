# Privacy policy acceptance and study consent forms

## Privacy policy acceptance

`POST /auth/register` requires `agrees_to_privacy_policy: true` and
`privacy_policy_version`, the version of the policy the user was shown.

- Without agreement the request is rejected with `400`.
- A version that is no longer current is rejected with `409`, so the user reviews
  the new text first.
- The server stores when, and to which version, the user agreed
  (`users.privacy_policy_accepted_at` and `users.privacy_policy_version`).
  Accounts created before this was tracked have neither value.

When the policy text changes, bump `CURRENT_PRIVACY_POLICY_VERSION` in
`src/meditation_server/api/auth.py` and `PrivacyPolicyActivity.POLICY_VERSION` in
the Android app together. Nothing enforces that they match; if only one is
bumped, registration fails with the `409` above.

## Study consent forms

Every study starts with a placeholder consent form. The owning researcher writes
the real one with:

`POST /studies/{study_id}/consent-form/edit`

```json
{ "body_text": "You agree to walk for 10 minutes a day for 14 days." }
```

- The text is trimmed and must be 1 to 20,000 characters.
- Only the study's owner can edit, and only while the study is a draft; once it
  is published the endpoint returns `409`. The rule is the
  `CONSENT_EDITABLE_STATUSES` set in `src/meditation_server/api/studies.py`.
- Each change to the text increases the form's `version` by one. Saving
  identical text leaves it alone.
- A study cannot be published while its consent form is still the placeholder
  text (`409`).

## Recording what each participant agreed to

`POST /studies/{study_id}/join` stores the consent form version on the
registration (`study_registrations.consent_version`). The request may include
`consent_version`, the version the participant was shown. If the form has
changed since then, the join is rejected with `409` so they review the new text
first. Older clients that omit it are recorded against the current version.
