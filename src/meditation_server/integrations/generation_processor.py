from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Callable
from typing import Any, Sequence

from meditation_server import settings
from meditation_server.api.models import ScriptWithParkRequest, ScriptWithoutParkRequest
from meditation_server.integrations.google_context import (
    EnrichedSegment,
    enrich_route,
    lookup_nearby_features,
    weather_condition,
)
from meditation_server.integrations.openai_generation import (
    GeneratedText,
    GenerationUsage,
    OpenAiGenerationError,
    generate_text,
    zero_usage,
)


logger = logging.getLogger(__name__)

TARGET_WORDS_PER_MINUTE = 150
WORD_COUNT_TOLERANCE = 0.2
PROMPT_VERSION = "narration-2026-10-v3"
FOCUSED_ATTENTION_SHARE = 0.2
COMPASSION_SHARE = 0.4
CLOSING_SHARE = 0.4
NON_EMPTY_TEXT_PATTERN = r"\S"


def _words_for_seconds(seconds: float) -> int:
    return int(round((seconds / 60) * TARGET_WORDS_PER_MINUTE))


def _word_range(target_words: int) -> tuple[int, int]:
    return (
        max(0, math.floor(target_words * (1 - WORD_COUNT_TOLERANCE))),
        max(0, math.ceil(target_words * (1 + WORD_COUNT_TOLERANCE))),
    )


def _route_label(instruction: str) -> str:
    first_line = next(
        (line.strip() for line in instruction.splitlines() if line.strip()), ""
    )
    match = re.search(
        r"(?:onto|on|along)\s+(.+?)(?:\s+toward|$)", first_line, re.IGNORECASE
    )
    return match.group(1).strip(" .") if match else first_line or "the path"


def _segment_id(route_phase: str, segment: EnrichedSegment) -> str:
    return f"{route_phase}_{segment.index}"


def _route_context(
    segments: Sequence[EnrichedSegment],
    route_phase: str,
    nearby: dict[str, list[str]],
) -> str:
    values: list[dict[str, Any]] = []
    for segment in segments:
        target = _words_for_seconds(segment.duration_s)
        minimum, maximum = _word_range(target)
        segment_id = _segment_id(route_phase, segment)
        value: dict[str, Any] = {
            "segment_id": segment_id,
            "route_label": _route_label(segment.instruction),
            "instruction": segment.instruction,
            "terrain": segment.elevation_classification,
            "estimated_duration_seconds": segment.duration_s,
            "target_words": target,
            "minimum_words": minimum,
            "maximum_words": maximum,
        }
        if nearby.get(segment_id):
            value["nearby_features"] = nearby[segment_id]
        values.append(value)
    return json.dumps(values, ensure_ascii=False, indent=2)


def _segment_narrations_schema(segment_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            segment_id: {
                "type": "string",
                "pattern": NON_EMPTY_TEXT_PATTERN,
                "description": f"Narration for route segment {segment_id}.",
            }
            for segment_id in segment_ids
        },
        "required": list(segment_ids),
        "additionalProperties": False,
    }


def _walking_section_schema(segment_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "opening_narration": {
                "type": "string",
                "pattern": NON_EMPTY_TEXT_PATTERN,
            },
            "segment_narrations": _segment_narrations_schema(segment_ids),
            "closing_narration": {
                "type": "string",
                "pattern": NON_EMPTY_TEXT_PATTERN,
            },
        },
        "required": [
            "opening_narration",
            "segment_narrations",
            "closing_narration",
        ],
        "additionalProperties": False,
    }


def _narration_schema(
    focused_segment_ids: Sequence[str], closing_segment_ids: Sequence[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "focused_attention": _walking_section_schema(focused_segment_ids),
            "compassion_meditation": {
                "type": "object",
                "properties": {
                    "narration": {
                        "type": "string",
                        "pattern": NON_EMPTY_TEXT_PATTERN,
                    }
                },
                "required": ["narration"],
                "additionalProperties": False,
            },
            "closing_meditation": _walking_section_schema(closing_segment_ids),
        },
        "required": [
            "focused_attention",
            "compassion_meditation",
            "closing_meditation",
        ],
        "additionalProperties": False,
    }


