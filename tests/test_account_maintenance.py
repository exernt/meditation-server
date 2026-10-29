from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from meditation_server.application.account_maintenance import (
    UNVERIFIED_ACCOUNT_CLEANUP_BATCH_SIZE,
    UNVERIFIED_ACCOUNT_CLEANUP_MAX_BATCHES,
    cleanup_unverified_accounts,
)
from meditation_server.repositories.executor import QueryResult
from meditation_server.workers import maintenance


def test_cleanup_removes_expired_accounts_in_bounded_batches() -> None:
    statements: list[str] = []
    counts = iter((UNVERIFIED_ACCOUNT_CLEANUP_BATCH_SIZE, 2))

    async def execute(sql: str) -> QueryResult:
        statements.append(" ".join(sql.split()))
        return QueryResult(records_updated=next(counts))

    database = SimpleNamespace(execute=execute)

    result = asyncio.run(cleanup_unverified_accounts(database))

    assert result == {
        "unverified_accounts_deleted": UNVERIFIED_ACCOUNT_CLEANUP_BATCH_SIZE + 2
    }
    assert len(statements) == 2
    assert all("email_verified = false" in sql for sql in statements)
    assert all("interval '7 days'" in sql for sql in statements)
    assert all("unverified_account_expired" in sql for sql in statements)
    assert all("DELETE FROM users" in sql for sql in statements)


def test_cleanup_stops_at_the_maximum_batch_count() -> None:
    execute = AsyncMock(
        return_value=QueryResult(
            records_updated=UNVERIFIED_ACCOUNT_CLEANUP_BATCH_SIZE
        )
    )

    result = asyncio.run(
        cleanup_unverified_accounts(SimpleNamespace(execute=execute))
    )

    assert result == {
        "unverified_accounts_deleted": (
            UNVERIFIED_ACCOUNT_CLEANUP_BATCH_SIZE
            * UNVERIFIED_ACCOUNT_CLEANUP_MAX_BATCHES
        )
    }
    assert execute.await_count == UNVERIFIED_ACCOUNT_CLEANUP_MAX_BATCHES


def test_maintenance_routes_unverified_account_cleanup(monkeypatch) -> None:
    database = object()
    cleanup = AsyncMock(return_value={"unverified_accounts_deleted": 3})
    monkeypatch.setattr(
        maintenance,
        "create_data_api_executor_from_env",
        lambda: database,
    )
    monkeypatch.setattr(maintenance, "cleanup_unverified_accounts", cleanup)

    result = asyncio.run(
        maintenance._handle_event({"task": "unverified-account-cleanup"})
    )

    assert result == {"unverified_accounts_deleted": 3}
    cleanup.assert_awaited_once_with(database)
