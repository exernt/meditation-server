from __future__ import annotations

import asyncio
import io

import pytest

from meditation_server.integrations import speech


class RecordingPollyClient:
    def __init__(self, audio: bytes = b"ID3-test-audio") -> None:
        self.audio = audio
        self.request: dict[str, object] | None = None
        self.stream: io.BytesIO | None = None

    def synthesize_speech(self, **kwargs):
        self.request = kwargs
        self.stream = io.BytesIO(self.audio)
        return {"AudioStream": self.stream, "ContentType": "audio/mpeg"}


def test_disabled_speech_provider_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("TTS_PROVIDER", "disabled")

    with pytest.raises(speech.SpeechDisabled):
        asyncio.run(speech.synthesize("A quiet walk."))


def test_unknown_speech_provider_fails_without_falling_back_to_polly(monkeypatch) -> None:
    monkeypatch.setenv("TTS_PROVIDER", "another-provider")
    monkeypatch.setattr(
        speech.boto3,
        "client",
        lambda *args, **kwargs: pytest.fail("Polly must not be contacted"),
    )

    with pytest.raises(RuntimeError, match="Unsupported text-to-speech provider"):
        asyncio.run(speech.synthesize("A quiet walk."))


def test_polly_request_uses_configured_mp3_voice(monkeypatch) -> None:
    client = RecordingPollyClient()
    monkeypatch.setenv("TTS_PROVIDER", "polly")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("TTS_VOICE_ID", "Russell")
    monkeypatch.setenv("TTS_ENGINE", "standard")
    monkeypatch.setenv("TTS_LANGUAGE_CODE", "en-AU")
    monkeypatch.setattr(speech.boto3, "client", lambda *args, **kwargs: client)

    audio = asyncio.run(speech.synthesize("A quiet walk."))

    assert audio == b"ID3-test-audio"
    assert client.request == {
        "Engine": "standard",
        "LanguageCode": "en-AU",
        "OutputFormat": "mp3",
        "Text": "A quiet walk.",
        "TextType": "text",
        "VoiceId": "Russell",
    }
    assert client.stream is not None and client.stream.closed


def test_empty_polly_response_is_rejected(monkeypatch) -> None:
    client = RecordingPollyClient(audio=b"")
    monkeypatch.setenv("TTS_PROVIDER", "polly")
    monkeypatch.setattr(speech.boto3, "client", lambda *args, **kwargs: client)

    with pytest.raises(RuntimeError, match="no audio"):
        asyncio.run(speech.synthesize("A quiet walk."))