def _parse_narrations(
    generated_text: str,
    focused_segment_ids: Sequence[str],
    closing_segment_ids: Sequence[str],
) -> dict[str, Any]:
    try:
        value = json.loads(generated_text)
    except json.JSONDecodeError as exc:
        raise ValueError("Generated narration is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Generated narration must be a JSON object")

    expected_ids = {
        "focused_attention": set(focused_segment_ids),
        "closing_meditation": set(closing_segment_ids),
    }
    try:
        narration_values = [
            value["focused_attention"]["opening_narration"],
            value["focused_attention"]["closing_narration"],
            value["compassion_meditation"]["narration"],
            value["closing_meditation"]["opening_narration"],
            value["closing_meditation"]["closing_narration"],
        ]
        for section_name, required_ids in expected_ids.items():
            actual = value[section_name]["segment_narrations"]
            if not isinstance(actual, dict) or set(actual) != required_ids:
                raise ValueError(
                    f"Generated {section_name} segment IDs do not match the route"
                )
            narration_values.extend(actual.values())
    except (KeyError, TypeError) as exc:
        raise ValueError("Generated narration has an invalid structure") from exc
    if any(not isinstance(text, str) or not text.strip() for text in narration_values):
        raise ValueError("Generated narration contains an empty text block")
    return value


def _word_count(text: str) -> int:
    without_pauses = re.sub(r"\(\s*pause\s*\)", " ", text, flags=re.IGNORECASE)
    return len(without_pauses.split())


def _word_count_violations(
    narrations: dict[str, Any],
    focused_segments: Sequence[EnrichedSegment],
    focused_phase: str,
    compassion_words: int,
    closing_segments: Sequence[EnrichedSegment],
    closing_phase: str,
    standalone_closing_words: int,
) -> list[str]:
    problems: list[str] = []

    def check(name: str, words: int, target: int) -> None:
        minimum, maximum = _word_range(target)
        if words < minimum or words > maximum:
            problems.append(f"{name}: {words} spoken words, expected {minimum}-{maximum}")

    def check_walking(
        section_name: str,
        segments: Sequence[EnrichedSegment],
        route_phase: str,
    ) -> None:
        if not segments:
            return
        section = narrations[section_name]
        last_index = len(segments) - 1
        for index, segment in enumerate(segments):
            segment_id = _segment_id(route_phase, segment)
            words = _word_count(section["segment_narrations"][segment_id])
            if index == 0:
                words += _word_count(section["opening_narration"])
            if index == last_index:
                words += _word_count(section["closing_narration"])
            check(f"{section_name}.{segment_id}", words, _words_for_seconds(segment.duration_s))

    check_walking("focused_attention", focused_segments, focused_phase)
    check_walking("closing_meditation", closing_segments, closing_phase)
    check(
        "compassion_meditation",
        _word_count(narrations["compassion_meditation"]["narration"]),
        compassion_words,
    )
    if not closing_segments:
        closing = narrations["closing_meditation"]
        check(
            "closing_meditation",
            _word_count(closing["opening_narration"])
            + _word_count(closing["closing_narration"]),
            standalone_closing_words,
        )
    return problems


def _fallback_segment_text(segment: EnrichedSegment) -> str:
    label = segment.elevation_classification.casefold()
    if "uphill" in label:
        terrain = "The path rises here, so let your pace ease and your breathing stay natural."
    elif "downhill" in label:
        terrain = "The path slopes down here, so keep your steps light and unhurried."
    else:
        terrain = "The path is mostly flat here, so settle into an even, easy rhythm."
    return (
        f"Continue along {_route_label(segment.instruction)}. {terrain} "
        "Feel your feet meet the ground, one step and then the next. (pause) "
        "Stay aware of the people and crossings around you, and return your "
        "attention to the rhythm of walking."
    )


