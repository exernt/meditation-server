# TTS implementation and provider testing

Last reviewed: 2026-10-24

## Current implementation

The public contract is intentionally provider-neutral:

```text
Android app
  -> authenticated POST /tts {"text": "..."}
  -> API Gateway (binary media type: audio/mpeg)
  -> FastAPI Lambda
  -> Amazon Polly SynthesizeSpeech
  -> MP3 bytes returned as audio/mpeg
  -> Android app-private cache and MediaPlayer
```

`POST /tts` requires a valid access token. The request accepts only `text`, from
1 to 3,000 characters. The successful response is MP3 data with
`Content-Type: audio/mpeg`, `Cache-Control: no-store`, and an inline
`narration.mp3` filename. A disabled provider returns `503`; a provider or
transport failure returns `502`.

The Lambda calls Polly synchronously from an async worker thread. It uses a
3-second connection timeout, a 15-second read timeout, and at most three
standard retry attempts. Its Polly client is cached for reuse by warm Lambda
execution environments. The initial synthesis settings are:

| Setting | Current value |
| --- | --- |
| Provider | Amazon Polly |
| Region | Lambda/AWS Region (`ap-southeast-2`) |
| Voice | `Russell` |
| Language | `en-AU` |
| Engine | `standard` |
| Public input | Provider-neutral text with optional `(pause)` markers |
| Polly input | Escaped SSML; each marker becomes a 2.5-second break |
| Output | MP3 |

The API role receives `polly:SynthesizeSpeech` only when CDK context
`ttsProvider=polly`; the permission is constrained to the configured AWS
Region. Polly does not support resource-level permissions for this action, so
the IAM resource is `*`. No generated audio is stored on the server or in S3.

The Android `TtsClient` splits narration into chunks of no more than 2,800
Unicode code points. Requests use the authenticated HTTP client, including one
token refresh after a `401`. Successful chunks are written atomically to the
app-private cache and their paths are stored as an ordered list under the
original route-anchored narration. `MediaPlayer` advances through every part
before the scheduler advances to the next narration. A failed preparation is
not published as a ready walk and can be retried without downloading completed
chunks again.

The client preserves `(pause)` tokens exactly. The server detects them
case-insensitively, XML-escapes every ordinary text segment, inserts only its
own `<break time="2500ms"/>` elements, and wraps the result in `<speak>`. A
pause-only request or text that expands beyond Polly's 6,000-character SSML
limit returns `422` before Polly is contacted.

## What is and is not configurable today

CDK accepts only:

```text
ttsProvider=polly
ttsProvider=disabled
```

`ttsProvider` is supplied by the deployment workflow's `TTS_PROVIDER`
environment variable. `polly` is the current default if the context value is
omitted. The voice, engine, and language are Lambda environment variables, but
their deployed values are currently fixed in `infra/stacks/api_stack.py`.

This means the `/tts` API boundary is provider-neutral, while the server-side
implementation is currently a Polly/disabled switch rather than a general
runtime plug-in registry.

## Test another Polly voice or engine

1. Confirm the proposed voice supports the selected language and engine in the
   deployment Region.
2. Change `TTS_VOICE_ID`, `TTS_ENGINE`, and, if needed,
   `TTS_LANGUAGE_CODE` in `infra/stacks/api_stack.py`.
3. Update the speech unit test to assert the exact request sent to the provider.
4. Run the server tests and synthesize the API stack.
5. Review the CloudFormation diff, then deploy `WmApi-dev` through the protected
   development workflow using synthetic narration only.
6. Clear the current Android walk before listening (or deliberately bump the
   mobile TTS contract ID). Cache keys cover endpoint, contract, chunking, and
   text, but a server-only voice change at the same URL cannot change a local
   key. Signing out or clearing app storage is also sufficient.
7. Compare the same short and long scripts for pronunciation, pacing, latency,
   failure behavior, and cost. Confirm a request over 3,000 characters is
   rejected and that disabling TTS fails preparation cleanly without a request
   loop.

## Test a different TTS provider

Keep the Android and API contract unchanged so the mobile app does not need a
provider-specific build. The replacement adapter must accept plain narration
text and return non-empty MP3 bytes.

1. Add a provider adapter under `src/meditation_server/integrations/`. Give it
   bounded timeouts and retries, and translate provider failures into the
   existing `/tts` `502` response without logging narration or credentials.
2. Change `src/meditation_server/integrations/speech.py` to select the adapter
   explicitly. An unknown provider must fail closed; it must never silently use
   Polly.
3. Extend the `ttsProvider` allowlist in `infra/config.py` and add configuration
   tests for the new value.
4. Add only the required SDK dependency. Store API keys in Secrets Manager,
   grant the API role access to the exact secret ARN, and keep secret values out
   of Lambda environment variables, source, logs, and GitHub.
5. Remove the Polly IAM permission when the selected provider is not Polly. If
   the new provider needs outbound internet access, retain the current
   non-VPC Lambda model or explicitly account for VPC egress cost and design.
6. Add adapter tests for the outbound request, MP3 response, empty response,
   timeout, authentication failure, and disabled mode. Keep the endpoint tests
   for authentication, 3,000-character validation, content type, cache headers,
   `502`, and `503` behavior.
7. Run tests, dependency/security checks, CDK synthesis, and a reviewed
   `WmApi-dev` deployment. Use synthetic narration and verify logs contain no
   narration text or provider credentials.
8. Clear the Android TTS cache, then run the same listening comparison used for
   Polly. Roll back by redeploying the prior provider, or set
   `ttsProvider=disabled` to stop synthesis while investigating.

Before sending narration to a new vendor, document its processing Region,
retention/training policy, data-processing terms, quotas, and pricing, and have
the project privacy/ethics owner approve the disclosure. Only narration text
should leave the application.

## Verification still required

The implementation and mocked/local contract tests are in place. A live Polly
request, Android device playback, disabled-mode walk, provider-failure walk,
load test, and production deployment are still release-gate work. The deployed
synthetic infrastructure baseline predates the migrated FastAPI application, so
it must not be treated as end-to-end application verification.

The accepted Android implementation sends only the supported
`{"text": "..."}` body. Provider-specific Qwen and Polly payload builders have
been removed from the mobile client; the request model continues to forbid
extra fields with `422`.
