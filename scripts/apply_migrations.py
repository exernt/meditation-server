from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from meditation_server.migration_runner import apply_migrations, load_migrations
from meditation_server.repositories.asyncpg import AsyncpgExecutor
from meditation_server.repositories.data_api import RdsDataApiExecutor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply ordered Walking Meditation PostgreSQL migrations."
    )
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--database-url",
        help="Local asyncpg PostgreSQL URL; may also be supplied as DATABASE_URL.",
    )
    transport.add_argument(
        "--data-api",
        action="store_true",
        help="Use the RDS Data API and DATABASE_* environment variables.",
    )
    parser.add_argument(
        "--profile",
        help="Optional AWS profile for operator-run migrations",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    parser.add_argument("--migrations", type=Path, default=ROOT / "migrations")
    return parser.parse_args()


def required_environment_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


async def run(args: argparse.Namespace) -> tuple[str, ...]:
    executor: Any
    close_executor = False
    if args.data_api:
        client = None
        if args.profile:
            import boto3

            client = boto3.Session(
                profile_name=args.profile,
                region_name=args.region,
            ).client("rds-data")
        executor = RdsDataApiExecutor(
            resource_arn=required_environment_value("DATABASE_RESOURCE_ARN"),
            secret_arn=required_environment_value("DATABASE_SECRET_ARN"),
            database=required_environment_value("DATABASE_NAME"),
            client=client,
            region_name=args.region,
        )
    else:
        database_url = args.database_url or os.environ.get("DATABASE_URL", "")
        if not database_url:
            raise ValueError("--database-url or DATABASE_URL is required")
        executor = await AsyncpgExecutor.connect(database_url, min_size=1, max_size=2)
        close_executor = True

    try:
        return await apply_migrations(executor, load_migrations(args.migrations))
    finally:
        if close_executor:
            await executor.close()


def main() -> int:
    args = parse_args()
    try:
        applied = asyncio.run(run(args))
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    if applied:
        print("Applied migrations: " + ", ".join(applied))
    else:
        print("Database schema is already current.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