def _fallback_narrations(
    focused_segments: Sequence[EnrichedSegment],
    focused_phase: str,
    closing_segments: Sequence[EnrichedSegment],
    closing_phase: str,
    *,
    has_park: bool,
) -> dict[str, Any]:
    if has_park:
        compassion = (
            "You have arrived at the park. Pause here for a moment and let your body "
            "settle. (pause) Silently wish yourself well: may I be calm, may I be "
            "safe. (pause) Now bring to mind someone you care about and wish them the "
            "same. (pause) Let that kindness extend to the people around you. (pause) "
            "When you are ready, begin walking again."
        )
        closing_opening = (
            "Begin walking again, carrying a little of that kindness with you. Keep "
            "an easy pace and stay aware of your surroundings. (pause)"
        )
    else:
        compassion = (
            "As you keep walking, silently wish yourself well: may I be calm, may I "
            "be safe. (pause) Now bring to mind someone you care about and wish them "
            "the same. (pause) Let that kindness extend to the people around you. (pause)"
        )
        closing_opening = (
            "Let your walking stay steady and unhurried, carrying a little of that "
            "kindness with you. (pause)"
        )
    return {
        "focused_attention": {
            "opening_narration": (
                "Welcome to this walking meditation. Choose a pace that feels "
                "comfortable and safe, and keep your eyes softly forward. (pause)"
            ),
            "segment_narrations": {
                _segment_id(focused_phase, segment): _fallback_segment_text(segment)
                for segment in focused_segments
            },
            "closing_narration": (
                "Let your attention rest on the feeling of walking for a few more steps. (pause)"
            ),
        },
        "compassion_meditation": {"narration": compassion},
        "closing_meditation": {
            "opening_narration": closing_opening,
            "segment_narrations": {
                _segment_id(closing_phase, segment): _fallback_segment_text(segment)
                for segment in closing_segments
            },
            "closing_narration": (
                "As you arrive, take one slow breath and notice how your body feels. "
                "Thank yourself for taking this time. (pause)"
            ),
        },
    }


async def _generate_narrations(
    prompt: str,
    focused_segments: Sequence[EnrichedSegment],
    focused_phase: str,
    compassion_words: int,
    closing_segments: Sequence[EnrichedSegment],
    closing_phase: str,
    standalone_closing_words: int,
    *,
    has_park: bool,
    raw_output_observer: Callable[[GeneratedText], None] | None = None,
) -> tuple[dict[str, Any], GenerationUsage, bool]:
    focused_ids = [_segment_id(focused_phase, segment) for segment in focused_segments]
    closing_ids = [_segment_id(closing_phase, segment) for segment in closing_segments]
    try:
        generated = await generate_text(
            prompt,
            json_schema=_narration_schema(focused_ids, closing_ids),
            schema_name="meditation_narrations",
        )
        if raw_output_observer is not None:
            raw_output_observer(generated)
    except OpenAiGenerationError:
        if not settings.script_fallback_enabled():
            raise
        logger.warning("Using deterministic generation fallback", exc_info=True)
        return (
            _fallback_narrations(
                focused_segments,
                focused_phase,
                closing_segments,
                closing_phase,
                has_park=has_park,
            ),
            zero_usage(),
            True,
        )

    try:
        narrations = _parse_narrations(generated.text, focused_ids, closing_ids)
        problems = _word_count_violations(
            narrations,
            focused_segments,
            focused_phase,
            compassion_words,
            closing_segments,
            closing_phase,
            standalone_closing_words,
        )
        if problems:
            for problem in problems:
                logger.warning("Generated narration outside word target: %s", problem)
            if not settings.script_fallback_enabled():
                raise OpenAiGenerationError("Generated narration violated word targets")
            return (
                _fallback_narrations(
                    focused_segments,
                    focused_phase,
                    closing_segments,
                    closing_phase,
                    has_park=has_park,
                ),
                generated.usage,
                True,
            )
        return narrations, generated.usage, False
    except ValueError:
        if not settings.script_fallback_enabled():
            raise
        logger.warning("Using deterministic generation fallback", exc_info=True)
        return (
            _fallback_narrations(
                focused_segments,
                focused_phase,
                closing_segments,
                closing_phase,
                has_park=has_park,
            ),
            generated.usage,
            True,
        )


def _load_prompt(name: str) -> str:
    path = settings.prompt_root() / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to load generation prompt {name}") from exc


