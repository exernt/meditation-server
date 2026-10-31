from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 35.0
RETRYABLE_STATUSES = {502, 503, 504}


class SmokeTestFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Any
    body: bytes


OpenUrl = Callable[..., Any]


class SmokeClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: OpenUrl = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized = base_url.strip().rstrip("/") + "/"
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise SmokeTestFailure("SMOKE_BASE_URL must be an absolute HTTPS URL")
        if timeout_seconds <= 0:
            raise SmokeTestFailure("SMOKE_TIMEOUT_SECONDS must be greater than zero")
        self.base_url = normalized
        self.timeout_seconds = timeout_seconds
        self._opener = opener
        self._sleeper = sleeper

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        access_token: str | None = None,
        attempts: int = 1,
    ) -> HttpResponse:
        if attempts < 1:
            raise ValueError("attempts must be at least one")
        url = urljoin(self.base_url, path.lstrip("/"))
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json, audio/mpeg",
            "User-Agent": "walking-meditation-post-deployment-smoke/1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        for attempt in range(1, attempts + 1):
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    result = HttpResponse(
                        status=response.status,
                        headers=response.headers,
                        body=response.read(),
                    )
            except HTTPError as exc:
                result = HttpResponse(
                    status=exc.code,
                    headers=exc.headers,
                    body=exc.read(),
                )
            except URLError as exc:
                if attempt < attempts:
                    self._sleeper(float(attempt))
                    continue
                raise SmokeTestFailure(
                    f"{method} {path} could not reach the deployed API: {exc.reason}"
                ) from exc

            if result.status in RETRYABLE_STATUSES and attempt < attempts:
                self._sleeper(float(attempt))
                continue
            return result
        raise AssertionError("request loop exhausted without returning")

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        access_token: str | None = None,
        attempts: int = 1,
        expected_status: int = 200,
    ) -> dict[str, Any]:
        response = self.request(
            method,
            path,
            payload=payload,
            access_token=access_token,
            attempts=attempts,
        )
        if response.status != expected_status:
            detail = _safe_error_detail(response.body)
            raise SmokeTestFailure(
                f"{method} {path} returned HTTP {response.status}; expected "
                f"{expected_status}{detail}"
            )
        try:
            decoded = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeTestFailure(f"{method} {path} did not return valid JSON") from exc
        if not isinstance(decoded, dict):
            raise SmokeTestFailure(f"{method} {path} did not return a JSON object")
        return decoded


def _safe_error_detail(body: bytes) -> str:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(decoded, dict):
        return ""
    detail = decoded.get("detail")
    return f": {detail}" if isinstance(detail, str) else ""


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SmokeTestFailure(f"{name} was missing from a successful response")
    return value


def run_smoke_tests(
    client: SmokeClient,
    *,
    email: str,
    password: str,
    tts_voice: str | None = None,
) -> None:
    if not email or not password:
        raise SmokeTestFailure(
            "SMOKE_TEST_EMAIL and SMOKE_TEST_PASSWORD must be configured as "
            "GitHub environment secrets"
        )
    if tts_voice not in {None, "male", "female"}:
        raise SmokeTestFailure("SMOKE_TEST_TTS_VOICE must be male, female, or unset")

    print("[smoke] API health")
    health = client.json("GET", "/health", attempts=5)
    if health != {"status": "ready", "component": "api"}:
        raise SmokeTestFailure("GET /health returned an unexpected readiness payload")

    print("[smoke] Database health")
    database = client.json("GET", "/health/database", attempts=5)
    if database != {"database": "ready"}:
        raise SmokeTestFailure("GET /health/database returned an unexpected payload")

    refresh_token: str | None = None
    primary_failure: Exception | None = None
    try:
        print("[smoke] Account login")
        login = client.json(
            "POST",
            "/auth/login",
            payload={"email": email, "password": password},
        )
        access_token = _required_string(login.get("access_token"), "access_token")
        refresh_token = _required_string(login.get("refresh_token"), "refresh_token")
        if login.get("token_type") != "bearer":
            raise SmokeTestFailure("POST /auth/login returned an unexpected token type")

        print("[smoke] Authenticated identity")
        profile = client.json("GET", "/auth/me", access_token=access_token)
        profile_email = _required_string(profile.get("email"), "email")
        if profile_email.casefold() != email.casefold():
            raise SmokeTestFailure("GET /auth/me returned the wrong smoke account")
        _required_string(profile.get("user_id"), "user_id")
        _required_string(profile.get("role"), "role")

        print("[smoke] Deterministic script generation")
        generated = client.json(
            "POST",
            "/route/generate-script/without-park",
            payload={
                "source": {"lat": -37.8136, "lon": 144.9631},
                "destination": {"lat": -37.8128, "lon": 144.9650},
                "total_walking_time": 300,
                "context": "Synthetic post-deployment smoke test",
                "route_steps": [],
            },
            access_token=access_token,
        )
        script = generated.get("script")
        if not isinstance(script, dict) or script.get("fallback") is not True:
            raise SmokeTestFailure(
                "Script generation did not return the deterministic fallback response"
            )

        print("[smoke] Refresh-token rotation")
        refreshed = client.json(
            "POST",
            "/auth/refresh",
            payload={"refresh_token": refresh_token},
        )
        access_token = _required_string(refreshed.get("access_token"), "access_token")
        refresh_token = _required_string(refreshed.get("refresh_token"), "refresh_token")
        client.json("GET", "/auth/me", access_token=access_token)

        if tts_voice is not None:
            print("[smoke] Polly text-to-speech")
            audio = client.request(
                "POST",
                "/tts",
                payload={
                    "text": "This is a synthetic post-deployment smoke test.",
                    "voice": tts_voice,
                },
                access_token=access_token,
            )
            content_type = str(audio.headers.get("Content-Type", "")).split(";", 1)[0]
            if audio.status != 200:
                detail = _safe_error_detail(audio.body)
                raise SmokeTestFailure(
                    f"POST /tts returned HTTP {audio.status}; expected 200{detail}"
                )
            if content_type.casefold() != "audio/mpeg" or not audio.body:
                raise SmokeTestFailure("POST /tts did not return non-empty MP3 audio")
    except Exception as exc:
        primary_failure = exc
        raise
    finally:
        if refresh_token is not None:
            print("[smoke] Logout cleanup")
            try:
                client.json(
                    "POST",
                    "/auth/logout",
                    payload={"refresh_token": refresh_token},
                )
            except Exception:
                if primary_failure is None:
                    raise
                print("[smoke] Logout cleanup also failed", file=sys.stderr)


def main() -> int:
    try:
        timeout_seconds = float(
            os.getenv("SMOKE_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        )
        voice = os.getenv("SMOKE_TEST_TTS_VOICE", "").strip().casefold() or None
        client = SmokeClient(
            os.getenv("SMOKE_BASE_URL", ""),
            timeout_seconds=timeout_seconds,
        )
        run_smoke_tests(
            client,
            email=os.getenv("SMOKE_TEST_EMAIL", "").strip(),
            password=os.getenv("SMOKE_TEST_PASSWORD", ""),
            tts_voice=voice,
        )
    except (SmokeTestFailure, ValueError) as exc:
        print(f"Smoke tests failed: {exc}", file=sys.stderr)
        return 1
    print("Post-deployment smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
