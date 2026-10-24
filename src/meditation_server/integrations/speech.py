from __future__ import annotations

import asyncio
import os

import boto3
from botocore.config import Config


class SpeechDisabled(RuntimeError):
    pass


async def synthesize(text: str) -> bytes:
    provider = os.getenv("TTS_PROVIDER", "disabled").strip().casefold()
    if provider == "disabled":
        raise SpeechDisabled("Text-to-speech is disabled")
    if provider != "polly":
        raise RuntimeError(f"Unsupported text-to-speech provider: {provider}")
    client = boto3.client(
        "polly",
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        config=Config(connect_timeout=3, read_timeout=15, retries={"total_max_attempts": 3, "mode": "standard"}),
    )
    response = await asyncio.to_thread(
        client.synthesize_speech,
        Engine=os.getenv("TTS_ENGINE", "standard"),
        LanguageCode=os.getenv("TTS_LANGUAGE_CODE", "en-AU"),
        OutputFormat="mp3",
        Text=text,
        TextType="text",
        VoiceId=os.getenv("TTS_VOICE_ID", "Russell"),
    )
    stream = response["AudioStream"]
    try:
        audio = await asyncio.to_thread(stream.read)
    finally:
        stream.close()
    if not audio:
        raise RuntimeError("Speech provider returned no audio")
    return audio
