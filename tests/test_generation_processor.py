from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meditation_server.api.models import LatLng, RouteStepInfo, ScriptWithoutParkRequest
from meditation_server.integrations import generation_processor
from meditation_server.integrations.google_context import EnrichedSegment
from meditation_server.integrations.openai_generation import (
    GeneratedText,
    GenerationUsage,
    OpenAiGenerationError,
    _calculate_luna_standard_cost_usd,
    _generate_text_sync,
)


def _words(count: int) -> str:
    return " ".join(["calm"] * count)


def _payload() -> ScriptWithoutParkRequest:
    return ScriptWithoutParkRequest(
        source=LatLng(lat=-37.81, lon=144.96),
        destination=LatLng(lat=-37.80, lon=144.97),
        total_walking_time=300,
        context="settling after study",
        study_parameters="Use short sentences.",
        route_steps=[
            RouteStepInfo(
                index=1,
                instruction="Turn left onto Swanston Street",
                start=LatLng(lat=-37.81, lon=144.96),
                end=LatLng(lat=-37.80, lon=144.97),
                duration_s=60,
                distance_m=80,
                polyline="encoded",
            )
        ],
    )


def _segment() -> EnrichedSegment:
    step = _payload().route_steps[0]
    return EnrichedSegment(
        index=step.index,
        instruction=step.instruction,
        start=step.start,
        end=step.end,
        polyline=step.polyline,
        duration_s=step.duration_s,
        distance_m=step.distance_m,
        elevation_classification="high elevation (uphill)",
        elevation_gain_m=9.92,
        elevation_start_m=20.04,
        elevation_end_m=29.96,
    )


def _generated_text() -> GeneratedText:
    text = json.dumps(
        {
            "focused_attention": {
                "opening_narration": _words(50),
                "segment_narrations": {"source_to_destination_1": _words(50)},
                "closing_narration": _words(50),
            },
            "compassion_meditation": {"narration": _words(300)},
            "closing_meditation": {
                "opening_narration": _words(150),
                "segment_narrations": {},
                "closing_narration": _words(150),
            },
        }
    )
    usage = GenerationUsage(
        model="gpt-6-luna",
        service_tier="default",
        input_tokens=100,
        cached_input_tokens=10,
        cache_write_tokens=0,
        output_tokens=50,
        reasoning_tokens=0,
        total_tokens=150,
        cost_usd=0.000034,
    )
    return GeneratedText(text=text, usage=usage)


def test_worker_processor_enriches_and_minimises_openai_prompt(monkeypatch) -> None:
    monkeypatch.setattr(
        generation_processor, "weather_condition", AsyncMock(return_value="Clear, 20°C")
    )
    monkeypatch.setattr(
        generation_processor, "enrich_route", AsyncMock(return_value=[_segment()])
    )
    monkeypatch.setattr(
        generation_processor,
        "lookup_nearby_features",
        AsyncMock(return_value={"source_to_destination_1": ["Royal Park"]}),
    )
    generate = AsyncMock(return_value=_generated_text())
    monkeypatch.setattr(generation_processor, "generate_text", generate)

    result = asyncio.run(generation_processor.generate_without_park(_payload()))

    prompt = generate.await_args.args[0]
    assert "Royal Park" in prompt
    assert "Swanston Street" in prompt
    assert "Use short sentences." in prompt
    assert "-37.81" not in prompt
    assert "144.96" not in prompt
    assert "encoded" not in prompt
    assert generate.await_args.kwargs["schema_name"] == "meditation_narrations"
    assert result["generation_cost_usd"] == 0.000034
    assert result["usage"]["total_tokens"] == 150
    assert result["script"]["fallback"] is False
    segment = result["script"]["sections"][0]["route_segments"][0]
    assert segment["id"] == "source_to_destination_1"
    assert segment["elevation"]["gain_m"] == 9.9
    assert segment["nearby_features"] == ["Royal Park"]


def test_worker_processor_falls_back_when_openai_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("SCRIPT_FALLBACK_ENABLED", "true")
    monkeypatch.setattr(
        generation_processor, "weather_condition", AsyncMock(return_value="Unavailable")
    )
    monkeypatch.setattr(
        generation_processor, "enrich_route", AsyncMock(return_value=[_segment()])
    )
    monkeypatch.setattr(
        generation_processor, "lookup_nearby_features", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        generation_processor,
        "generate_text",
        AsyncMock(side_effect=OpenAiGenerationError("provider unavailable")),
    )

    result = asyncio.run(generation_processor.generate_without_park(_payload()))

    assert result["generation_cost_usd"] == 0.0
    assert result["usage"]["model"] == "fallback"
    assert result["script"]["fallback"] is True
    assert "Swanston Street" in result["script"]["sections"][0]["route_segments"][0]["narration"]


def test_worker_processor_can_fail_closed_when_fallback_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("SCRIPT_FALLBACK_ENABLED", "false")
    monkeypatch.setattr(
        generation_processor, "weather_condition", AsyncMock(return_value="Unavailable")
    )
    monkeypatch.setattr(
        generation_processor, "enrich_route", AsyncMock(return_value=[_segment()])
    )
    monkeypatch.setattr(
        generation_processor, "lookup_nearby_features", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        generation_processor,
        "generate_text",
        AsyncMock(side_effect=OpenAiGenerationError("provider unavailable")),
    )

    with pytest.raises(OpenAiGenerationError):
        asyncio.run(generation_processor.generate_without_park(_payload()))


def test_luna_standard_cost_accounts_for_cached_input() -> None:
    assert _calculate_luna_standard_cost_usd(
        input_tokens=1_000_000,
        cached_input_tokens=100_000,
        cache_write_tokens=0,
        output_tokens=100_000,
    ) == 0.257


def test_openai_client_sends_strict_responses_schema(monkeypatch) -> None:
    import openai

    requests: list[dict] = []
    response = SimpleNamespace(
        model="gpt-6-luna",
        service_tier="default",
        output_text='{"ok":true}',
        usage=SimpleNamespace(
            input_tokens=100,
            input_tokens_details=SimpleNamespace(
                cached_tokens=10, cache_write_tokens=0
            ),
            output_tokens=20,
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            total_tokens=120,
        ),
    )

    class FakeOpenAi:
        def __init__(self, **_kwargs):
            self.responses = SimpleNamespace(create=self.create)

        def create(self, **request):
            requests.append(request)
            return response

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAi)
    result = _generate_text_sync(
        "prompt",
        json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        schema_name="meditation_narrations",
        api_key="test-key",
    )

    assert result.text == '{"ok":true}'
    request = requests[0]
    assert request["model"] == "gpt-6-luna"
    assert request["reasoning"] == {"effort": "none"}
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
