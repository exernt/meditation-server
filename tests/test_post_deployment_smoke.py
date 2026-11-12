from __future__ import annotations

import json
from collections import deque
from urllib.parse import urlsplit

import pytest

from scripts.post_deployment_smoke import (
    SmokeClient,
    SmokeTestFailure,
    run_smoke_tests,
)


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.body


class FakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = deque(responses)
        self.requests = []
        self.timeouts: list[float] = []

    def __call__(self, request, *, timeout: float):
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self.responses.popleft()


def json_response(payload: dict[str, object], status: int = 200) -> FakeResponse:
    return FakeResponse(status, json.dumps(payload).encode("utf-8"))


def successful_responses(*, include_tts: bool = False) -> list[FakeResponse]:
    responses = [
        json_response({"status": "ready", "component": "api"}),
        json_response({"database": "ready"}),
        json_response(
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "token_type": "bearer",
            }
        ),
        json_response(
            {
                "user_id": "00000000-0000-0000-0000-000000000001",
                "email": "smoke@example.com",
                "role": "participant",
            }
        ),
        json_response(
            {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "token_type": "bearer",
            }
        ),
        json_response(
            {
                "user_id": "00000000-0000-0000-0000-000000000001",
                "email": "smoke@example.com",
                "role": "participant",
            }
        ),
    ]
    if include_tts:
        responses.append(FakeResponse(200, b"ID3-synthetic-audio", "audio/mpeg"))
    responses.append(json_response({"message": "Logged out"}))
    return responses


def test_smoke_suite_checks_auth_rotation_and_logout_without_exposing_credentials() -> None:
    opener = FakeOpener(successful_responses())
    client = SmokeClient(
        "https://example.execute-api.ap-southeast-2.amazonaws.com/dev/",
        opener=opener,
        sleeper=lambda _seconds: None,
    )

    run_smoke_tests(
        client,
        email="smoke@example.com",
        password="not-logged-password",
    )

    assert [urlsplit(request.full_url).path for request in opener.requests] == [
        "/dev/health",
        "/dev/health/database",
        "/dev/auth/login",
        "/dev/auth/me",
        "/dev/auth/refresh",
        "/dev/auth/me",
        "/dev/auth/logout",
    ]
    assert json.loads(opener.requests[-1].data) == {"refresh_token": "refresh-2"}
    assert opener.requests[3].get_header("Authorization") == "Bearer access-1"
    assert opener.requests[5].get_header("Authorization") == "Bearer access-2"
    assert opener.timeouts == [35.0] * 7


def test_smoke_suite_can_verify_non_empty_mpeg_tts() -> None:
    opener = FakeOpener(successful_responses(include_tts=True))
    client = SmokeClient("https://api.example.com/dev/", opener=opener)

    run_smoke_tests(
        client,
        email="smoke@example.com",
        password="password",
        tts_voice="female",
    )

    tts_request = opener.requests[-2]
    assert urlsplit(tts_request.full_url).path == "/dev/tts"
    assert json.loads(tts_request.data) == {
        "text": "This is a synthetic post-deployment smoke test.",
        "voice": "female",
    }


def test_smoke_suite_retries_readiness_and_requires_https() -> None:
    sleeps: list[float] = []
    opener = FakeOpener(
        [
            json_response({"detail": "warming"}, status=503),
            *successful_responses(),
        ]
    )
    client = SmokeClient(
        "https://api.example.com/dev/",
        opener=opener,
        sleeper=sleeps.append,
    )

    run_smoke_tests(client, email="smoke@example.com", password="password")

    assert sleeps == [1.0]
    with pytest.raises(SmokeTestFailure, match="absolute HTTPS URL"):
        SmokeClient("http://api.example.com/dev/")
