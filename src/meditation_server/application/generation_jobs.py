from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any
from uuid import UUID

from meditation_server.api.models import (
    GenerationJobRequest,
    ScriptWithParkRequest,
    ScriptWithoutParkRequest,
)
from meditation_server.repositories.executor import DatabaseExecutor, Transaction


MAX_IN_FLIGHT_JOBS_PER_USER = 2
MAX_JOBS_PER_USER_PER_24_HOURS = 20
WORKER_LEASE_MINUTES = 10
MAX_WORKER_ATTEMPTS = 5
TERMINAL_JOB_RETENTION_DAYS = 7
PUBLISHED_OUTBOX_RETENTION_HOURS = 24
CLEANUP_BATCH_SIZE = 500
CLEANUP_MAX_BATCHES = 10


class GenerationJobConflict(RuntimeError):
    pass


class GenerationJobLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class CreatedGenerationJob:
    row: Mapping[str, Any]
    created: bool


def normalize_request(request: GenerationJobRequest) -> dict[str, Any]:
    model = ScriptWithParkRequest if request.kind == "park" else ScriptWithoutParkRequest
    return model.model_validate(request.payload).model_dump(mode="json")


def request_hash(kind: str, payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"kind": kind, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def create_generation_job(
    database: DatabaseExecutor,
    *,
    user_id: UUID,
    idempotency_key: str,
    kind: str,
    payload: Mapping[str, Any],
    audio_status: str,
) -> CreatedGenerationJob:
    fingerprint = request_hash(kind, payload)
    async with database.transaction() as transaction:
        await database.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:user_id AS text), 0))",
            {"user_id": user_id},
            transaction=transaction,
        )
        existing = await _find_by_idempotency_key(
            database,
            user_id=user_id,
            idempotency_key=idempotency_key,
            transaction=transaction,
        )
        if existing is not None:
            if existing["request_hash"] != fingerprint:
                raise GenerationJobConflict(
                    "Idempotency-Key has already been used for a different generation request"
                )
            return CreatedGenerationJob(existing, False)

        counts = (
            await database.execute(
                """SELECT
                       COUNT(*) FILTER (WHERE status IN ('queued', 'running')) AS in_flight,
                       COUNT(*) FILTER (WHERE created_at >= now() - interval '24 hours') AS daily
                   FROM generation_jobs
                  WHERE user_id = :user_id::uuid""",
                {"user_id": user_id},
                transaction=transaction,
            )
        ).one()
        if int(counts["in_flight"]) >= MAX_IN_FLIGHT_JOBS_PER_USER:
            raise GenerationJobLimitExceeded(
                f"At most {MAX_IN_FLIGHT_JOBS_PER_USER} generation jobs may be active at once"
            )
        if int(counts["daily"]) >= MAX_JOBS_PER_USER_PER_24_HOURS:
            raise GenerationJobLimitExceeded(
                f"At most {MAX_JOBS_PER_USER_PER_24_HOURS} generation jobs may be submitted in 24 hours"
            )

        job = (
            await database.execute(
                """INSERT INTO generation_jobs
                       (user_id,idempotency_key,request_hash,kind,request_payload,audio_status)
                   VALUES
                       (:user_id::uuid,:idempotency_key,:request_hash,
                        CAST(:kind AS generation_job_kind),:request_payload::jsonb,
                        CAST(:audio_status AS generation_audio_status))
                   RETURNING id,user_id,idempotency_key,request_hash,kind::text AS kind,
                             status::text AS status,audio_status::text AS audio_status,
                             attempt_count,created_at,updated_at,completed_at,last_error_code""",
                {
                    "user_id": user_id,
                    "idempotency_key": idempotency_key,
                    "request_hash": fingerprint,
                    "kind": kind,
                    "request_payload": dict(payload),
                    "audio_status": audio_status,
                },
                transaction=transaction,
            )
        ).one()
        await database.execute(
            "INSERT INTO generation_outbox (job_id) VALUES (:job_id::uuid)",
            {"job_id": job["id"]},
            transaction=transaction,
        )
        return CreatedGenerationJob(job, True)


async def _find_by_idempotency_key(
    database: DatabaseExecutor,
    *,
    user_id: UUID,
    idempotency_key: str,
    transaction: Transaction,
) -> Mapping[str, Any] | None:
    result = await database.execute(
        """SELECT id,user_id,idempotency_key,request_hash,kind::text AS kind,
                  status::text AS status,audio_status::text AS audio_status,
                  attempt_count,created_at,updated_at,completed_at,last_error_code
             FROM generation_jobs
            WHERE user_id = :user_id::uuid AND idempotency_key = :idempotency_key""",
        {"user_id": user_id, "idempotency_key": idempotency_key},
        transaction=transaction,
    )
    return result.rows[0] if result.rows else None


async def get_generation_job(
    database: DatabaseExecutor, *, user_id: UUID, job_id: UUID
) -> Mapping[str, Any] | None:
    result = await database.execute(
        """SELECT id,kind::text AS kind,status::text AS status,
                  audio_status::text AS audio_status,attempt_count,
                  created_at,updated_at,completed_at,last_error_code
             FROM generation_jobs
            WHERE id = :job_id::uuid AND user_id = :user_id::uuid""",
        {"job_id": job_id, "user_id": user_id},
    )
    return result.rows[0] if result.rows else None


