from __future__ import annotations

import asyncio
import io

import pytest

from meditation_server.integrations import speech


@pytest.fixture(autouse=True)
def clear_cached_polly_clients():
    speech._polly_client.cache_clear()
    yield
    speech._polly_client.cache_clear()


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
        "Text": "<speak>A quiet walk.</speak>",
        "TextType": "ssml",
        "VoiceId": "Russell",
    }
    assert client.stream is not None and client.stream.closed


def test_empty_polly_response_is_rejected(monkeypatch) -> None:
    client = RecordingPollyClient(audio=b"")
    monkeypatch.setenv("TTS_PROVIDER", "polly")
    monkeypatch.setattr(speech.boto3, "client", lambda *args, **kwargs: client)

    with pytest.raises(RuntimeError, match="no audio"):
        asyncio.run(speech.synthesize("A quiet walk."))


def test_pause_markers_become_fixed_ssml_breaks_and_text_is_escaped(monkeypatch) -> None:
    client = RecordingPollyClient()
    monkeypatch.setenv("TTS_PROVIDER", "polly")
    monkeypatch.setattr(speech.boto3, "client", lambda *args, **kwargs: client)

    asyncio.run(speech.synthesize("(pause)A & B <tag>(PAUSE)(pause)End.(pause)"))

    assert client.request is not None
    assert client.request["TextType"] == "ssml"
    assert client.request["Text"] == (
        '<speak><break time="2500ms"/>A &amp; B &lt;tag&gt;'
        '<break time="2500ms"/><break time="2500ms"/>End.'
        '<break time="2500ms"/></speak>'
    )


def test_pause_only_and_oversized_escaped_ssml_are_rejected_before_polly(monkeypatch) -> None:
    monkeypatch.setenv("TTS_PROVIDER", "polly")
    monkeypatch.setattr(
        speech.boto3,
        "client",
        lambda *args, **kwargs: pytest.fail("Invalid narration must not contact Polly"),
    )

    with pytest.raises(speech.InvalidSpeechText, match="spoken text"):
        asyncio.run(speech.synthesize("(pause)(PAUSE)"))
    with pytest.raises(speech.InvalidSpeechText, match="SSML limit"):
        asyncio.run(speech.synthesize("&" * 2000))


def test_reuses_polly_client_within_a_warm_lambda_environment(monkeypatch) -> None:
    client = RecordingPollyClient()
    creations = 0

    def create_client(*args, **kwargs):
        nonlocal creations
        creations += 1
        return client

    monkeypatch.setenv("TTS_PROVIDER", "polly")
    monkeypatch.setattr(speech.boto3, "client", create_client)

    asyncio.run(speech.synthesize("First."))
    asyncio.run(speech.synthesize("Second."))

    assert creations == 1
