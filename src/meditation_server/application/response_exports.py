from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
import json
from typing import Any
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from meditation_server.repositories.executor import DatabaseExecutor


EXPORT_RETENTION_DAYS = 7
EXPORT_LEASE_MINUTES = 10
MAX_EXPORT_ATTEMPTS = 5
OUTBOX_RECOVERY_BATCH_SIZE = 25
OUTBOX_RETRY_BASE_SECONDS = 60
OUTBOX_RETRY_MAX_SECONDS = 60 * 60


@dataclass(frozen=True)
class ExportRequestResult:
    export: dict[str, Any]
    cached: bool


@dataclass(frozen=True)
class OutboxPublishResult:
    published: int = 0
    failed: int = 0


async def request_response_export(
    database: DatabaseExecutor,
    *,
    researcher_id: UUID,
    scope: str,
    study_id: UUID | None,
) -> ExportRequestResult:
    if scope not in {"all_studies", "study"}:
        raise ValueError("Unsupported response export scope")
    if (scope == "study") != (study_id is not None):
        raise ValueError("Study exports require a study ID and all-study exports do not")

    source = await _source_signature(
        database,
        researcher_id=researcher_id,
        study_id=study_id,
    )
    study_filter = "study_id=:study_id::uuid" if study_id is not None else "study_id IS NULL"
    parameters: dict[str, Any] = {
        "researcher_id": researcher_id,
        "scope": scope,
        "response_count": source["response_count"],
        "last_submitted_on": source["last_submitted_on"],
    }
    if study_id is not None:
        parameters["study_id"] = study_id
    cached = await database.execute(
        f"""SELECT id,researcher_id,scope::text AS scope,study_id,status::text AS status,
                   source_response_count,source_last_submitted_on,filename,created_at,
                   generated_at,expires_at
              FROM response_exports
             WHERE researcher_id=:researcher_id::uuid
               AND scope=:scope::response_export_scope
               AND {study_filter}
               AND source_response_count=:response_count
               AND source_last_submitted_on IS NOT DISTINCT FROM :last_submitted_on
               AND status='complete'::response_export_status
               AND notification_sent_at IS NOT NULL
               AND expires_at > now()
             ORDER BY generated_at DESC
             LIMIT 1""",
        parameters,
    )
    if cached.rows:
        return ExportRequestResult(export=dict(cached.rows[0]), cached=True)

    active = await database.execute(
        f"""SELECT id,researcher_id,scope::text AS scope,study_id,status::text AS status,
                   source_response_count,source_last_submitted_on,filename,created_at,
                   generated_at,expires_at
              FROM response_exports
             WHERE researcher_id=:researcher_id::uuid
               AND scope=:scope::response_export_scope
               AND {study_filter}
               AND source_response_count=:response_count
               AND source_last_submitted_on IS NOT DISTINCT FROM :last_submitted_on
               AND (
                   status IN (
                       'queued'::response_export_status,
                       'running'::response_export_status
                   )
                   OR (
                       status='complete'::response_export_status
                       AND notification_sent_at IS NULL
                   )
               )
             ORDER BY created_at DESC
             LIMIT 1""",
        parameters,
    )
    if active.rows:
        return ExportRequestResult(export=dict(active.rows[0]), cached=False)

    export_id = uuid4()
    async with database.transaction() as transaction:
        created = await database.execute(
            """INSERT INTO response_exports
                   (id,researcher_id,scope,study_id,source_response_count,source_last_submitted_on)
               VALUES
                   (:id::uuid,:researcher_id::uuid,:scope::response_export_scope,
                    :study_id::uuid,:response_count,:last_submitted_on)
               RETURNING id,researcher_id,scope::text AS scope,study_id,status::text AS status,
                         source_response_count,source_last_submitted_on,filename,created_at,
                         generated_at,expires_at""",
            {
                **parameters,
                "id": export_id,
                "study_id": study_id,
            },
            transaction=transaction,
        )
        await database.execute(
            "INSERT INTO response_export_outbox (export_id) VALUES (:export_id::uuid)",
            {"export_id": export_id},
            transaction=transaction,
        )
    return ExportRequestResult(export=dict(created.rows[0]), cached=False)


