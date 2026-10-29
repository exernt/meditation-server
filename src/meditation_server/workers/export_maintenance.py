from __future__ import annotations

import asyncio
import os

from meditation_server.application.response_exports import (
    OUTBOX_RECOVERY_BATCH_SIZE,
    publish_due_export_outbox,
)
from meditation_server.repositories.factory import create_data_api_executor_from_env


def handler(event: dict, _context: object) -> dict:
    return asyncio.run(_handle_event(event))


async def _handle_event(event: dict) -> dict:
    if event.get("task") != "outbox-recovery":
        raise ValueError("Unsupported export maintenance task")
    result = await publish_due_export_outbox(
        create_data_api_executor_from_env(),
        queue_url=os.environ["RESPONSE_EXPORT_QUEUE_URL"],
        max_records=OUTBOX_RECOVERY_BATCH_SIZE,
    )
    if result.failed:
        raise RuntimeError("One or more response export outbox records failed to publish")
    return {"published": result.published, "failed": result.failed}
