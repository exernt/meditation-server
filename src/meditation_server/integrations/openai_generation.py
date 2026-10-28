from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from meditation_server import settings


logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TEMPERATURE = 0.5
LONG_CONTEXT_THRESHOLD = 272_000
TOKENS_PER_MILLION = 1_000_000


class OpenAiGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GenerationUsage:
    model: str
    service_tier: str
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost_usd: float

    def response_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("cost_usd")
        return value


@dataclass(frozen=True)
class GeneratedText:
    text: str
    usage: GenerationUsage


def zero_usage() -> GenerationUsage:
    return GenerationUsage(
        model="fallback",
        service_tier="local",
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        total_tokens=0,
        cost_usd=0.0,
    )


def combine_generation_usage(usages: Sequence[GenerationUsage]) -> GenerationUsage:
    if not usages:
        return zero_usage()

    model = usages[0].model
    if any(usage.model != model for usage in usages):
        raise ValueError("Cannot combine generation usage from different models")
    service_tiers = {usage.service_tier for usage in usages}
    return GenerationUsage(
        model=model,
        service_tier=usages[0].service_tier if len(service_tiers) == 1 else "mixed",
        input_tokens=sum(usage.input_tokens for usage in usages),
        cached_input_tokens=sum(usage.cached_input_tokens for usage in usages),
        cache_write_tokens=sum(usage.cache_write_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        reasoning_tokens=sum(usage.reasoning_tokens for usage in usages),
        total_tokens=sum(usage.total_tokens for usage in usages),
        cost_usd=round(sum(usage.cost_usd for usage in usages), 10),
    )


def _token_count(value: object | None, attribute: str) -> int:
    return int(getattr(value, attribute, 0) or 0)


def _calculate_luna_standard_cost_usd(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> float:
    if input_tokens > LONG_CONTEXT_THRESHOLD:
        input_rate, cached_rate, cache_write_rate, output_rate = 0.20, 0.02, 0.25, 0.75
    else:
        input_rate, cached_rate, cache_write_rate, output_rate = 0.10, 0.01, 0.125, 0.50
    ordinary_input = max(0, input_tokens - cached_input_tokens - cache_write_tokens)
    return round(
        (
            ordinary_input * input_rate
            + cached_input_tokens * cached_rate
            + cache_write_tokens * cache_write_rate
            + output_tokens * output_rate
        )
        / TOKENS_PER_MILLION,
        10,
    )


def _generate_text_sync(
    prompt: str,
    *,
    json_schema: dict[str, Any],
    schema_name: str,
    api_key: str | None = None,
) -> GeneratedText:
    try:
        from openai import OpenAI, OpenAIError
    except ImportError as exc:
        raise OpenAiGenerationError("The OpenAI runtime dependency is unavailable") from exc

    model = settings.openai_model()
    if model != DEFAULT_MODEL:
        raise ValueError(
            f"Generation cost accounting is configured only for {DEFAULT_MODEL}; received {model}"
        )
    client = OpenAI(
        api_key=api_key or settings.openai_api_key(),
        timeout=settings.openai_timeout_seconds(),
        max_retries=settings.openai_max_retries(),
    )
    try:
        response = client.responses.create(
            model=model,
            reasoning={"effort": "none"},
            input=prompt,
            temperature=DEFAULT_TEMPERATURE,
            service_tier="default",
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": json_schema,
                    "strict": True,
                }
            },
        )
    except OpenAIError as exc:
        logger.warning("OpenAI request failed: %s", type(exc).__name__)
        raise OpenAiGenerationError(
            f"OpenAI request failed: {type(exc).__name__}"
        ) from exc

    if response.usage is None:
        raise OpenAiGenerationError("OpenAI response did not include token usage")
    input_details = response.usage.input_tokens_details
    output_details = response.usage.output_tokens_details
    input_tokens = _token_count(response.usage, "input_tokens")
    cached_input_tokens = _token_count(input_details, "cached_tokens")
    cache_write_tokens = _token_count(input_details, "cache_write_tokens")
    output_tokens = _token_count(response.usage, "output_tokens")
    usage = GenerationUsage(
        model=str(getattr(response, "model", None) or model),
        service_tier=str(getattr(response, "service_tier", None) or "default"),
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=_token_count(output_details, "reasoning_tokens"),
        total_tokens=_token_count(response.usage, "total_tokens")
        or input_tokens + output_tokens,
        cost_usd=_calculate_luna_standard_cost_usd(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
        ),
    )
    logger.info(
        "OpenAI generation completed model=%s service_tier=%s input_tokens=%d "
        "cached_input_tokens=%d cache_write_tokens=%d output_tokens=%d "
        "reasoning_tokens=%d total_tokens=%d cost_usd=%.10f",
        usage.model,
        usage.service_tier,
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.cache_write_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.total_tokens,
        usage.cost_usd,
    )
    text = (response.output_text or "").strip()
    if not text:
        raise OpenAiGenerationError("OpenAI response did not contain output text")
    return GeneratedText(text=text, usage=usage)


async def generate_text(
    prompt: str,
    *,
    json_schema: dict[str, Any],
    schema_name: str = "structured_response",
    api_key: str | None = None,
) -> GeneratedText:
    return await asyncio.to_thread(
        _generate_text_sync,
        prompt,
        json_schema=json_schema,
        schema_name=schema_name,
        api_key=api_key,
    )
