from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from meditation_server import settings


@lru_cache(maxsize=4)
def _client(region: str | None) -> Any:
    return boto3.client(
        "s3",
        region_name=region,
        config=Config(
            retries={"total_max_attempts": 3, "mode": "standard"},
            connect_timeout=3,
            read_timeout=30,
            max_pool_connections=10,
            user_agent_appid="walking-meditation",
        ),
    )


async def store_response_export(
    *,
    key: str,
    filename: str,
    content: bytes,
    client: Any | None = None,
) -> None:
    s3 = client or _client(None)
    await asyncio.to_thread(
        s3.put_object,
        Bucket=settings.response_export_bucket(),
        Key=key,
        Body=content,
        ContentType="text/csv; charset=utf-8",
        ContentDisposition=f'attachment; filename="{filename}"',
        ServerSideEncryption="AES256",
    )


def response_export_download_url(
    *,
    key: str,
    filename: str,
    client: Any | None = None,
) -> str:
    s3 = client or _client(None)
    return str(
        s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.response_export_bucket(),
                "Key": key,
                "ResponseContentType": "text/csv; charset=utf-8",
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=settings.response_export_presigned_ttl_seconds(),
        )
    )
