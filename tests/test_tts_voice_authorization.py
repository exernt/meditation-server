from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import HTTPException

from meditation_server.api import app as app_module
from meditation_server.api.dependencies import CurrentUser
from meditation_server.api.models import TtsRequest
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult


class VoiceExecutor(DatabaseExecutor):
    def __init__(self, voice: str | None) -> None:
        self.voice = voice

    async def execute(self, sql, parameters=None, *, transaction=None):
        if self.voice is None:
            return QueryResult()
        return QueryResult(rows=({
            "study_id": uuid4(),
            "study_status": "active",
            "study_group": "experimental",
            "script_generation_parameters": "",
            "tts_voice": self.voice,
        },))

    async def execute_batch(self, sql, parameter_sets, *, transaction=None):
        raise AssertionError("batch execution is not expected")

    async def begin_transaction(self):
        raise AssertionError("transactions are not expected")

    async def commit(self, transaction):
        raise AssertionError("transactions are not expected")

    async def rollback(self, transaction):
        raise AssertionError("transactions are not expected")


def participant() -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        email="participant@example.com",
        role="participant",
        session_version=0,
        email_verified=True,
    )


def test_non_study_user_cannot_request_female_voice(monkeypatch) -> None:
    async def unexpected_synthesis(*_args, **_kwargs):
        raise AssertionError("Polly must not be called")

    monkeypatch.setattr(app_module, "synthesize", unexpected_synthesis)

    try:
        asyncio.run(app_module.text_to_speech(
            TtsRequest(text="A quiet walk.", voice="female"),
            participant(),
            VoiceExecutor(None),
        ))
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("female voice should require an active study configuration")


def test_active_study_user_can_request_configured_female_voice(monkeypatch) -> None:
    calls = []

    async def synthesize(text: str, voice: str):
        calls.append((text, voice))
        return b"ID3-audio"

    monkeypatch.setattr(app_module, "synthesize", synthesize)

    response = asyncio.run(app_module.text_to_speech(
        TtsRequest(text="A quiet walk.", voice="female"),
        participant(),
        VoiceExecutor("female"),
    ))

    assert calls == [("A quiet walk.", "female")]
    assert response.body == b"ID3-audio"