async def _source_signature(
    database: DatabaseExecutor,
    *,
    researcher_id: UUID,
    study_id: UUID | None,
) -> dict[str, Any]:
    study_filter = "AND study.id=:study_id::uuid" if study_id is not None else ""
    parameters: dict[str, Any] = {"researcher_id": researcher_id}
    if study_id is not None:
        parameters["study_id"] = study_id
    result = await database.execute(
        f"""SELECT count(response.id) AS response_count,
                   max(response.submitted_on) AS last_submitted_on
              FROM study_created_by AS ownership
              JOIN study ON study.id=ownership.study_id
              LEFT JOIN study_questionnaires AS issuance ON issuance.study_id=study.id
              LEFT JOIN questionnaire_responses AS response
                     ON response.study_questionnaire_id=issuance.id
             WHERE ownership.researcher_id=:researcher_id::uuid
                   {study_filter}""",
        parameters,
    )
    row = result.rows[0]
    return {
        "response_count": int(row["response_count"]),
        "last_submitted_on": row["last_submitted_on"],
    }


async def claim_response_export(
    database: DatabaseExecutor,
    *,
    export_id: UUID,
    lease_token: str,
) -> dict[str, Any] | None:
    result = await database.execute(
        """UPDATE response_exports
              SET status=CASE
                      WHEN status='complete'::response_export_status THEN status
                      ELSE 'running'::response_export_status
                  END,
                  attempt_count=attempt_count+1,
                  lease_token=:lease_token,
                  lease_expires_at=:lease_expires_at,
                  updated_at=now(),
                  last_error_code=NULL
            WHERE id=:export_id::uuid
              AND (
                  status='queued'::response_export_status
                  OR (
                      status='running'::response_export_status
                      AND lease_expires_at < now()
                  )
                  OR (
                      status='complete'::response_export_status
                      AND notification_sent_at IS NULL
                      AND (lease_token IS NULL OR lease_expires_at < now())
                  )
              )
         RETURNING id,researcher_id,scope::text AS scope,study_id,status::text AS status,attempt_count,
                   source_response_count,source_last_submitted_on""",
        {
            "export_id": export_id,
            "lease_token": lease_token,
            "lease_expires_at": datetime.now(UTC) + timedelta(minutes=EXPORT_LEASE_MINUTES),
        },
    )
    return dict(result.rows[0]) if result.rows else None


async def complete_response_export(
    database: DatabaseExecutor,
    *,
    export_id: UUID,
    lease_token: str,
    s3_key: str,
    filename: str,
    generated_at: datetime,
) -> bool:
    result = await database.execute(
        """UPDATE response_exports
              SET status='complete'::response_export_status,
                  s3_key=:s3_key,
                  filename=:filename,
                  generated_at=:generated_at,
                  expires_at=:expires_at,
                  updated_at=now(),
                  last_error_code=NULL
            WHERE id=:export_id::uuid
              AND status='running'::response_export_status
              AND lease_token=:lease_token""",
        {
            "export_id": export_id,
            "lease_token": lease_token,
            "s3_key": s3_key,
            "filename": filename,
            "generated_at": generated_at,
            "expires_at": generated_at + timedelta(days=EXPORT_RETENTION_DAYS),
        },
    )
    return result.records_updated == 1


async def mark_response_export_notified(
    database: DatabaseExecutor,
    *,
    export_id: UUID,
    lease_token: str,
) -> bool:
    result = await database.execute(
        """UPDATE response_exports
              SET notification_sent_at=now(),
                  lease_token=NULL,
                  lease_expires_at=NULL,
                  updated_at=now(),
                  last_error_code=NULL
            WHERE id=:export_id::uuid
              AND status='complete'::response_export_status
              AND notification_sent_at IS NULL
              AND lease_token=:lease_token""",
        {"export_id": export_id, "lease_token": lease_token},
    )
    return result.records_updated == 1


async def release_response_export(
    database: DatabaseExecutor,
    *,
    export_id: UUID,
    lease_token: str,
    error_code: str,
    terminal: bool,
) -> None:
    status = "failed" if terminal else "queued"
    await database.execute(
        """UPDATE response_exports
              SET status=:status::response_export_status,
                  lease_token=NULL,
                  lease_expires_at=NULL,
                  last_error_code=:error_code,
                  updated_at=now()
            WHERE id=:export_id::uuid
              AND status='running'::response_export_status
              AND lease_token=:lease_token""",
        {
            "export_id": export_id,
            "lease_token": lease_token,
            "status": status,
            "error_code": error_code[:100],
        },
    )


