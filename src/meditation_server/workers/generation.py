from __future__ import annotations

import asyncio
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
from meditation_server.integrations.scripts import with_park, without_park
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
    try:
        result = await _generate(dict(job))
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
    await complete_generation_job(
        database,
        job_id=job_id,
        lease_token=message_id,
        result=result,
    )
    return True


async def _generate(job: dict[str, Any]) -> dict[str, Any]:
    if job["kind"] == "park":
        return await with_park(ScriptWithParkRequest.model_validate(job["request_payload"]))
    if job["kind"] == "without_park":
        return await without_park(
            ScriptWithoutParkRequest.model_validate(job["request_payload"])
        )
    raise InvalidGenerationJob("Unsupported generation job kind")

