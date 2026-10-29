from __future__ import annotations

import asyncio
import os

from meditation_server.application.account_maintenance import (
    cleanup_unverified_accounts,
)
from meditation_server.application.generation_jobs import cleanup_generation_data
from meditation_server.application.outbox import (
    OUTBOX_RECOVERY_BATCH_SIZE,
    publish_due_outbox,
)
from meditation_server.repositories.factory import create_data_api_executor_from_env


def handler(event: dict, _context: object) -> dict:
    return asyncio.run(_handle_event(event))


async def _handle_event(event: dict) -> dict:
    database = create_data_api_executor_from_env()
    task = event.get("task")
    if task == "outbox-recovery":
        result = await publish_due_outbox(
            database,
            queue_url=os.environ["GENERATION_QUEUE_URL"],
            max_records=OUTBOX_RECOVERY_BATCH_SIZE,
        )
        if result.failed:
            raise RuntimeError("One or more generation outbox records failed to publish")
        return {"published": result.published, "failed": result.failed}
    if task == "retention-cleanup":
        return await cleanup_generation_data(database)
    if task == "unverified-account-cleanup":
        return await cleanup_unverified_accounts(database)
    raise ValueError("Unsupported maintenance task")