async def release_completed_response_export(
    database: DatabaseExecutor,
    *,
    export_id: UUID,
    lease_token: str,
    error_code: str,
    terminal: bool,
) -> None:
    await database.execute(
        """UPDATE response_exports
              SET status=CASE
                      WHEN :terminal THEN 'failed'::response_export_status
                      ELSE 'complete'::response_export_status
                  END,
                  s3_key=CASE WHEN :terminal THEN NULL ELSE s3_key END,
                  filename=CASE WHEN :terminal THEN NULL ELSE filename END,
                  generated_at=CASE WHEN :terminal THEN NULL ELSE generated_at END,
                  expires_at=CASE WHEN :terminal THEN NULL ELSE expires_at END,
                  lease_token=NULL,
                  lease_expires_at=NULL,
                  last_error_code=:error_code,
                  updated_at=now()
            WHERE id=:export_id::uuid
              AND status='complete'::response_export_status
              AND notification_sent_at IS NULL
              AND lease_token=:lease_token""",
        {
            "export_id": export_id,
            "lease_token": lease_token,
            "terminal": terminal,
            "error_code": error_code[:100],
        },
    )


@lru_cache(maxsize=4)
def _sqs_client(region: str | None):
    return boto3.client(
        "sqs",
        region_name=region,
        config=Config(
            retries={"total_max_attempts": 2, "mode": "standard"},
            connect_timeout=1,
            read_timeout=2,
            max_pool_connections=10,
            user_agent_appid="walking-meditation",
        ),
    )


async def publish_due_export_outbox(
    database: DatabaseExecutor,
    *,
    queue_url: str,
    export_id: UUID | None = None,
    max_records: int = OUTBOX_RECOVERY_BATCH_SIZE,
    client: Any | None = None,
) -> OutboxPublishResult:
    if not queue_url:
        raise ValueError("RESPONSE_EXPORT_QUEUE_URL is not configured")
    if max_records < 1:
        raise ValueError("max_records must be positive")
    sqs = client or _sqs_client(None)
    published = 0
    failed = 0
    for _ in range(max_records):
        send_failed = False
        async with database.transaction() as transaction:
            parameters: dict[str, Any] = {}
            export_filter = ""
            if export_id is not None:
                export_filter = "AND export_id=:export_id::uuid"
                parameters["export_id"] = export_id
            pending = await database.execute(
                f"""SELECT id,export_id,attempt_count
                       FROM response_export_outbox
                      WHERE published_at IS NULL
                        AND next_attempt_at <= now()
                        {export_filter}
                      ORDER BY created_at
                      LIMIT 1
                      FOR UPDATE SKIP LOCKED""",
                parameters,
                transaction=transaction,
            )
            if not pending.rows:
                break
            row = pending.rows[0]
            try:
                await asyncio.to_thread(
                    sqs.send_message,
                    QueueUrl=queue_url,
                    MessageBody=json.dumps(
                        {"export_id": str(row["export_id"])}, separators=(",", ":")
                    ),
                )
            except (BotoCoreError, ClientError) as exc:
                attempt_count = int(row["attempt_count"])
                delay_seconds = min(
                    OUTBOX_RETRY_MAX_SECONDS,
                    OUTBOX_RETRY_BASE_SECONDS * (2 ** min(attempt_count, 6)),
                )
                await database.execute(
                    """UPDATE response_export_outbox
                           SET attempt_count=attempt_count+1,
                               next_attempt_at=:next_attempt_at,
                               last_error_code=:error_code
                         WHERE id=:outbox_id::uuid""",
                    {
                        "outbox_id": row["id"],
                        "next_attempt_at": datetime.now(UTC)
                        + timedelta(seconds=delay_seconds),
                        "error_code": _error_code(exc),
                    },
                    transaction=transaction,
                )
                failed += 1
                send_failed = True
            else:
                await database.execute(
                    """UPDATE response_export_outbox
                           SET published_at=now(),last_error_code=NULL
                         WHERE id=:outbox_id::uuid""",
                    {"outbox_id": row["id"]},
                    transaction=transaction,
                )
                published += 1
        if export_id is not None or send_failed:
            break
    return OutboxPublishResult(published=published, failed=failed)


def _error_code(exc: BotoCoreError | ClientError) -> str:
    if isinstance(exc, ClientError):
        value = exc.response.get("Error", {}).get("Code")
        if isinstance(value, str) and value:
            return value[:100]
    return type(exc).__name__[:100]