async def get_generation_result(
    database: DatabaseExecutor, *, user_id: UUID, job_id: UUID
) -> Mapping[str, Any] | None:
    result = await database.execute(
        """SELECT status::text AS status,result,last_error_code
             FROM generation_jobs
            WHERE id = :job_id::uuid AND user_id = :user_id::uuid""",
        {"job_id": job_id, "user_id": user_id},
    )
    return result.rows[0] if result.rows else None


async def claim_generation_job(
    database: DatabaseExecutor, *, job_id: UUID, lease_token: str
) -> Mapping[str, Any] | None:
    result = await database.execute(
        f"""UPDATE generation_jobs
               SET status = 'running',
                   attempt_count = attempt_count + 1,
                   lease_token = :lease_token,
                   lease_expires_at = now() + interval '{WORKER_LEASE_MINUTES} minutes',
                   updated_at = now(),
                   last_error_code = NULL
             WHERE id = :job_id::uuid
               AND (status = 'queued' OR (status = 'running' AND lease_expires_at < now()))
         RETURNING id,kind::text AS kind,request_payload,attempt_count""",
        {"job_id": job_id, "lease_token": lease_token},
    )
    return result.rows[0] if result.rows else None


async def complete_generation_job(
    database: DatabaseExecutor,
    *,
    job_id: UUID,
    lease_token: str,
    result: Mapping[str, Any],
) -> bool:
    updated = await database.execute(
        """UPDATE generation_jobs
               SET status = 'complete',result = :result::jsonb,
                   request_payload = '{}'::jsonb,
                   lease_token = NULL,lease_expires_at = NULL,
                   last_error_code = NULL,updated_at = now(),completed_at = now()
             WHERE id = :job_id::uuid AND status = 'running' AND lease_token = :lease_token""",
        {"job_id": job_id, "lease_token": lease_token, "result": dict(result)},
    )
    return updated.records_updated == 1


async def release_generation_job(
    database: DatabaseExecutor,
    *,
    job_id: UUID,
    lease_token: str,
    error_code: str,
    terminal: bool,
) -> bool:
    status = "failed" if terminal else "queued"
    updated = await database.execute(
        """UPDATE generation_jobs
               SET status = CAST(:status AS generation_job_status),
                   request_payload = CASE WHEN :terminal THEN '{}'::jsonb ELSE request_payload END,
                   lease_token = NULL,lease_expires_at = NULL,
                   last_error_code = :error_code,updated_at = now(),
                   completed_at = CASE WHEN :terminal THEN now() ELSE NULL END
             WHERE id = :job_id::uuid AND status = 'running' AND lease_token = :lease_token""",
        {
            "job_id": job_id,
            "lease_token": lease_token,
            "status": status,
            "error_code": error_code[:100],
            "terminal": terminal,
        },
    )
    return updated.records_updated == 1


async def cleanup_generation_data(database: DatabaseExecutor) -> dict[str, int]:
    outbox_deleted = await _delete_in_batches(
        database,
        f"""WITH expired AS (
                   SELECT id FROM generation_outbox
                    WHERE published_at < now() - interval '{PUBLISHED_OUTBOX_RETENTION_HOURS} hours'
                    ORDER BY published_at
                    LIMIT {CLEANUP_BATCH_SIZE}
               )
               DELETE FROM generation_outbox WHERE id IN (SELECT id FROM expired)""",
    )
    jobs_deleted = await _delete_in_batches(
        database,
        f"""WITH expired AS (
                   SELECT id FROM generation_jobs
                    WHERE status IN ('complete', 'failed')
                      AND completed_at < now() - interval '{TERMINAL_JOB_RETENTION_DAYS} days'
                    ORDER BY completed_at
                    LIMIT {CLEANUP_BATCH_SIZE}
               )
               DELETE FROM generation_jobs WHERE id IN (SELECT id FROM expired)""",
    )
    return {"outbox_deleted": outbox_deleted, "jobs_deleted": jobs_deleted}


async def _delete_in_batches(database: DatabaseExecutor, sql: str) -> int:
    deleted = 0
    for _ in range(CLEANUP_MAX_BATCHES):
        count = (await database.execute(sql)).records_updated
        deleted += count
        if count < CLEANUP_BATCH_SIZE:
            break
    return deleted


def job_response(row: Mapping[str, Any]) -> dict[str, Any]:
    job_id = str(row["id"])
    return {
        "job_id": job_id,
        "kind": row["kind"],
        "status": row["status"],
        "audio_status": row["audio_status"],
        "attempt_count": row["attempt_count"],
        "created_at": _json_datetime(row.get("created_at")),
        "updated_at": _json_datetime(row.get("updated_at")),
        "completed_at": _json_datetime(row.get("completed_at")),
        "error_code": row.get("last_error_code") if row["status"] == "failed" else None,
        "status_url": f"/v1/generation-jobs/{job_id}",
        "result_url": f"/v1/generation-jobs/{job_id}/result",
    }


def _json_datetime(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
