from __future__ import annotations

import re
from typing import Any

from meditation_server.api.models import LatLng, RouteStepInfo, ScriptWithParkRequest, ScriptWithoutParkRequest


def _route_label(instruction: str) -> str:
    match = re.search(r"(?:onto|on|along)\s+(.+?)(?:\s+toward|$)", instruction, re.IGNORECASE)
    return match.group(1).strip(" .") if match else "the path"


def _segment(step: RouteStepInfo, phase: str) -> dict[str, Any]:
    label = _route_label(step.instruction)
    return {
        "id": f"{phase}_{step.index}",
        "route_phase": phase,
        "index": step.index,
        "instruction": step.instruction,
        "route_label": label,
        "start": step.start.model_dump(),
        "end": step.end.model_dump(),
        "polyline": step.polyline,
        "duration_s": step.duration_s,
        "distance_m": step.distance_m,
        "elevation": {"classification": "unknown", "gain_m": 0.0, "start_m": 0.0, "end_m": 0.0},
        "nearby_features": [],
        "narration": f"Continue mindfully along {label}, noticing each steady step and returning gently to the breath.",
    }


def _script(
    *,
    source: LatLng,
    destination: LatLng,
    park: LatLng | None,
    duration_seconds: int,
    focused_steps: list[RouteStepInfo],
    focused_phase: str,
    closing_steps: list[RouteStepInfo],
    closing_phase: str,
    context: str,
) -> dict[str, Any]:
    duration_minutes = max(1, round(duration_seconds / 60))
    focused_words = round(duration_minutes * 150 * 0.2)
    compassion_words = round(duration_minutes * 150 * 0.4)
    closing_words = round(duration_minutes * 150 * 0.4)
    return {
        "duration_minutes": duration_minutes,
        "language": "en-AU",
        "includes_pauses": True,
        "prompt_version": "narration-2026-10-v3",
        "model": "fallback",
        "fallback": True,
        "inputs": {
            "context": context,
            "weather_condition": "Weather information unavailable",
            "source": source.model_dump(),
            "destination": destination.model_dump(),
            "park": park.model_dump() if park else None,
        },
        "sections": [
            {
                "type": "focused_attention",
                "minimum_words": focused_words,
                "opening_narration": {"id": "focused_opening", "text": "Begin by settling into a comfortable walking rhythm and noticing the natural movement of your breath. (pause)"},
                "route_segments": [_segment(step, focused_phase) for step in focused_steps],
                "closing_narration": {"id": "focused_closing", "text": "Let your attention widen while keeping a gentle awareness of each step."},
            },
            {
                "type": "compassion_meditation",
                "minimum_words": compassion_words,
                "narration": {"id": "compassion", "text": "Offer yourself a quiet wish for ease, safety, and wellbeing, then extend that same wish to the people around you. (pause)"},
            },
            {
                "type": "closing_meditation",
                "minimum_words": closing_words,
                "opening_narration": {"id": "closing_opening", "text": "As the walk continues, notice sounds, light, and movement without needing to hold on to them."},
                "route_segments": [_segment(step, closing_phase) for step in closing_steps],
                "closing_narration": {"id": "closing_end", "text": "Approaching the end of the walk, take one fuller breath and carry this steady awareness with you."},
            },
        ],
    }


def _response(script: dict[str, Any]) -> dict[str, Any]:
    usage = {
        "model": "fallback", "service_tier": "local", "input_tokens": 0,
        "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0,
        "reasoning_tokens": 0, "total_tokens": 0,
    }
    return {"script": script, "generation_cost_usd": 0.0, "usage": usage}


async def without_park(payload: ScriptWithoutParkRequest) -> dict[str, Any]:
    return _response(_script(
        source=payload.source,
        destination=payload.destination,
        park=None,
        duration_seconds=payload.total_walking_time,
        focused_steps=payload.route_steps,
        focused_phase="source_to_destination",
        closing_steps=[],
        closing_phase="park_to_destination",
        context=payload.context,
    ))


async def with_park(payload: ScriptWithParkRequest) -> dict[str, Any]:
    return _response(_script(
        source=payload.source,
        destination=payload.destination,
        park=payload.park,
        duration_seconds=payload.to_park_time + payload.park_time + payload.park_to_destination_time,
        focused_steps=payload.source_to_park_steps,
        focused_phase="source_to_park",
        closing_steps=payload.park_to_destination_steps,
        closing_phase="park_to_destination",
        context=payload.context,
    ))
