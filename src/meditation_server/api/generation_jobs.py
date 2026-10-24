from __future__ import annotations

import logging
import os
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from meditation_server.api.dependencies import CurrentUserDependency, DatabaseDependency
from meditation_server.api.models import GenerationJobRequest
from meditation_server.application.generation_jobs import (
    GenerationJobConflict,
    GenerationJobLimitExceeded,
    create_generation_job,
    get_generation_job,
    get_generation_result,
    job_response,
    normalize_request,
)
from meditation_server.application.outbox import publish_due_outbox


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/generation-jobs", tags=["generation"])
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def submit_generation_job(
    payload: GenerationJobRequest,
    user: CurrentUserDependency,
    database: DatabaseDependency,
    idempotency_key: IdempotencyKey,
):
    try:
        normalized_payload = normalize_request(payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    try:
        created = await create_generation_job(
            database,
            user_id=user.id,
            idempotency_key=idempotency_key,
            kind=payload.kind,
            payload=normalized_payload,
            audio_status=(
                "disabled"
                if os.getenv("TTS_PROVIDER", "disabled").strip().casefold() == "disabled"
                else "not_requested"
            ),
        )
    except GenerationJobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GenerationJobLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "60"},
        ) from exc

    try:
        await publish_due_outbox(
            database,
            queue_url=os.getenv("GENERATION_QUEUE_URL", ""),
            job_id=created.row["id"],
            max_records=1,
        )
    except Exception:
        # The committed outbox row is the recovery mechanism. Do not turn a
        # successfully accepted job into a client-visible failure.
        logger.exception("Immediate generation outbox publish failed", extra={"job_id": str(created.row["id"])})
    response = job_response(created.row)
    response["created"] = created.created
    return response


@router.get("/{job_id}")
async def generation_job_status(
    job_id: UUID,
    user: CurrentUserDependency,
    database: DatabaseDependency,
):
    row = await get_generation_job(database, user_id=user.id, job_id=job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    return job_response(row)


@router.get("/{job_id}/result")
async def generation_job_result(
    job_id: UUID,
    user: CurrentUserDependency,
    database: DatabaseDependency,
):
    row = await get_generation_result(database, user_id=user.id, job_id=job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Generation job not found")
    if row["status"] in {"queued", "running"}:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"job_id": str(job_id), "status": row["status"]},
            headers={"Retry-After": "2"},
        )
    if row["status"] == "failed":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Generation job failed",
                "error_code": row["last_error_code"] or "generation_failed",
            },
        )
    return {"job_id": str(job_id), **dict(row["result"])}
