from __future__ import annotations

import os

from meditation_server.repositories.data_api import RdsDataApiExecutor


def create_data_api_executor_from_env() -> RdsDataApiExecutor:
    return RdsDataApiExecutor(
        resource_arn=os.environ["DATABASE_RESOURCE_ARN"],
        secret_arn=os.environ["DATABASE_SECRET_ARN"],
        database=os.environ["DATABASE_NAME"],
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
    )
