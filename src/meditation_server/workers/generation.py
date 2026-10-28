from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from meditation_server.api.models import ScriptWithParkRequest, ScriptWithoutParkRequest
from meditation_server.application.generation_jobs import (
    MAX_WORKER_ATTEMPTS,
    claim_generation_job,
    complete_generation_job,
    release_generation_job,
)
from meditation_server.integrations.generation_processor import (
    generate_with_park,
    generate_without_park,
)
from meditation_server.integrations.openai_generation import GeneratedText
from meditation_server.integrations.script_archive import (
    archive_generation_result,
    archive_raw_generation,
)
from meditation_server.repositories.executor import DatabaseExecutor
from meditation_server.repositories.factory import create_data_api_executor_from_env


logger = logging.getLogger(__name__)


class InvalidGenerationJob(ValueError):
    pass


def handler(event: dict, _context: object) -> dict:
    return asyncio.run(_handle_event(event, create_data_api_executor_from_env()))


async def _handle_event(event: dict, database: DatabaseExecutor) -> dict:
    failures = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId") or "")
        if not message_id:
            logger.error("Generation queue record is missing a message ID")
            continue
        succeeded = await _handle_record(record, database)
        if not succeeded:
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


async def _handle_record(record: dict[str, Any], database: DatabaseExecutor) -> bool:
    message_id = str(record["messageId"])
    try:
        body = json.loads(record.get("body") or "")
        job_id = UUID(str(body["job_id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.error("Generation queue record has an invalid body", extra={"message_id": message_id})
        return False

    job = await claim_generation_job(database, job_id=job_id, lease_token=message_id)
    if job is None:
        # Already complete/failed, or another delivery currently owns the lease.
        return True
    raw_generations: list[GeneratedText] = []
    try:
        result = await _generate(
            dict(job), raw_output_observer=raw_generations.append
        )
    except (InvalidGenerationJob, ValidationError):
        logger.exception("Stored generation job is invalid", extra={"job_id": str(job_id)})
        await release_generation_job(
            database,
            job_id=job_id,
            lease_token=message_id,
            error_code="invalid_job_payload",
            terminal=True,
        )
        return True
    except Exception:
        logger.exception("Generation job processing failed", extra={"job_id": str(job_id)})
        _archive_raw_generations(
            job_id=job_id,
            kind=str(job["kind"]),
            attempt_count=int(job["attempt_count"]),
            generations=raw_generations,
        )
        receive_count = int((record.get("attributes") or {}).get("ApproximateReceiveCount", "1"))
        terminal = max(receive_count, int(job["attempt_count"])) >= MAX_WORKER_ATTEMPTS
        await release_generation_job(
            database,
            job_id=job_id,
            lease_token=message_id,
            error_code="generation_failed",
            terminal=terminal,
        )
        return False
    completed = await complete_generation_job(
        database,
        job_id=job_id,
        lease_token=message_id,
        result=result,
    )
    if not completed:
        logger.warning(
            "generation_result_not_committed job_id=%s message_id=%s",
            job_id,
            message_id,
        )
        return True

    raw_archive_keys = _archive_raw_generations(
        job_id=job_id,
        kind=str(job["kind"]),
        attempt_count=int(job["attempt_count"]),
        generations=raw_generations,
    )
    archive_key = None
    try:
        archive_key = archive_generation_result(
            job_id=job_id,
            kind=str(job["kind"]),
            result=result,
        )
    except Exception:
        # Archiving is an operator convenience. The completed Aurora result must
        # remain available even if S3 has a transient failure.
        logger.exception("generation_archive_failed job_id=%s", job_id)

    script = result.get("script")
    script_metadata = script if isinstance(script, dict) else {}
    usage = result.get("usage")
    usage_metadata = usage if isinstance(usage, dict) else {}
    logger.info(
        "generation_outcome %s",
        json.dumps(
            {
                "job_id": str(job_id),
                "kind": job["kind"],
                "attempt_count": job["attempt_count"],
                "fallback": bool(script_metadata.get("fallback")),
                "model": usage_metadata.get("model") or script_metadata.get("model"),
                "input_tokens": usage_metadata.get("input_tokens"),
                "output_tokens": usage_metadata.get("output_tokens"),
                "total_tokens": usage_metadata.get("total_tokens"),
                "generation_cost_usd": result.get("generation_cost_usd"),
                "archived": archive_key is not None,
                "archive_key": archive_key,
                "raw_archived": bool(raw_archive_keys),
                "raw_archive_keys": raw_archive_keys,
            },
            separators=(",", ":"),
        ),
    )
    return True


def _archive_raw_generations(
    *,
    job_id: UUID,
    kind: str,
    attempt_count: int,
    generations: list[GeneratedText],
) -> list[str]:
    keys = []
    for generated in generations:
        try:
            key = archive_raw_generation(
                job_id=job_id,
                kind=kind,
                attempt_count=attempt_count,
                raw_output=generated.text,
                model=generated.usage.model,
            )
        except Exception:
            logger.exception(
                "raw_generation_archive_failed job_id=%s attempt_count=%d",
                job_id,
                attempt_count,
            )
            continue
        if key is not None:
            logger.info(
                "raw_generation_archived job_id=%s attempt_count=%d archive_key=%s",
                job_id,
                attempt_count,
                key,
            )
            keys.append(key)
    return keys


async def _generate(
    job: dict[str, Any],
    *,
    raw_output_observer: Callable[[GeneratedText], None],
) -> dict[str, Any]:
    if job["kind"] == "park":
        return await generate_with_park(
            ScriptWithParkRequest.model_validate(job["request_payload"]),
            raw_output_observer=raw_output_observer,
        )
    if job["kind"] == "without_park":
        return await generate_without_park(
            ScriptWithoutParkRequest.model_validate(job["request_payload"]),
            raw_output_observer=raw_output_observer,
        )
    raise InvalidGenerationJob("Unsupported generation job kind")

