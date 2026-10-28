from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any, Mapping
from uuid import UUID

import boto3

from meditation_server import settings


def _put_result(
    *,
    bucket: str,
    key: str,
    job_id: UUID,
    kind: str,
    result: Mapping[str, Any],
) -> None:
    script = result.get("script")
    script_metadata = script if isinstance(script, Mapping) else {}
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
        Metadata={
            "job-id": str(job_id),
            "kind": kind,
            "fallback": str(bool(script_metadata.get("fallback"))).lower(),
            "model": str(script_metadata.get("model") or "unknown")[:128],
        },
    )


def archive_generation_result(
    *,
    job_id: UUID,
    kind: str,
    result: Mapping[str, Any],
) -> str | None:
    """Archive a completed result when an archive bucket is configured.

    The caller treats this operator-facing archive as best-effort because Aurora
    remains the delivery system of record for generation jobs.
    """
    bucket = settings.generation_archive_bucket()
    if bucket is None:
        return None
    now = datetime.now(UTC)
    key = f"generation-results/{now:%Y/%m/%d}/{job_id}.json"
    _put_result(
        bucket=bucket,
        key=key,
        job_id=job_id,
        kind=kind,
        result=result,
    )
    return key


def archive_raw_generation(
    *,
    job_id: UUID,
    kind: str,
    attempt_count: int,
    raw_output: str,
    model: str,
) -> str | None:
    """Archive the exact model output for evaluation when configured."""
    bucket = settings.generation_archive_bucket()
    if bucket is None:
        return None
    now = datetime.now(UTC)
    key = (
        f"generation-results/{now:%Y/%m/%d}/{job_id}."
        f"attempt-{attempt_count}.raw.json"
    )
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=raw_output.encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
        Metadata={
            "job-id": str(job_id),
            "kind": kind,
            "attempt-count": str(attempt_count),
            "artifact": "raw-model-output",
            "model": model[:128],
        },
    )
    return key