def _render_prompt(name: str, **values: object) -> str:
    try:
        return _load_prompt(name).format(**values)
    except KeyError as exc:
        raise RuntimeError(f"Generation prompt is missing value {exc}") from exc


def _script_response(
    *,
    duration_minutes: int,
    context: str,
    weather: str,
    source: dict[str, float],
    destination: dict[str, float],
    park: dict[str, float] | None,
    focused_words: int,
    compassion_words: int,
    closing_words: int,
    focused_segments: Sequence[EnrichedSegment],
    focused_phase: str,
    closing_segments: Sequence[EnrichedSegment],
    closing_phase: str,
    nearby: dict[str, list[str]],
    narrations: dict[str, Any],
    usage: GenerationUsage,
    fallback: bool,
) -> dict[str, Any]:
    def route_segments(
        segments: Sequence[EnrichedSegment], route_phase: str, section: str
    ) -> list[dict[str, Any]]:
        result = []
        for segment in segments:
            segment_id = _segment_id(route_phase, segment)
            result.append(
                {
                    "id": segment_id,
                    "route_phase": route_phase,
                    "index": segment.index,
                    "instruction": segment.instruction,
                    "route_label": _route_label(segment.instruction),
                    "start": segment.start.model_dump(),
                    "end": segment.end.model_dump(),
                    "polyline": segment.polyline,
                    "duration_s": segment.duration_s,
                    "distance_m": segment.distance_m,
                    "elevation": {
                        "classification": segment.elevation_classification,
                        "gain_m": round(segment.elevation_gain_m, 1),
                        "start_m": round(segment.elevation_start_m, 1),
                        "end_m": round(segment.elevation_end_m, 1),
                    },
                    "nearby_features": nearby.get(segment_id, []),
                    "narration": narrations[section]["segment_narrations"][segment_id].strip(),
                }
            )
        return result

    focused = narrations["focused_attention"]
    compassion = narrations["compassion_meditation"]
    closing = narrations["closing_meditation"]
    script = {
        "duration_minutes": duration_minutes,
        "language": "en-AU",
        "includes_pauses": True,
        "prompt_version": PROMPT_VERSION,
        "model": usage.model,
        "fallback": fallback,
        "inputs": {
            "context": context,
            "weather_condition": weather,
            "source": source,
            "destination": destination,
            "park": park,
        },
        "sections": [
            {
                "type": "focused_attention",
                "minimum_words": focused_words,
                "opening_narration": {"id": "focused_opening", "text": focused["opening_narration"].strip()},
                "route_segments": route_segments(
                    focused_segments, focused_phase, "focused_attention"
                ),
                "closing_narration": {"id": "focused_closing", "text": focused["closing_narration"].strip()},
            },
            {
                "type": "compassion_meditation",
                "minimum_words": compassion_words,
                "narration": {"id": "compassion", "text": compassion["narration"].strip()},
            },
            {
                "type": "closing_meditation",
                "minimum_words": closing_words,
                "opening_narration": {"id": "closing_opening", "text": closing["opening_narration"].strip()},
                "route_segments": route_segments(
                    closing_segments, closing_phase, "closing_meditation"
                ),
                "closing_narration": {"id": "closing_end", "text": closing["closing_narration"].strip()},
            },
        ],
    }
    return {
        "script": script,
        "generation_cost_usd": usage.cost_usd,
        "usage": usage.response_dict(),
    }


