from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
import json
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from meditation_server.repositories.executor import DatabaseExecutor


OUTBOX_RECOVERY_BATCH_SIZE = 25
OUTBOX_RETRY_BASE_SECONDS = 60
OUTBOX_RETRY_MAX_SECONDS = 60 * 60


@dataclass(frozen=True)
class OutboxPublishResult:
    published: int = 0
    failed: int = 0


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


async def publish_due_outbox(
    database: DatabaseExecutor,
    *,
    queue_url: str,
    job_id: UUID | None = None,
    max_records: int = OUTBOX_RECOVERY_BATCH_SIZE,
    client: Any | None = None,
) -> OutboxPublishResult:
    if not queue_url:
        raise ValueError("GENERATION_QUEUE_URL is not configured")
    if max_records < 1:
        raise ValueError("max_records must be positive")
    sqs = client or _sqs_client(None)
    published = 0
    failed = 0
    for _ in range(max_records):
        send_failed = False
        async with database.transaction() as transaction:
            parameters: dict[str, Any] = {}
            job_filter = ""
            if job_id is not None:
                job_filter = "AND job_id = :job_id::uuid"
                parameters["job_id"] = job_id
            pending = await database.execute(
                f"""SELECT id,job_id,attempt_count
                       FROM generation_outbox
                      WHERE published_at IS NULL
                        AND next_attempt_at <= now()
                        {job_filter}
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
                        {"job_id": str(row["job_id"])}, separators=(",", ":")
                    ),
                )
            except (BotoCoreError, ClientError) as exc:
                attempt_count = int(row["attempt_count"])
                delay_seconds = min(
                    OUTBOX_RETRY_MAX_SECONDS,
                    OUTBOX_RETRY_BASE_SECONDS * (2 ** min(attempt_count, 6)),
                )
                await database.execute(
                    """UPDATE generation_outbox
                           SET attempt_count = attempt_count + 1,
                               next_attempt_at = :next_attempt_at,
                               last_error_code = :error_code
                         WHERE id = :outbox_id::uuid""",
                    {
                        "outbox_id": row["id"],
                        "next_attempt_at": datetime.now(UTC) + timedelta(seconds=delay_seconds),
                        "error_code": _error_code(exc),
                    },
                    transaction=transaction,
                )
                failed += 1
                send_failed = True
            else:
                await database.execute(
                    """UPDATE generation_outbox
                           SET published_at = now(),last_error_code = NULL
                         WHERE id = :outbox_id::uuid""",
                    {"outbox_id": row["id"]},
                    transaction=transaction,
                )
                published += 1
        if job_id is not None or send_failed:
            break
    return OutboxPublishResult(published=published, failed=failed)


def _error_code(exc: BotoCoreError | ClientError) -> str:
    if isinstance(exc, ClientError):
        value = exc.response.get("Error", {}).get("Code")
        if isinstance(value, str) and value:
            return value[:100]
    return type(exc).__name__[:100]
