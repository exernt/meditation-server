from __future__ import annotations

import asyncio
import os
import re
from functools import lru_cache
from xml.sax.saxutils import escape

import boto3
from botocore.config import Config


class SpeechDisabled(RuntimeError):
    pass


class InvalidSpeechText(ValueError):
    pass


_PAUSE = re.compile(r"\(pause\)", re.IGNORECASE)
_BREAK = '<break time="2500ms"/>'
_MAX_POLLY_SSML_CHARACTERS = 6000
_POLLY_VOICE_IDS = {"male": "Russell", "female": "Nicole"}


def _as_ssml(text: str) -> str:
    if not _PAUSE.sub("", text).strip():
        raise InvalidSpeechText("Narration must contain spoken text")
    ssml = f"<speak>{_BREAK.join(escape(part) for part in _PAUSE.split(text))}</speak>"
    if len(ssml) > _MAX_POLLY_SSML_CHARACTERS:
        raise InvalidSpeechText("Narration expands beyond the Polly SSML limit")
    return ssml


@lru_cache(maxsize=4)
def _polly_client(region: str | None):
    # Lambda execution environments are reused, so retain the SDK client and its connection pool.
    return boto3.client(
        "polly",
        region_name=region,
        config=Config(
            connect_timeout=3,
            read_timeout=15,
            retries={"total_max_attempts": 3, "mode": "standard"},
        ),
    )


async def synthesize(text: str, voice: str = "male") -> bytes:
    provider = os.getenv("TTS_PROVIDER", "disabled").strip().casefold()
    if provider == "disabled":
        raise SpeechDisabled("Text-to-speech is disabled")
    if provider != "polly":
        raise RuntimeError(f"Unsupported text-to-speech provider: {provider}")
    if voice not in _POLLY_VOICE_IDS:
        raise ValueError(f"Unsupported text-to-speech voice: {voice}")
    ssml = _as_ssml(text)
    client = _polly_client(os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"))
    response = await asyncio.to_thread(
        client.synthesize_speech,
        Engine=os.getenv("TTS_ENGINE", "standard"),
        LanguageCode=os.getenv("TTS_LANGUAGE_CODE", "en-AU"),
        OutputFormat="mp3",
        Text=ssml,
        TextType="ssml",
        VoiceId=(
            os.getenv("TTS_MALE_VOICE_ID", os.getenv("TTS_VOICE_ID", "Russell"))
            if voice == "male"
            else os.getenv("TTS_FEMALE_VOICE_ID", "Nicole")
        ),
    )
    stream = response["AudioStream"]
    try:
        audio = await asyncio.to_thread(stream.read)
    finally:
        stream.close()
    if not audio:
        raise RuntimeError("Speech provider returned no audio")
    return audio