async def generate_without_park(
    payload: ScriptWithoutParkRequest,
    *,
    raw_output_observer: Callable[[GeneratedText], None] | None = None,
) -> dict[str, Any]:
    weather, focused_segments = await asyncio.gather(
        weather_condition(payload.destination), enrich_route(payload.route_steps)
    )
    focused_phase = "source_to_destination"
    closing_phase = "park_to_destination"
    nearby = await lookup_nearby_features(focused_segments, focused_phase)
    focused_words = _words_for_seconds(
        payload.total_walking_time * FOCUSED_ATTENTION_SHARE
    )
    compassion_words = _words_for_seconds(payload.total_walking_time * COMPASSION_SHARE)
    closing_words = _words_for_seconds(payload.total_walking_time * CLOSING_SHARE)
    compassion_min, compassion_max = _word_range(compassion_words)
    closing_min, closing_max = _word_range(closing_words)
    prompt = _render_prompt(
        "without_park_narration.txt",
        context=payload.context,
        duration_minutes=max(1, round(payload.total_walking_time / 60)),
        FAM_word_count=focused_words,
        source_to_park=_route_context(focused_segments, focused_phase, nearby),
        CM_word_count=compassion_words,
        CM_min_words=compassion_min,
        CM_max_words=compassion_max,
        Closing_word_count=closing_words,
        Closing_min_words=closing_min,
        Closing_max_words=closing_max,
        weatherCondition=weather,
        study_parameters=payload.study_parameters.strip()
        or "No additional study instructions.",
    )
    narrations, usage, fallback = await _generate_narrations(
        prompt,
        focused_segments,
        focused_phase,
        compassion_words,
        [],
        closing_phase,
        closing_words,
        has_park=False,
        raw_output_observer=raw_output_observer,
    )
    return _script_response(
        duration_minutes=max(1, round(payload.total_walking_time / 60)),
        context=payload.context,
        weather=weather,
        source=payload.source.model_dump(),
        destination=payload.destination.model_dump(),
        park=None,
        focused_words=focused_words,
        compassion_words=compassion_words,
        closing_words=closing_words,
        focused_segments=focused_segments,
        focused_phase=focused_phase,
        closing_segments=[],
        closing_phase=closing_phase,
        nearby=nearby,
        narrations=narrations,
        usage=usage,
        fallback=fallback,
    )


async def generate_with_park(
    payload: ScriptWithParkRequest,
    *,
    raw_output_observer: Callable[[GeneratedText], None] | None = None,
) -> dict[str, Any]:
    weather, focused_segments, closing_segments = await asyncio.gather(
        weather_condition(payload.park),
        enrich_route(payload.source_to_park_steps),
        enrich_route(payload.park_to_destination_steps),
    )
    focused_phase = "source_to_park"
    closing_phase = "park_to_destination"
    focused_nearby, closing_nearby = await asyncio.gather(
        lookup_nearby_features(focused_segments, focused_phase),
        lookup_nearby_features(closing_segments, closing_phase),
    )
    nearby = {**focused_nearby, **closing_nearby}
    focused_words = _words_for_seconds(payload.to_park_time)
    compassion_words = _words_for_seconds(payload.park_time)
    closing_words = _words_for_seconds(payload.park_to_destination_time)
    compassion_min, compassion_max = _word_range(compassion_words)
    duration_seconds = (
        payload.to_park_time + payload.park_time + payload.park_to_destination_time
    )
    prompt = _render_prompt(
        "with_park_narration.txt",
        context=payload.context,
        duration_minutes=max(1, round(duration_seconds / 60)),
        FAM_word_count=focused_words,
        source_to_park=_route_context(focused_segments, focused_phase, nearby),
        park_to_destination=_route_context(closing_segments, closing_phase, nearby),
        CM_word_count=compassion_words,
        CM_min_words=compassion_min,
        CM_max_words=compassion_max,
        Closing_word_count=closing_words,
        weatherCondition=weather,
        study_parameters=payload.study_parameters.strip()
        or "No additional study instructions.",
    )
    narrations, usage, fallback = await _generate_narrations(
        prompt,
        focused_segments,
        focused_phase,
        compassion_words,
        closing_segments,
        closing_phase,
        closing_words,
        has_park=True,
        raw_output_observer=raw_output_observer,
    )
    return _script_response(
        duration_minutes=max(1, round(duration_seconds / 60)),
        context=payload.context,
        weather=weather,
        source=payload.source.model_dump(),
        destination=payload.destination.model_dump(),
        park=payload.park.model_dump(),
        focused_words=focused_words,
        compassion_words=compassion_words,
        closing_words=closing_words,
        focused_segments=focused_segments,
        focused_phase=focused_phase,
        closing_segments=closing_segments,
        closing_phase=closing_phase,
        nearby=nearby,
        narrations=narrations,
        usage=usage,
        fallback=fallback,
    )
